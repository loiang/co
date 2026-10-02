"""Fetch and validate the upstream static Linux bubblewrap release asset."""

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from common import LifecycleError, sha256
from package_verification import extract_single_executable
from upstream_version import version_key

RELEASE_API = "repos/openai/codex/releases/tags"
_BROWSER_DOWNLOAD_PREFIX = "https://github.com/openai/codex/releases/download/"
_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
_ASSET_URL_PATTERN = re.compile(
    r"https://api\.github\.com/repos/openai/codex/releases/assets/[1-9][0-9]*"
)
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
    tag = _release_tag(version)
    architecture = _target_architecture(target)
    name = f"bwrap-{architecture}-unknown-linux-musl.tar.gz"
    metadata = _fetch_release_metadata(tag)
    if metadata.get("tag_name") != tag:
        raise LifecycleError("bwrap release API 返回了错误的 tag")
    expected_prerelease = "-" in version.split("+", 1)[0]
    if (
        metadata.get("draft") is not False
        or metadata.get("prerelease") is not expected_prerelease
    ):
        raise LifecycleError("bwrap release draft/prerelease 与精确版本不一致")
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
    api_url = asset.get("url")
    browser_url = asset.get("browser_download_url")
    size = asset.get("size")
    match = _DIGEST_PATTERN.fullmatch(str(digest))
    if (
        match is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size > _MAX_ARCHIVE_BYTES
        or not isinstance(api_url, str)
        or _ASSET_URL_PATTERN.fullmatch(api_url) is None
        or not isinstance(browser_url, str)
        or browser_url != f"{_BROWSER_DOWNLOAD_PREFIX}{tag}/{name}"
    ):
        raise LifecycleError(f"bwrap asset metadata 不可信: {name}")
    return BwrapAsset(version, tag, architecture, name, api_url, size, match.group(1))


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
    expected_name = asset.name.removesuffix(".tar.gz")
    extract_single_executable(archive, binary, expected_name)
    return binary


def _target_architecture(target: str) -> str:
    fields = target.split("-")
    architecture = fields[0] if fields else ""
    if architecture not in _ARCHITECTURES or "linux" not in fields:
        raise LifecycleError(f"bwrap 只支持 Linux x86_64/aarch64 target: {target}")
    return architecture


def _release_tag(version: str) -> str:
    tag = f"rust-v{version}"
    if version_key(version) is None:
        raise LifecycleError(f"官方 Codex version 无效: {version}")
    return tag


def _fetch_release_metadata(tag: str) -> dict[str, object]:
    raw = _gh_api(f"{RELEASE_API}/{tag}", "application/vnd.github+json")
    if len(raw) > _MAX_METADATA_BYTES:
        raise LifecycleError("bwrap release metadata 超过 1 MiB")
    try:
        metadata = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LifecycleError("bwrap release metadata 不是有效 JSON") from error
    if not isinstance(metadata, dict):
        raise LifecycleError("bwrap release metadata 必须是 JSON object")
    return metadata


def _gh_api(endpoint: str, accept: str) -> bytes:
    """Run one authenticated GitHub API request without exposing credentials."""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                endpoint,
                "--header",
                f"Accept: {accept}",
                "--header",
                "X-GitHub-Api-Version: 2022-11-28",
            ],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise LifecycleError("gh CLI 不可用；请安装 GitHub CLI") from error
    except OSError as error:
        raise LifecycleError("gh CLI 执行失败") from error
    if result.returncode:
        raise LifecycleError(f"gh CLI 请求失败 (exit {result.returncode})")
    raw = result.stdout
    if not isinstance(raw, bytes):
        raise LifecycleError("gh CLI 返回非 binary payload")
    return raw


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
            raw = _gh_api(asset.url, "application/octet-stream")
            size = len(raw)
            if size > asset.size or size > _MAX_ARCHIVE_BYTES:
                raise LifecycleError("bwrap release asset 超过声明大小")
            output.write(raw)
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
