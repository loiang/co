"""Published CLI asset identity and safe archive tests."""

import io
import json
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from common import LifecycleError, sha256
from native_fixtures import BINARY, make_assets
from release_asset import (
    Artifact,
    ReleaseBundle,
    _parse_checksums,
    fetch_release_bundle,
    resolve_release_tag,
    verified_cli,
)

SOURCE_REV = "8aaeb5e955881902ff624e1b049c4199afea079a"
UPSTREAM_REV = "c4017a87aacc7558002b7cb510025e967c1d765e"
TAG = "co-20260912T154829Z-8aaeb5e955"
CLI_NAME = "co-cli-x86_64-linux-8aaeb5e955.tar.gz"
MANIFEST_NAME = "co-manifest-8aaeb5e955.json"


def _write_archive(path: Path, name: str = "codex", kind: bytes | None = None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        if kind is None:
            member.size = len(BINARY)
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(BINARY))
        else:
            member.type = kind
            member.linkname = "target"
            archive.addfile(member)


def _artifact(path: Path) -> Artifact:
    return Artifact(
        path.name,
        f"https://release.invalid/{path.name}",
        path,
        sha256(path),
        path.stat().st_size,
    )


def _release_files(
    root: Path, *, manifest_source: str = SOURCE_REV, archive_sum: str | None = None
) -> dict[str, Artifact]:
    archive, manifest, sums = make_assets(root, manifest_source, UPSTREAM_REV)
    if manifest_source != SOURCE_REV:
        archive.rename(root / CLI_NAME)
        manifest.rename(root / MANIFEST_NAME)
        archive, manifest = root / CLI_NAME, root / MANIFEST_NAME
    if archive_sum is not None:
        sums.write_text(
            f"{archive_sum}  {CLI_NAME}\n{sha256(manifest)}  {MANIFEST_NAME}\n",
            encoding="utf-8",
        )
    return {path.name: _artifact(path) for path in (archive, manifest, sums)}


def sha256_bytes(value: bytes) -> str:
    """Hash compact fixture bytes without creating another test file."""
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _fetch(root: Path, files: dict[str, Artifact]) -> ReleaseBundle:
    def prefetch(_root: Path, _tag: str, name: str) -> Artifact:
        return files[name]

    with patch("release_asset._prefetch", side_effect=prefetch):
        return fetch_release_bundle(root, TAG, SOURCE_REV)


def test_fetch_cross_checks_release_evidence(tmp_path: Path) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))

    assert bundle.source_rev == SOURCE_REV
    assert bundle.cli.sha256 == bundle.evidence()["cli"]["sha256"]
    assert bundle.binary_sha256 == sha256_bytes(BINARY)
    assert bundle.binary_size == len(BINARY)


def test_manifest_source_mismatch_is_rejected(tmp_path: Path) -> None:
    files = _release_files(tmp_path)
    payload = json.loads(files[MANIFEST_NAME].path.read_text())
    payload["sourceRev"] = "a" * 40
    _rewrite_manifest(files, payload)

    with pytest.raises(LifecycleError, match="manifest identity"):
        _fetch(tmp_path, files)


def test_archive_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    files = _release_files(tmp_path, archive_sum="0" * 64)

    with pytest.raises(LifecycleError, match="CLI archive SHA-256"):
        _fetch(tmp_path, files)


def test_checksum_file_rejects_path_entries(tmp_path: Path) -> None:
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{'0' * 64}  ../codex\n", encoding="utf-8")

    with pytest.raises(LifecycleError, match="格式、路径或重复项"):
        _parse_checksums(sums, {"../codex"})


@pytest.mark.parametrize("name", ["../codex", "/codex", "nested/codex"])
def test_archive_rejects_unsafe_or_nested_paths(tmp_path: Path, name: str) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    _write_archive(bundle.cli.path, name)

    with pytest.raises(LifecycleError, match="archive SHA-256"), verified_cli(bundle):
        pass


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_archive_rejects_links(tmp_path: Path, kind: bytes) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    _write_archive(bundle.cli.path, kind=kind)

    with pytest.raises(LifecycleError, match="archive SHA-256"), verified_cli(bundle):
        pass


