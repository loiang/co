"""Stamp Cargo's workspace version in disposable build sources.

Managed co builds already provide an isolated worktree and stamp it in place.
Standalone packaging retains a disposable copy to preserve the caller's files.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import tomllib


@contextmanager
def versioned_workspace(
    root: Path, version: str, cargo: str, env: dict[str, str]
) -> Iterator[Path]:
    """Yield a Cargo workspace stamped for one package build, then remove it.

    Args:
        root: Git checkout containing the current source, including local edits.
        version: Validated package version shared by all workspace crates.
        cargo: Cargo executable used for the subsequent locked build.
        env: Build environment, including the original absolute target directory.

    Yields:
        Managed build worktree in place, otherwise a disposable source copy.
    """
    workspace = root / "codex-rs"
    manifest = (workspace / "Cargo.toml").read_text(encoding="utf-8")
    if tomllib.loads(manifest)["workspace"]["package"]["version"] == version:
        yield workspace
        return
    if env.get("CO_BUILD_WORKTREE") == str(root):
        _stamp_and_update(workspace, manifest, version, (cargo, env))
        yield workspace
        return
    with tempfile.TemporaryDirectory(
        prefix="codex-release-source-", dir="/tmp"
    ) as temp:
        staged_root = Path(temp)
        _copy_source(root, staged_root)
        staged = staged_root / "codex-rs"
        _stamp_and_update(staged, manifest, version, (cargo, env))
        yield staged


def _stamp_and_update(
    workspace: Path,
    manifest: str,
    version: str,
    builder: tuple[str, dict[str, str]],
) -> None:
    cargo, env = builder
    _stamp_manifest(workspace / "Cargo.toml", manifest, version)
    original_pins = _dependency_pins(workspace / "Cargo.lock")
    subprocess.run(
        [cargo, "update", "--workspace"],
        cwd=workspace,
        env=env,
        check=True,
        pass_fds=build_lock_fds(env),
    )
    if _dependency_pins(workspace / "Cargo.lock") != original_pins:
        raise RuntimeError("Release version stamping changed locked dependency pins")


def build_lock_fds(env: dict[str, str]) -> tuple[int, ...]:
    """Keep the owning co build locked while an orphaned Cargo process runs."""
    descriptor = env.get("CO_BUILD_LOCK_FD")
    return (int(descriptor),) if descriptor is not None else ()


def _copy_source(root: Path, destination: Path) -> None:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    for encoded in set(result.stdout.split(b"\0")) - {b""}:
        relative = Path(os.fsdecode(encoded))
        source = root / relative
        if not source.exists() and not source.is_symlink():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def _stamp_manifest(path: Path, content: str, version: str) -> None:
    pattern = (
        r'(?ms)(^\[workspace\.package\]\s*\n(?:(?!^\[).)*?^version\s*=\s*)"[^"\n]*"'
    )
    stamped, count = re.subn(
        pattern, lambda match: match[1] + json.dumps(version), content, count=1
    )
    if count != 1:
        raise RuntimeError("Cannot locate Cargo workspace.package.version")
    path.write_text(stamped, encoding="utf-8")


def _dependency_pins(path: Path) -> set[tuple[str, str, str, str | None]]:
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        (
            package["name"],
            package["version"],
            package["source"],
            package.get("checksum"),
        )
        for package in lock["package"]
        if "source" in package
    }
