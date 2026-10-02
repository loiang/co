"""Compile only the stamped Codex CLI and archive its verified executable."""

import json
import os
import shutil
import tarfile
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import LifecycleError, OutputMode, run
from build_tools import prepend_tool_path, resolve_make_bin
from standalone_artifact import artifact_metadata, validate_cli_directory

_HOST_TARGET = """
import json
import sys
from codex_package.targets import TARGET_SPECS
value = sys.argv[1]
spec = TARGET_SPECS[value]
system = 'windows' if spec.is_windows else 'linux' if spec.is_linux else 'darwin'
print(json.dumps({'target': value, 'platform': value.split('-')[0] + '-' + system}))
"""
_VALIDATE_PACKAGE = """
import sys
from pathlib import Path
from codex_package.dotslash import artifact_for_target
from codex_package.layout import validate_package_dir
from codex_package.targets import PACKAGE_VARIANTS, TARGET_SPECS
from codex_package.zsh import ZSH_MANIFEST
spec = TARGET_SPECS[sys.argv[2]]
include_zsh = artifact_for_target(
    spec, ZSH_MANIFEST, artifact_label='codex-zsh', missing_ok=True
) is not None
validate_package_dir(
    Path(sys.argv[1]), PACKAGE_VARIANTS['codex'], spec, include_zsh=include_zsh
)
"""


@dataclass(frozen=True)
class PackageRequest:
    """Bind immutable source, CLI version and resource limits before compilation."""

    root: Path
    output_dir: Path
    source_rev: str
    version: str
    cores: int = 0
    target_dir: Path | None = None
    lock_fd: int | None = None


@dataclass(frozen=True)
class NativePackage:
    """Carry the standalone CLI descriptor and its single-executable archive."""

    directory: Path
    archive: Path
    platform: str
    metadata: dict[str, Any]


def _environment(request: PackageRequest) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_REPO_ROOT"] = str(request.root)
    env["PYTHONPATH"] = str(request.root / "scripts")
    env["CARGO_TARGET_DIR"] = str(
        (request.target_dir or request.root / "codex-rs/target").resolve()
    )
    env["CO_BUILD_WORKTREE"] = str(request.root)
    env["CODEX_CLI_VERSION"] = request.version
    env.pop("CARGO_BUILD_TARGET", None)
    if request.lock_fd is not None:
        env["CO_BUILD_LOCK_FD"] = str(request.lock_fd)
    env.pop("CARGO_BUILD_JOBS", None)
    if request.cores:
        env["CARGO_BUILD_JOBS"] = str(request.cores)
    prepend_tool_path(env, resolve_make_bin(env))
    return env


def _host_target(root: Path, env: dict[str, str]) -> tuple[str, str]:
    compiler = run(["rustc", "--version", "--verbose"], cwd=root, env=env)
    hosts = [
        line.removeprefix("host: ")
        for line in (compiler.stdout or "").splitlines()
        if line.startswith("host: ")
    ]
    if len(hosts) != 1 or not hosts[0]:
        raise LifecycleError("rustc 未返回唯一 host target")
    result = run([sys.executable, "-c", _HOST_TARGET, hosts[0]], cwd=root, env=env)
    try:
        value = json.loads(result.stdout or "")
        target, platform = value["target"], value["platform"]
        if not isinstance(target, str) or not isinstance(platform, str):
            raise ValueError("target and platform must be strings")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise LifecycleError("官方 builder host target metadata 无效") from error
    return target, platform


_BUILD_CLI = """
import os
import subprocess
import sys
from pathlib import Path
from codex_package.versioned_source import build_lock_fds, versioned_workspace
root = Path(sys.argv[1])
version = sys.argv[2]
env = dict(os.environ)
with versioned_workspace(root, version, 'cargo', env) as workspace:
    subprocess.run(
        ['cargo', 'build', '--release', '--locked', '-p', 'codex-cli', '--bin', 'codex'],
        cwd=workspace, env=env, check=True, pass_fds=build_lock_fds(env)
    )
"""


def build_package(request: PackageRequest) -> NativePackage:
    """Compile the CLI only, verifying its version before emitting an archive."""
    if request.cores < 0:
        raise LifecycleError("Cargo cores 必须是非负整数")
    env = _environment(request)
    target, platform = _host_target(request.root, env)
    entrypoint = "codex.exe" if platform.endswith("-windows") else "codex"
    metadata = artifact_metadata(
        {
            "schemaVersion": 3,
            "sourceVersion": request.version,
            "platform": platform,
            "artifact": {
                "kind": "standalone-cli",
                "version": request.version,
                "target": target,
                "entrypoint": entrypoint,
            },
        }
    )
    run(
        [sys.executable, "-c", _BUILD_CLI, str(request.root), request.version],
        cwd=request.root,
        env=env,
        capture=OutputMode.INHERIT,
        pass_fds=(request.lock_fd,) if request.lock_fd is not None else (),
    )
    binary = Path(env["CARGO_TARGET_DIR"]) / "release" / entrypoint
    if binary.is_symlink() or not binary.is_file() or not binary.stat().st_mode & 0o111:
        raise LifecycleError("Cargo 未生成普通可执行 codex binary")
    result = run([str(binary), "--version"], cwd=request.root, env=env)
    if (result.stdout or "").strip() != f"codex-cli {request.version}":
        raise LifecycleError("CLI --version 与所选官方 tag 不一致")
    directory = request.output_dir / "artifact"
    directory.mkdir()
    shutil.copy2(binary, directory / entrypoint)
    validate_cli_directory(directory, metadata)
    archive = request.output_dir / f"co-cli-{platform}-{request.source_rev[:10]}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(directory / entrypoint, arcname=entrypoint, recursive=False)
    return NativePackage(directory, archive, platform, metadata)