def test_archive_rejects_extra_member(tmp_path: Path) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    with tarfile.open(bundle.cli.path, "w:gz") as archive:
        for name in ("codex", "extra"):
            member = tarfile.TarInfo(name)
            member.size = len(BINARY)
            archive.addfile(member, io.BytesIO(BINARY))

    with pytest.raises(LifecycleError, match="archive SHA-256"), verified_cli(bundle):
        pass


def test_archive_rejects_binary_digest_mismatch(tmp_path: Path) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    mismatched = replace(bundle, binary_sha256="0" * 64)

    with (
        pytest.raises(LifecycleError, match="binary SHA-256"),
        verified_cli(mismatched),
    ):
        pass


def test_main_resolves_unique_peeled_release_tag(tmp_path: Path) -> None:
    output = f"tag-object\trefs/tags/{TAG}\n{SOURCE_REV}\trefs/tags/{TAG}^{{}}\n"

    with patch("release_asset.git", return_value=output):
        assert resolve_release_tag(tmp_path, "main", SOURCE_REV) == TAG


@pytest.mark.parametrize(
    ("output", "count"),
    [
        ("", 0),
        (
            (
                f"{SOURCE_REV}\trefs/tags/{TAG}^{{}}\n"
                f"{SOURCE_REV}\trefs/tags/co-20260912T160000Z-8aaeb5e955^{{}}\n"
            ),
            2,
        ),
    ],
)
def test_main_without_unique_release_tag_fails_closed(
    tmp_path: Path, output: str, count: int
) -> None:
    with (
        patch("release_asset.git", return_value=output),
        pytest.raises(LifecycleError, match=f"实际 {count}"),
    ):
        resolve_release_tag(tmp_path, "main", SOURCE_REV)


def _rewrite_manifest(files: dict[str, Artifact], payload: dict) -> None:
    manifest = files[MANIFEST_NAME].path
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    sums = files["SHA256SUMS"].path
    sums.write_text(
        f"{sha256(files[CLI_NAME].path)}  {CLI_NAME}\n"
        f"{sha256(manifest)}  {MANIFEST_NAME}\n",
        encoding="utf-8",
    )
    files[MANIFEST_NAME] = _artifact(manifest)
    files["SHA256SUMS"] = _artifact(sums)


def test_verified_cli_preserves_resources_and_cleans_directory(tmp_path: Path) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    with verified_cli(bundle) as binary:
        assert binary.read_bytes() == BINARY
        assert binary.name == "codex" and binary.parent.name == "bin"
        assert (binary.parent.parent / "codex-resources/bwrap").is_file()
        assert (binary.parent.parent / "codex-path/rg").is_file()
    assert not binary.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("entrypoint", "../codex"),
        ("target", "aarch64-unknown-linux-gnu"),
        ("variant", "codex-app-server"),
        ("layoutVersion", 2),
        ("version", "0.0.0"),
        ("resourcesDir", "elsewhere"),
    ],
)
def test_manifest_rejects_tampered_package_metadata(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    files = _release_files(tmp_path)
    payload = json.loads(files[MANIFEST_NAME].path.read_text())
    payload["package"][field] = value
    _rewrite_manifest(files, payload)
    with pytest.raises(LifecycleError, match="package metadata"):
        _fetch(tmp_path, files)


def test_archive_entrypoint_digest_is_checked_during_fetch(tmp_path: Path) -> None:
    files = _release_files(tmp_path)
    payload = json.loads(files[MANIFEST_NAME].path.read_text())
    payload["checksums"]["bin/codex"] = "0" * 64
    _rewrite_manifest(files, payload)
    with pytest.raises(LifecycleError, match="binary SHA-256"):
        _fetch(tmp_path, files)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escaped", tarfile.REGTYPE),
        ("/escaped", tarfile.REGTYPE),
        ("bin/../../escaped", tarfile.REGTYPE),
        ("bin\\escaped", tarfile.REGTYPE),
        ("bin/codex", tarfile.SYMTYPE),
        ("bin/codex", tarfile.LNKTYPE),
        ("bin/fifo", tarfile.FIFOTYPE),
        ("bin/device", tarfile.CHRTYPE),
        ("bin/codex", tarfile.REGTYPE),
        ("elsewhere", tarfile.REGTYPE),
    ],
)
def test_authenticated_archive_rejects_unsafe_members(
    tmp_path: Path,
    name: str,
    kind: bytes,
) -> None:
    files = _release_files(tmp_path)
    bundle = _fetch(tmp_path, files)
    with tarfile.open(bundle.cli.path, "r:gz") as archive:
        members = [
            (member, archive.extractfile(member).read() if member.isfile() else None)
            for member in archive.getmembers()
        ]
    with tarfile.open(bundle.cli.path, "w:gz") as archive:
        for member, data in members:
            archive.addfile(member, io.BytesIO(data) if data is not None else None)
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "../../escaped"
        archive.addfile(member)
    bundle = replace(bundle, cli=_artifact(bundle.cli.path))
    with (
        pytest.raises(LifecycleError, match="不安全路径、类型或重复项"),
        verified_cli(bundle),
    ):
        pytest.fail("unsafe archive reached host gate")
    assert not (tmp_path / "escaped").exists()


