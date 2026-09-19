"""Fetch and validate the upstream static Linux bubblewrap release asset."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from common import LifecycleError, sha256
from package_verification import extract_single_executable

RELEASE_API = "https://api.github.com/repos/openai/codex/releases/tags"
_TAG_PATTERN = re.compile(r"rust-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
_ARCHITECTURES = {"x86_64", "aarch64"}
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class BwrapAsset:
    """Bind one exact GitHub asset to the upstream release and target arch."""

    version: str
    tag: str
    architecture: str
    name: str
    url: str
    size: int
    digest: str


def resolve_bwrap_asset(version: str, target: str) -> BwrapAsset:
    """Resolve the exact static bwrap asset for a Codex package target.

    The release API is queried by exact ``rust-v<version>`` tag so bwrap and
    Codex cannot silently come from different upstream releases. GitHub's
    signed asset digest is the trust boundary for the downloaded archive.
    """
    tag = f"rust-v{version}"
    if _TAG_PATTERN.fullmatch(tag) is None:
        raise LifecycleError(f"官方 Codex version 无效: {version}")
    architecture = _target_architecture(target)
    name = f"bwrap-{architecture}-unknown-linux-musl.tar.gz"
    metadata = _fetch_release_metadata(tag)
    if metadata.get("tag_name") != tag:
        raise LifecycleError("bwrap release API 返回了错误的 tag")
    if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        raise LifecycleError("bwrap release 必须是 stable release")
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise LifecycleError("bwrap release 缺少 assets 列表")
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name") == name
    ]
    if len(matches) != 1:
        raise LifecycleError(f"bwrap release 必须唯一提供 asset: {name}")
    asset = matches[0]
    digest = asset.get("digest")
    url = asset.get("browser_download_url")
    size = asset.get("size")
    match = _DIGEST_PATTERN.fullmatch(str(digest))
    if (
        match is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size > _MAX_ARCHIVE_BYTES
        or not isinstance(url, str)
        or not url.startswith("https://github.com/openai/codex/releases/download/")
    ):
        raise LifecycleError(f"bwrap asset metadata 不可信: {name}")
    return BwrapAsset(version, tag, architecture, name, url, size, match.group(1))


def resolve_bwrap_binary(
    version: str, target: str, *, cache_root: Path | None = None
) -> Path:
    """Fetch, validate, and cache the static bwrap binary for one target."""
    asset = resolve_bwrap_asset(version, target)
    return fetch_bwrap_binary(asset, cache_root=cache_root)


def fetch_bwrap_binary(asset: BwrapAsset, *, cache_root: Path | None = None) -> Path:
    """Materialize an asset under a versioned, content-addressed cache."""
    root = cache_root or Path(tempfile.gettempdir()) / "codex-package" / "bwrap"
    cache_dir = root / asset.tag / asset.architecture / asset.digest
    archive = cache_dir / asset.name
    binary = cache_dir / "bwrap"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not _valid_cached_archive(archive, asset):
        _download_archive(asset, archive)
    # Always derive the executable again from the verified archive. A cached
    # executable can be modified independently while retaining its mode/size;
    # re-extraction keeps the archive digest as the sole trust boundary.
    extract_single_executable(archive, binary, "bwrap")
    return binary


def _target_architecture(target: str) -> str:
    fields = target.split("-")
    architecture = fields[0] if fields else ""
    if architecture not in _ARCHITECTURES or "linux" not in fields:
        raise LifecycleError(f"bwrap 只支持 Linux x86_64/aarch64 target: {target}")
    return architecture


def _fetch_release_metadata(tag: str) -> dict[str, object]:
    request = Request(
        f"{RELEASE_API}/{tag}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "loiang-co-build",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(_MAX_METADATA_BYTES + 1)
    except OSError as error:
        raise LifecycleError(f"无法读取官方 bwrap release: {tag}") from error
    if len(raw) > _MAX_METADATA_BYTES:
        raise LifecycleError("bwrap release metadata 超过 1 MiB")
    try:
        metadata = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LifecycleError("bwrap release metadata 不是有效 JSON") from error
    if not isinstance(metadata, dict):
        raise LifecycleError("bwrap release metadata 必须是 JSON object")
    return metadata


def _valid_cached_archive(path: Path, asset: BwrapAsset) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size != asset.size or sha256(path) != asset.digest:
            path.unlink(missing_ok=True)
            return False
    except OSError as error:
        raise LifecycleError(f"无法校验 bwrap cache: {path}") from error
    return True


def _download_archive(asset: BwrapAsset, destination: Path) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            with urlopen(asset.url, timeout=60) as response:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > asset.size or size > _MAX_ARCHIVE_BYTES:
                        raise LifecycleError("bwrap release asset 超过声明大小")
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if size != asset.size or sha256(temporary) != asset.digest:
            raise LifecycleError("bwrap release asset 的 size/digest 校验失败")
        temporary.replace(destination)
    except OSError as error:
        raise LifecycleError(f"bwrap release asset 下载失败: {asset.name}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
