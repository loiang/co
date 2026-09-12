"""Published CLI asset identity and safe archive tests."""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from common import LifecycleError, sha256  # noqa: E402
from release_asset import (  # noqa: E402
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
BINARY = b"verified-codex-binary"


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
    archive = root / CLI_NAME
    manifest = root / MANIFEST_NAME
    sums = root / "SHA256SUMS"
    _write_archive(archive)
    archive_sha = sha256(archive)
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "repository": "loiang/co",
                "upstreamRev": UPSTREAM_REV,
                "sourceRev": manifest_source,
                "sourceVersion": "0.0.0",
                "platform": "x86_64-linux",
                "checksums": {
                    "codex": sha256_bytes(BINARY),
                    CLI_NAME: archive_sha,
                },
            }
        ),
        encoding="utf-8",
    )
    sums.write_text(
        f"{archive_sum or archive_sha}  {CLI_NAME}\n"
        f"{sha256(manifest)}  {MANIFEST_NAME}\n",
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
    files = _release_files(tmp_path, manifest_source="a" * 40)

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

    with pytest.raises(LifecycleError, match="单一 regular codex"):
        with verified_cli(bundle):
            pass


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_archive_rejects_links(tmp_path: Path, kind: bytes) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    _write_archive(bundle.cli.path, kind=kind)

    with pytest.raises(LifecycleError, match="单一 regular codex"):
        with verified_cli(bundle):
            pass


def test_archive_rejects_extra_member(tmp_path: Path) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    with tarfile.open(bundle.cli.path, "w:gz") as archive:
        for name in ("codex", "extra"):
            member = tarfile.TarInfo(name)
            member.size = len(BINARY)
            archive.addfile(member, io.BytesIO(BINARY))

    with pytest.raises(LifecycleError, match="单一 regular codex"):
        with verified_cli(bundle):
            pass


def test_archive_rejects_binary_digest_mismatch(tmp_path: Path) -> None:
    bundle = _fetch(tmp_path, _release_files(tmp_path))
    mismatched = ReleaseBundle(
        bundle.tag,
        bundle.source_rev,
        bundle.upstream_rev,
        bundle.source_version,
        bundle.platform,
        bundle.cli,
        bundle.manifest,
        bundle.checksums,
        "0" * 64,
        bundle.binary_size,
    )

    with pytest.raises(LifecycleError, match="binary SHA-256"):
        with verified_cli(mismatched):
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
            f"{SOURCE_REV}\trefs/tags/{TAG}^{{}}\n"
            f"{SOURCE_REV}\trefs/tags/co-20260912T160000Z-8aaeb5e955^{{}}\n",
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


def test_prefetch_uses_exact_release_urls(tmp_path: Path) -> None:
    files = _release_files(tmp_path)
    calls: list[list[str]] = []

    def capture(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        name = command[-1].rsplit("/", 1)[-1]
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"storePath": str(files[name].path)}), ""
        )

    with patch("release_asset.run", side_effect=capture):
        fetch_release_bundle(tmp_path, TAG, SOURCE_REV)

    assert [command[-1] for command in calls] == [
        f"https://github.com/loiang/co/releases/download/{TAG}/SHA256SUMS",
        f"https://github.com/loiang/co/releases/download/{TAG}/{MANIFEST_NAME}",
        f"https://github.com/loiang/co/releases/download/{TAG}/{CLI_NAME}",
    ]