def test_archive_metadata_must_match_manifest(tmp_path: Path) -> None:
    files = _release_files(tmp_path)
    bundle = _fetch(tmp_path, files)
    package = tmp_path / "package"
    metadata = json.loads((package / "codex-package.json").read_text())
    metadata["version"] = "0.0.0"
    (package / "codex-package.json").write_text(json.dumps(metadata))
    with tarfile.open(bundle.cli.path, "w:gz") as archive:
        for path in sorted(package.rglob("*")):
            archive.add(path, arcname=path.relative_to(package), recursive=False)
    bundle = replace(bundle, cli=_artifact(bundle.cli.path))
    with pytest.raises(LifecycleError, match="package metadata"), verified_cli(bundle):
        pass


def test_prefetch_uses_exact_urls_without_nix_and_reuses_digest_cache(
    tmp_path: Path,
) -> None:
    files = _release_files(tmp_path)
    calls: list[str] = []

    def response(url: str, **_kwargs: object) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(files[url.rsplit("/", 1)[-1]].path.read_bytes())

    with patch("release_asset.urlopen", side_effect=response):
        first = fetch_release_bundle(tmp_path, TAG, SOURCE_REV)
        second = fetch_release_bundle(tmp_path, TAG, SOURCE_REV)
    expected = [
        f"https://github.com/loiang/co/releases/download/{TAG}/{name}"
        for name in ("SHA256SUMS", MANIFEST_NAME, CLI_NAME)
    ]
    assert calls == expected * 2
    assert first.cli.path == second.cli.path
    assert first.cli.path.is_relative_to(tmp_path / ".states/co/downloads")
    with verified_cli(second) as binary:
        assert binary.read_bytes() == BINARY


def test_incomplete_download_keeps_previous_verified_cache(tmp_path: Path) -> None:
    from release_asset import _prefetch

    with patch("release_asset.urlopen", return_value=io.BytesIO(b"old")):
        first = _prefetch(tmp_path, TAG, "SHA256SUMS")
    with (
        patch("release_asset.urlopen", side_effect=OSError("network")),
        pytest.raises(LifecycleError, match="下载失败"),
    ):
        _prefetch(tmp_path, TAG, "SHA256SUMS")
    assert first.path.read_bytes() == b"old"
    assert list(first.path.parent.iterdir()) == [first.path]


@pytest.mark.parametrize(
    "mutation", ["missing-resource", "non-executable", "oversized"]
)
def test_archive_enforces_official_resources_permissions_and_size(
    tmp_path: Path,
    mutation: str,
) -> None:
    files = _release_files(tmp_path)
    bundle = _fetch(tmp_path, files)
    package = tmp_path / "package"
    resource = package / "codex-resources/bwrap"
    if mutation == "missing-resource":
        resource.unlink()
    elif mutation == "non-executable":
        resource.chmod(0o644)
    with tarfile.open(bundle.cli.path, "w:gz") as archive:
        for path in sorted(package.rglob("*")):
            if mutation == "oversized" and path.name == "codex":
                member = archive.gettarinfo(path, arcname="bin/codex")
                member.size = 1024**3 + 1
                archive.fileobj.write(member.tobuf())
                break
            archive.add(path, arcname=path.relative_to(package), recursive=False)
    bundle = replace(bundle, cli=_artifact(bundle.cli.path))
    with pytest.raises(LifecycleError), verified_cli(bundle):
        pytest.fail("invalid package reached host gate")
