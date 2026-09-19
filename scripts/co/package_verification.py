"""Verify native package identity and safely materialize the complete archive."""

import os
import re
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from common import LifecycleError, read_json, run, sha256
from native_package import _VALIDATE_PACKAGE

MAX_PACKAGE_SIZE = 4 * 1024**3
MAX_MEMBER_SIZE = 1024**3
MAX_MEMBERS = 10000


def package_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Bind canonical upstream paths and target to the enclosing release identity."""
    metadata = payload.get("package")
    platform = payload.get("platform")
    if not isinstance(metadata, dict) or not isinstance(platform, str):
        raise LifecycleError("release package metadata 缺失")
    target = metadata.get("target")
    match = re.fullmatch(
        r"(x86_64|aarch64)-(unknown-linux-(gnu|musl)|apple-darwin|pc-windows-msvc)",
        str(target),
    )
    system = (
        "windows"
        if "windows" in str(target)
        else ("darwin" if "darwin" in str(target) else "linux")
    )
    expected = {
        "layoutVersion": 1,
        "version": payload.get("sourceVersion"),
        "target": target,
        "variant": "codex",
        "entrypoint": "bin/codex.exe" if system == "windows" else "bin/codex",
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }
    if (
        match is None
        or platform != f"{match[1]}-{system}"
        or metadata != expected
        or not isinstance(expected["version"], str)
        or not expected["version"]
    ):
        raise LifecycleError(
            "release package metadata 与官方 layout/target/version 不一致"
        )
    return metadata


def validate_directory(directory: Path, metadata: dict[str, Any]) -> None:
    """Run upstream layout checks in a child environment after metadata equality."""
    if read_json(directory / "codex-package.json") != metadata:
        raise LifecycleError("archive package metadata 与 release manifest 不一致")
    scripts = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(scripts)
    environment["CODEX_REPO_ROOT"] = str(scripts.parent)
    run(
        [sys.executable, "-c", _VALIDATE_PACKAGE, str(directory), metadata["target"]],
        cwd=scripts.parent,
        env=environment,
    )


def _members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    entries: dict[str, tarfile.TarInfo] = {}
    size = 0
    for member in archive:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in member.name
            or not path.parts
            or path.as_posix() != member.name.rstrip("/")
            or path.parts[0]
            not in {"bin", "codex-resources", "codex-path", "codex-package.json"}
            or not (member.isdir() or member.isreg())
            or member.name.rstrip("/") in entries
            or not 0 <= member.size <= MAX_MEMBER_SIZE
        ):
            raise LifecycleError(
                f"CLI archive 含不安全路径、类型或重复项: {member.name}"
            )
        entries[member.name.rstrip("/")] = member
        size += member.size
        if len(entries) > MAX_MEMBERS or size > MAX_PACKAGE_SIZE:
            raise LifecycleError("CLI archive 超过 package 安全大小限制")
    for name in entries:
        for parent in PurePosixPath(name).parents:
            if str(parent) in entries and not entries[str(parent)].isdir():
                raise LifecycleError("CLI archive 文件与目录路径冲突")
    return list(entries.values())


def _extract(archive: tarfile.TarFile, directory: Path) -> None:
    for member in _members(archive):
        target = directory / member.name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise LifecycleError("CLI archive regular member 无法读取")
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        target.chmod(0o755 if member.mode & 0o111 else 0o644)
        if target.stat().st_size != member.size:
            raise LifecycleError("CLI archive member size 不一致")


@contextmanager
def verified_package(
    archive_path: Path,
    metadata: dict[str, Any],
    binary_sha256: str,
) -> Iterator[Path]:
    """Extract only regular safe members and validate every official resource.

    Yields:
        Temporary complete package directory, removed on success or failure.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="co-package-") as temporary:
            directory = Path(temporary)
            with tarfile.open(archive_path, "r:gz") as archive:
                _extract(archive, directory)
            validate_directory(directory, metadata)
            if sha256(directory / metadata["entrypoint"]) != binary_sha256:
                raise LifecycleError("CLI binary SHA-256 与 manifest 不一致")
            yield directory
    except (OSError, tarfile.TarError) as error:
        raise LifecycleError("CLI archive 解包验证失败") from error


def compare_package(directory: Path, extracted: Path) -> None:
    """Ensure local host-gate files are exactly the bytes that will be published."""
    local = {path.relative_to(directory): path for path in directory.rglob("*")}
    packed = {path.relative_to(extracted): path for path in extracted.rglob("*")}
    if local.keys() != packed.keys():
        raise LifecycleError("build package directory 与 archive layout 不一致")
    for name, path in local.items():
        other = packed[name]
        if (
            path.is_symlink()
            or path.is_dir() != other.is_dir()
            or path.is_file() != other.is_file()
        ):
            raise LifecycleError("build package directory 含链接或类型不一致")
        if path.is_file() and (
            sha256(path) != sha256(other)
            or bool(path.stat().st_mode & 0o111) != bool(other.stat().st_mode & 0o111)
        ):
            raise LifecycleError(
                f"build package directory 与 archive checksum 不一致: {name}"
            )
