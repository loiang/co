"""Invoke upstream packaging in a child environment without changing import state."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import LifecycleError, OutputMode, read_json, run

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
    """Bind explicit release inputs before launching the official source builder."""

    root: Path
    output_dir: Path
    source_rev: str
    version: str
    cores: int = 0


@dataclass(frozen=True)
class NativePackage:
    """Carry validated upstream metadata and its complete archive to asset emission."""

    directory: Path
    archive: Path
    platform: str
    metadata: dict[str, Any]


def _environment(request: PackageRequest) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_REPO_ROOT"] = str(request.root)
    env["PYTHONPATH"] = str(request.root / "scripts")
    env.pop("CARGO_BUILD_JOBS", None)
    if request.cores:
        env["CARGO_BUILD_JOBS"] = str(request.cores)
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


def build_package(request: PackageRequest) -> NativePackage:
    """Build and validate the upstream layout with fixed target and version inputs.

    Args:
        request: Source checkout, isolated output directory, and release inputs.

    Returns:
        Complete package archive plus metadata read back after upstream validation.

    Raises:
        LifecycleError: The builder, official validator, or metadata check fails.
    """
    if request.cores < 0:
        raise LifecycleError("Cargo cores 必须是非负整数")
    env = _environment(request)
    target, platform = _host_target(request.root, env)
    bwrap_bin = None
    if "-linux-" in target:
        from bwrap import resolve_bwrap_binary

        bwrap_bin = resolve_bwrap_binary(request.version, target)
    package_dir = request.output_dir / "package"
    archive = request.output_dir / (
        f"co-cli-{platform}-{request.source_rev[:10]}.tar.gz"
    )
    command = [
        sys.executable,
        str(request.root / "scripts/build_codex_package.py"),
        "--variant",
        "codex",
        "--target",
        target,
        "--package-version",
        request.version,
        "--package-dir",
        str(package_dir),
        "--cargo-profile",
        "release",
        "--archive-output",
        str(archive),
    ]
    if bwrap_bin is not None:
        command.extend(["--bwrap-bin", str(bwrap_bin)])
    run(command, cwd=request.root, env=env, capture=OutputMode.INHERIT)
    run(
        [sys.executable, "-c", _VALIDATE_PACKAGE, str(package_dir), target],
        cwd=request.root,
        env=env,
    )
    metadata = read_json(package_dir / "codex-package.json")
    if metadata.get("version") != request.version:
        raise LifecycleError("官方 package version 与解析的 stable release 不一致")
    if not archive.is_file():
        raise LifecycleError("官方 builder 未生成 package archive")
    return NativePackage(package_dir, archive, platform, metadata)
