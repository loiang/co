"""Build the static CLI and emit checksummed release assets."""

import gzip
import json
import re
import struct
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, BinaryIO
from urllib.request import Request, urlopen

from common import (
    LifecycleError,
    git,
    git_flake,
    require_no_untracked,
    require_repo,
    run,
    sha256,
    timestamp,
    write_json,
)
from evidence import source_identity

OFFICIAL_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/openai/codex/releases/latest"
)
_MAX_RELEASE_METADATA_BYTES = 1024 * 1024
_STABLE_RELEASE_TAG = re.compile(
    r"rust-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)


def _platform(root: Path) -> str:
    result = run(
        ["nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"],
        cwd=root,
    )
    value = (result.stdout or "").strip()
    if not value:
        raise LifecycleError("Nix platform 解析为空")
    return value


def _official_version() -> str:
    """Resolve the stable upstream version once for a reproducible build input."""
    request = Request(
        OFFICIAL_LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "loiang-co-build",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw_metadata = response.read(_MAX_RELEASE_METADATA_BYTES + 1)
    except OSError as error:
        raise LifecycleError("无法解析官方 latest stable release") from error
    if len(raw_metadata) > _MAX_RELEASE_METADATA_BYTES:
        raise LifecycleError("官方 latest stable release metadata 超过 1 MiB")
    try:
        metadata = json.loads(raw_metadata)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise LifecycleError(
            "官方 latest stable release metadata 不是有效 JSON"
        ) from error
    if not isinstance(metadata, dict):
        raise LifecycleError("官方 latest stable release metadata 不是 JSON object")
    tag = metadata.get("tag_name")
    if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        raise LifecycleError("官方 latest stable release 不是稳定 release")
    if not isinstance(tag, str) or _STABLE_RELEASE_TAG.fullmatch(tag) is None:
        raise LifecycleError("官方 latest stable release tag 不是 rust-v<semver>")
    return tag.removeprefix("rust-v")


def _program_headers(binary: BinaryIO) -> tuple[str, int, int, int]:
    header = binary.read(64)
    if header[:4] != b"\x7fELF":
        raise LifecycleError("CLI asset 不是 ELF")
    if header[5] not in (1, 2):
        raise LifecycleError("ELF byte order 无效")
    endian = "<" if header[5] == 1 else ">"
    if header[4] == 2:
        offset = struct.unpack_from(endian + "Q", header, 32)[0]
        entry_size, count = struct.unpack_from(endian + "HH", header, 54)
    elif header[4] == 1:
        offset = struct.unpack_from(endian + "I", header, 28)[0]
        entry_size, count = struct.unpack_from(endian + "HH", header, 42)
    else:
        raise LifecycleError("ELF class 无效")
    return endian, offset, entry_size, count


def verify_static_elf(binary_path: Path, expected_version: str | None = None) -> None:
    with binary_path.open("rb") as binary:
        endian, offset, entry_size, count = _program_headers(binary)
        for index in range(count):
            binary.seek(offset + index * entry_size)
            raw_type = binary.read(4)
            if len(raw_type) != 4:
                raise LifecycleError("ELF program header 截断")
            if struct.unpack(endian + "I", raw_type)[0] == 3:
                raise LifecycleError("CLI ELF 含 PT_INTERP，不是 static executable")
    result = subprocess.run(
        [str(binary_path), "--version"], check=False, capture_output=True, text=True
    )
    reported_version = (result.stdout or "").strip()
    if result.returncode or "codex" not in reported_version.lower():
        raise LifecycleError("static CLI --version smoke 失败")
    if (
        expected_version is not None
        and reported_version != f"codex-cli {expected_version}"
    ):
        raise LifecycleError(
            "static CLI version 不匹配: "
            f"expected codex-cli {expected_version}, got {reported_version}"
        )


def _write_archive(binary: Path, archive: Path) -> None:
    with archive.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_output, mtime=0
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as bundle:
                info = tarfile.TarInfo("codex")
                info.size = binary.stat().st_size
                info.mode = 0o755
                info.mtime = 0
                with binary.open("rb") as source:
                    bundle.addfile(info, source)


def _nix_build(root: Path, version: str, cores: int = 0) -> tuple[str, Path]:
    if cores < 0:
        raise LifecycleError("Nix cores 必须是非负整数")
    with tempfile.TemporaryDirectory(prefix="co-build-version-") as temporary:
        version_input = Path(temporary)
        (version_input / "version").write_text(f"{version}\n", encoding="utf-8")
        git(version_input, "init", "--quiet", "--initial-branch=main")
        git(version_input, "config", "user.name", "co-build")
        git(version_input, "config", "user.email", "co-build@localhost")
        git(version_input, "add", "version")
        git(version_input, "commit", "--quiet", "-m", "set build version")
        result = run(
            [
                "nix",
                "build",
                git_flake(root, "codex"),
                "--override-input",
                "build-version",
                git_flake(version_input),
                "--no-link",
                "--print-out-paths",
                "--no-write-lock-file",
                "--max-jobs",
                "1",
                "--cores",
                str(cores),
            ],
            cwd=root,
        )
    outputs = tuple(line for line in (result.stdout or "").splitlines() if line)
    if len(outputs) != 1:
        raise LifecycleError(f"Nix build 应返回一个 store path，实际 {len(outputs)} 个")
    binary = Path(outputs[0]) / "bin/codex"
    verify_static_elf(binary, expected_version=version)
    return outputs[0], binary


def _emit_assets(
    root: Path,
    identity: dict[str, Any],
    store_path: str,
    binary: Path,
    version: str,
) -> Path:
    platform = _platform(root)
    build_dir = (
        root / ".states/co/build" / f"{timestamp()}-{identity['sourceRev'][:10]}"
    )
    build_dir.mkdir(parents=True, exist_ok=False)
    archive = build_dir / f"co-cli-{platform}-{identity['sourceRev'][:10]}.tar.gz"
    _write_archive(binary, archive)
    manifest_path = build_dir / f"co-manifest-{identity['sourceRev'][:10]}.json"
    manifest = {
        "schemaVersion": 1,
        "repository": "loiang/co",
        "upstreamRev": identity["upstreamRev"],
        "sourceRev": identity["sourceRev"],
        "sourceVersion": version,
        "platform": platform,
        "checksums": {"codex": sha256(binary), archive.name: sha256(archive)},
    }
    write_json(manifest_path, manifest)
    checksums = build_dir / "SHA256SUMS"
    checksums.write_text(
        f"{sha256(archive)}  {archive.name}\n"
        f"{sha256(manifest_path)}  {manifest_path.name}\n",
        encoding="utf-8",
    )
    record = {
        "schemaVersion": 1,
        **identity,
        "completedAt": timestamp(),
        "storePath": store_path,
        "manifestSha256": sha256(manifest_path),
        "assets": [
            str(path.relative_to(root)) for path in (archive, manifest_path, checksums)
        ],
    }
    latest = root / ".states/co/build/latest.json"
    write_json(latest, record)
    return latest


def build(repository: Path, cores: int = 0) -> Path:
    """Build only the static CLI with a per-invocation Nix core limit.

    Args:
        repository: Checkout whose source and lock identities bind the assets.
        cores: Cores exposed to each Nix build job; zero means all available.

    Returns:
        Path to the build evidence record and checksummed publication assets.
    """
    root = require_repo(repository)
    require_no_untracked(root)
    identity = source_identity(root)
    version = _official_version()
    store_path, binary = _nix_build(root, version, cores)
    return _emit_assets(root, identity, store_path, binary, version)
