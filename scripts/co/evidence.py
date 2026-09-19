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
from package_verification import compare_package, package_metadata, verified_package
from release_asset import _load_manifest, _manifest_identity, _parse_checksums


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
    record = _require_record_identity(root, ".states/co/build/latest.json", identity)
    if record.get("schemaVersion") != 2:
        raise LifecycleError("build record 必须使用 native package schemaVersion=2")
    return record


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
        if not isinstance(raw_path, str) or Path(raw_path).is_absolute():
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
    directory: Path,
) -> None:
    manifests = tuple(path for path in assets if path.name.startswith("co-manifest-"))
    archives = tuple(path for path in assets if path.name.endswith(".tar.gz"))
    sums = tuple(path for path in assets if path.name == "SHA256SUMS")
    if len(archives) != 1 or len(sums) != 1:
        raise LifecycleError("build assets 缺少唯一 archive 或 SHA256SUMS")
    if len(manifests) != 1 or sha256(manifests[0]) != record.get("manifestSha256"):
        raise LifecycleError("build manifest identity 或 checksum 不一致")
    manifest = _load_manifest(manifests[0])
    for field in ("sourceRev", "upstreamRev"):
        if manifest.get(field) != identity[field]:
            raise LifecycleError(f"build manifest 与当前源码不一致: {field}")
    metadata = package_metadata(manifest)
    if record.get("package") != metadata:
        raise LifecycleError("build record package metadata 与 manifest 不一致")
    expected_archive = (
        f"co-cli-{manifest['platform']}-{identity['sourceRev'][:10]}.tar.gz"
    )
    if archives[0].name != expected_archive or manifests[0].name != (
        f"co-manifest-{identity['sourceRev'][:10]}.json"
    ):
        raise LifecycleError("build assets 文件名与 source/platform 不一致")
    _, _, binary_sha = _manifest_identity(
        manifest, identity["sourceRev"], archives[0].name, manifest["platform"]
    )
    if manifest["checksums"][archives[0].name] != sha256(archives[0]):
        raise LifecycleError("build archive checksum 不一致")
    expected = {path.name: sha256(path) for path in (archives[0], manifests[0])}
    if _parse_checksums(sums[0], set(expected)) != expected:
        raise LifecycleError("SHA256SUMS 与 release assets 不一致")
    with verified_package(archives[0], metadata, binary_sha) as extracted:
        compare_package(directory, extracted)


def verify_build_record(root: Path, record: dict[str, Any]) -> Path:
    """Bind a v2 local package to its complete archive before host tests or publish."""
    raw = record.get("packageDir")
    if (
        record.get("schemaVersion") != 2
        or not isinstance(raw, str)
        or Path(raw).is_absolute()
        or ".." in Path(raw).parts
    ):
        raise LifecycleError("build record packageDir 或 schemaVersion 无效")
    directory = root / raw
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or not directory.resolve().is_relative_to(root.resolve())
    ):
        raise LifecycleError("build record packageDir 缺失或越界")
    _verify_manifest(record, record, _asset_paths(root, record), directory)
    return directory


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
    if host_record.get("manifestSha256") != build_record.get("manifestSha256"):
        raise LifecycleError(
            "host integration evidence 与 native build manifest 不一致"
        )
    host_binary = Path(str(host_record.get("officialHostStorePath", ""))) / (
        "bin/codex-code-mode-host"
    )
    if not host_binary.is_file() or sha256(host_binary) != host_record.get(
        "officialHostSha256"
    ):
        raise LifecycleError("official host artifact checksum 不一致")
    assets = _asset_paths(root, build_record)
    verify_build_record(root, build_record)
    return assets
