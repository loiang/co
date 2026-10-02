"""Bind standalone build evidence to the exact local executable and release assets."""

from pathlib import Path
from typing import Any

from common import LifecycleError, sha256
from release_asset import _load_manifest, _manifest_identity, _parse_checksums
from standalone_artifact import (
    artifact_metadata,
    validate_cli_directory,
    verified_cli_archive,
)


def verify_standalone_record(
    root: Path, record: dict[str, Any], assets: tuple[Path, ...]
) -> Path:
    """Verify source-bound single-binary evidence before publication or host checks."""
    raw = record.get("artifactDir")
    if (
        not isinstance(raw, str)
        or not raw
        or Path(raw).is_absolute()
        or ".." in Path(raw).parts
    ):
        raise LifecycleError("build record artifactDir 必须是 root-relative path")
    directory = root / raw
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or not directory.resolve().is_relative_to(root.resolve())
    ):
        raise LifecycleError("build record artifactDir 缺失或越界")
    manifest, archive, sums = _asset_identity(record, assets)
    payload = _load_manifest(manifest)
    descriptor = artifact_metadata(payload)
    for field in ("sourceRev", "sourceVersion", "platform", "artifact"):
        if record.get(field) != payload.get(field):
            raise LifecycleError(f"build record 与 manifest 不一致: {field}")
    _, binary_sha = _manifest_identity(
        payload, record["sourceRev"], archive.name, record["platform"]
    )
    if payload["checksums"][archive.name] != sha256(archive):
        raise LifecycleError("build archive checksum 不一致")
    expected = {path.name: sha256(path) for path in (archive, manifest)}
    if _parse_checksums(sums, set(expected)) != expected:
        raise LifecycleError("SHA256SUMS 与 release assets 不一致")
    validate_cli_directory(directory, descriptor)
    with verified_cli_archive(archive, descriptor, binary_sha) as extracted:
        local = directory / descriptor["entrypoint"]
        verified = extracted / descriptor["entrypoint"]
        if sha256(local) != sha256(verified):
            raise LifecycleError("local CLI checksum 与 archive 不一致")
        if local.stat().st_mode & 0o777 != verified.stat().st_mode & 0o777:
            raise LifecycleError("local CLI mode 与 archive 不一致")
    return directory


def _asset_identity(
    record: dict[str, Any], assets: tuple[Path, ...]
) -> tuple[Path, Path, Path]:
    manifests = tuple(p for p in assets if p.name.startswith("co-manifest-"))
    archives = tuple(p for p in assets if p.name.endswith(".tar.gz"))
    sums = tuple(p for p in assets if p.name == "SHA256SUMS")
    if len(manifests) != 1 or len(archives) != 1 or len(sums) != 1:
        raise LifecycleError("build assets 缺少唯一 archive、manifest 或 SHA256SUMS")
    source = record.get("sourceRev")
    if not isinstance(source, str) or len(source) != 40:
        raise LifecycleError("build record sourceRev 无效")
    if (
        manifests[0].name != f"co-manifest-{source[:10]}.json"
        or archives[0].name != f"co-cli-{record.get('platform')}-{source[:10]}.tar.gz"
    ):
        raise LifecycleError("build assets 文件名与 source/platform 不一致")
    if sha256(manifests[0]) != record.get("manifestSha256"):
        raise LifecycleError("build manifest identity 或 checksum 不一致")
    return manifests[0], archives[0], sums[0]
