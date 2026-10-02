"""Validate the fork's standalone CLI contract independently of upstream layout.

Schema 3 archives contain one root executable and no bundled runtime resources.
The producer and consumer share bounded extraction and identity validation.
"""

import re
import shutil
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from common import LifecycleError, sha256
from upstream_version import version_key

MAX_BINARY_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_TARGET = re.compile(
    r"(x86_64|aarch64)-(unknown-linux-(gnu|musl)|apple-darwin|pc-windows-msvc)"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def artifact_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Bind a genuine standalone descriptor to manifest version and platform."""
    metadata = payload.get("artifact")
    version = payload.get("sourceVersion")
    if (
        payload.get("schemaVersion") != 3
        or "package" in payload
        or not isinstance(metadata, dict)
    ):
        raise LifecycleError("standalone CLI artifact schema 必须是 schemaVersion=3")
    target = metadata.get("target")
    match = _TARGET.fullmatch(target) if isinstance(target, str) else None
    system = (
        "windows"
        if "windows" in str(target)
        else "darwin"
        if "darwin" in str(target)
        else "linux"
    )
    expected = {
        "kind": "standalone-cli",
        "version": version,
        "target": target,
        "entrypoint": "codex.exe" if system == "windows" else "codex",
    }
    if (
        match is None
        or not isinstance(version, str)
        or version_key(version) is None
        or metadata != expected
        or payload.get("platform") != f"{match[1]}-{system}"
    ):
        raise LifecycleError("standalone CLI artifact version/target/platform 不一致")
    return metadata


def validate_cli_directory(directory: Path, metadata: dict[str, Any]) -> None:
    """Reject extra files and links before treating local output as one CLI."""
    expected = directory / metadata["entrypoint"]
    entries = list(directory.iterdir())
    if (
        entries != [expected]
        or expected.is_symlink()
        or not expected.is_file()
        or not 0 < expected.stat().st_size <= MAX_BINARY_BYTES
        or not expected.stat().st_mode & 0o111
    ):
        raise LifecycleError("standalone CLI directory 必须只包含一个普通可执行文件")


@contextmanager
def verified_cli_archive(
    archive_path: Path, metadata: dict[str, Any], binary_sha256: str
) -> Iterator[Path]:
    """Extract one bounded executable, verify its digest, and remove temporary data."""
    if not isinstance(binary_sha256, str) or _SHA256.fullmatch(binary_sha256) is None:
        raise LifecycleError("standalone CLI SHA-256 无效")
    try:
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise LifecycleError("standalone CLI archive 超过安全大小限制")
        with tempfile.TemporaryDirectory(prefix="co-standalone-cli-") as temporary:
            directory = Path(temporary)
            with tarfile.open(archive_path, "r:gz") as archive:
                member = archive.next()
                if (
                    member is None
                    or member.name != metadata["entrypoint"]
                    or member.name not in {"codex", "codex.exe"}
                    or not member.isreg()
                    or not 0 < member.size <= MAX_BINARY_BYTES
                    or not member.mode & 0o111
                    or member.mode & ~0o777
                ):
                    raise LifecycleError(
                        "standalone CLI archive 成员必须是 root 普通可执行文件"
                    )
                if archive.next() is not None:
                    raise LifecycleError("standalone CLI archive 必须只包含一个成员")
                source = archive.extractfile(member)
                if source is None:
                    raise LifecycleError("standalone CLI archive 成员无法读取")
                binary = directory / member.name
                with source, binary.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                binary.chmod(member.mode & 0o777)
                if binary.stat().st_size != member.size:
                    raise LifecycleError("standalone CLI archive member size 不一致")
            validate_cli_directory(directory, metadata)
            if sha256(binary) != binary_sha256:
                raise LifecycleError("CLI binary SHA-256 与 manifest 不一致")
            yield directory
    except (OSError, tarfile.TarError) as error:
        raise LifecycleError("standalone CLI archive 解包验证失败") from error
