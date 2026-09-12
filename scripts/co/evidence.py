"""Validate source identity and provenance-bound release evidence."""

from pathlib import Path
from typing import Any

from common import (
    LifecycleError,
    head,
    read_json,
    require_repo,
    sha256,
    working_dirty,
)


def source_identity(root: Path) -> dict[str, Any]:
    """Return the commit, upstream baseline, lock, and dirtiness identity."""
    try:
        upstream_rev = (root / ".co/upstream-rev").read_text(encoding="utf-8").strip()
    except OSError as error:
        raise LifecycleError("缺少 .co/upstream-rev") from error
    return {
        "sourceRev": head(root),
        "upstreamRev": upstream_rev,
        "flakeLockSha256": sha256(root / "flake.lock"),
        "dirty": working_dirty(root),
    }


def read_build_record(root: Path, identity: dict[str, Any]) -> dict[str, Any]:
    """Require a build record bound to the current source, lock, and dirtiness."""
    return _require_record_identity(root, ".states/co/build/latest.json", identity)


def _require_record_identity(
    root: Path, relative: str, identity: dict[str, Any]
) -> dict[str, Any]:
    record = read_json(root / relative)
    for field in ("sourceRev", "upstreamRev", "flakeLockSha256", "dirty"):
        if record.get(field) != identity[field]:
            raise LifecycleError(f"{relative} 与当前源码不一致: {field}")
    return record


def _asset_paths(root: Path, build_record: dict[str, Any]) -> tuple[Path, ...]:
    raw_assets = build_record.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) != 3:
        raise LifecycleError(
            "build record assets 必须包含 archive、manifest、checksums"
        )
    assets: list[Path] = []
    for raw_path in raw_assets:
        if not isinstance(raw_path, str):
            raise LifecycleError("build record asset path 无效")
        path = (root / raw_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise LifecycleError(f"build record asset 缺失或越界: {raw_path}")
        assets.append(path)
    return tuple(assets)


def _verify_manifest(
    identity: dict[str, Any],
    record: dict[str, Any],
    assets: tuple[Path, ...],
) -> None:
    manifests = tuple(path for path in assets if path.name.startswith("co-manifest-"))
    archives = tuple(path for path in assets if path.name.endswith(".tar.gz"))
    sums = tuple(path for path in assets if path.name == "SHA256SUMS")
    if len(archives) != 1 or len(sums) != 1:
        raise LifecycleError("build assets 缺少唯一 archive 或 SHA256SUMS")
    if len(manifests) != 1 or sha256(manifests[0]) != record.get("manifestSha256"):
        raise LifecycleError("build manifest identity 或 checksum 不一致")
    manifest = read_json(manifests[0])
    for field in ("sourceRev", "upstreamRev"):
        if manifest.get(field) != identity[field]:
            raise LifecycleError(f"build manifest 与当前源码不一致: {field}")
    binary = Path(str(record.get("storePath", ""))) / "bin/codex"
    checksums = manifest.get("checksums")
    if not binary.is_file() or not isinstance(checksums, dict):
        raise LifecycleError("build store binary 或 manifest checksums 缺失")
    if checksums.get("codex") != sha256(binary):
        raise LifecycleError("build store binary checksum 不一致")
    if checksums.get(archives[0].name) != sha256(archives[0]):
        raise LifecycleError("build archive checksum 不一致")
    listed = {}
    for line in sums[0].read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in listed:
            raise LifecycleError("SHA256SUMS 格式无效或含重复文件")
        listed[name] = digest
    expected = {
        archives[0].name: sha256(archives[0]),
        manifests[0].name: sha256(manifests[0]),
    }
    if listed != expected:
        raise LifecycleError("SHA256SUMS 与 release assets 不一致")


def require_release_records(repository: Path) -> tuple[Path, ...]:
    """Validate clean source-bound test, build, and host integration evidence."""
    root = require_repo(repository)
    identity = source_identity(root)
    if identity["dirty"]:
        raise LifecycleError("发布要求 clean candidate worktree")
    _require_record_identity(root, ".states/co/test/latest.json", identity)
    build_record = read_build_record(root, identity)
    host_record = _require_record_identity(
        root, ".states/co/host-integration/latest.json", identity
    )
    host_binary = Path(str(host_record.get("officialHostStorePath", ""))) / (
        "bin/codex-code-mode-host"
    )
    if not host_binary.is_file() or sha256(host_binary) != host_record.get(
        "officialHostSha256"
    ):
        raise LifecycleError("official host artifact checksum 不一致")
    assets = _asset_paths(root, build_record)
    _verify_manifest(identity, build_record, assets)
    return assets
