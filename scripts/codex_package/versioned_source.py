"""Stamp Cargo's workspace version in a disposable copy of the current source.

Cargo owns CARGO_PKG_VERSION and its components. Updating an isolated manifest
keeps every crate consistent without editing the user's manifest or lockfile.
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
        Original workspace if already stamped, otherwise its disposable copy.
    """
    workspace = root / "codex-rs"
    manifest = (workspace / "Cargo.toml").read_text(encoding="utf-8")
    if tomllib.loads(manifest)["workspace"]["package"]["version"] == version:
        yield workspace
        return
    with tempfile.TemporaryDirectory(
        prefix="codex-release-source-", dir="/tmp"
    ) as temp:
        staged_root = Path(temp)
        _copy_source(root, staged_root)
        staged = staged_root / "codex-rs"
        _stamp_manifest(staged / "Cargo.toml", manifest, version)
        original_pins = _dependency_pins(staged / "Cargo.lock")
        subprocess.run(
            [cargo, "update", "--workspace"],
            cwd=staged,
            env=env,
            check=True,
        )
        if _dependency_pins(staged / "Cargo.lock") != original_pins:
            raise RuntimeError(
                "Release version stamping changed locked dependency pins"
            )
        yield staged


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
