"""Standalone release consumption preserves provenance and safe extraction gates."""

import io
import json
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from common import LifecycleError, sha256, write_json  # noqa: E402
from evidence import verify_build_record  # noqa: E402
from native_fixtures import BINARY  # noqa: E402
from release_asset import Artifact, fetch_release_bundle, verified_cli  # noqa: E402
from standalone_fixtures import DESCRIPTOR, VERSION, make_assets  # noqa: E402

SOURCE = "8aaeb5e955881902ff624e1b049c4199afea079a"
TAG = "co-20260912T154829Z-8aaeb5e955"


def _record(root: Path) -> dict:
    assets = make_assets(root, SOURCE)
    return {
        "schemaVersion": 3,
        "sourceRev": SOURCE,
        "sourceVersion": VERSION,
        "platform": "x86_64-linux",
        "artifact": DESCRIPTOR,
        "artifactDir": "artifact",
        "manifestSha256": sha256(assets[1]),
        "assets": [path.name for path in assets],
    }


def test_standalone_local_evidence_accepts_only_verified_binary(tmp_path: Path) -> None:
    record = _record(tmp_path)
    assert verify_build_record(tmp_path, record) == tmp_path / "artifact"
    (tmp_path / "artifact/codex").write_bytes(b"tampered")
    with pytest.raises(LifecycleError, match="checksum"):
        verify_build_record(tmp_path, record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifactDir", "../artifact"),
        ("artifactDir", "/tmp/artifact"),
        ("sourceVersion", "0.1.0"),
        ("platform", "aarch64-linux"),
        ("artifact", {**DESCRIPTOR, "target": "aarch64-unknown-linux-gnu"}),
    ],
)
def test_standalone_local_record_identity_mismatch_fails(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    record = _record(tmp_path)
    record[field] = value
    with pytest.raises(LifecycleError):
        verify_build_record(tmp_path, record)


def test_standalone_release_fetch_exposes_root_entrypoint(tmp_path: Path) -> None:
    paths = make_assets(tmp_path, SOURCE)
    files = {
        p.name: Artifact(
            p.name, f"https://release.invalid/{p.name}", p, sha256(p), p.stat().st_size
        )
        for p in paths
    }
    with patch("release_asset._prefetch", side_effect=lambda _r, _t, n: files[n]):
        bundle = fetch_release_bundle(tmp_path, TAG, SOURCE)
    assert bundle.package is None
    assert bundle.artifact == DESCRIPTOR
    assert "package" not in bundle.evidence()
    assert bundle.evidence()["cli"]["binaryPath"] == "codex"
    with verified_cli(bundle) as binary:
        assert binary.read_bytes() == BINARY
        assert binary.name == "codex"


def test_standalone_local_executable_mode_must_match_archive(tmp_path: Path) -> None:
    record = _record(tmp_path)
    (tmp_path / "artifact/codex").chmod(0o700)
    with pytest.raises(LifecycleError, match="mode"):
        verify_build_record(tmp_path, record)


def test_standalone_archive_size_is_bounded_even_with_valid_checksums(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    with (
        patch("standalone_artifact.MAX_ARCHIVE_BYTES", 1),
        pytest.raises(LifecycleError, match="大小限制"),
    ):
        verify_build_record(tmp_path, record)


def test_standalone_manifest_rejects_invalid_version_even_when_resigned(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    archive, manifest, sums = [tmp_path / name for name in record["assets"]]
    payload = json.loads(manifest.read_text())
    payload["sourceVersion"] = payload["artifact"]["version"] = "not-semver"
    manifest.write_text(json.dumps(payload))
    sums.write_text(
        f"{sha256(archive)}  {archive.name}\n{sha256(manifest)}  {manifest.name}\n"
    )
    record["sourceVersion"] = "not-semver"
    record["artifact"] = {**DESCRIPTOR, "version": "not-semver"}
    record["manifestSha256"] = sha256(manifest)
    with pytest.raises(LifecycleError, match="version"):
        verify_build_record(tmp_path, record)


def _replace_archive(
    root: Path, record: dict, names: list[str], kind: bytes = tarfile.REGTYPE
) -> None:
    archive, manifest, sums = [root / name for name in record["assets"]]
    with tarfile.open(archive, "w:gz") as output:
        for name in names:
            member = tarfile.TarInfo(name)
            member.mode = 0o755
            member.type = kind
            member.linkname = "codex"
            member.size = len(BINARY) if member.isreg() else 0
            output.addfile(member, io.BytesIO(BINARY) if member.isreg() else None)
    payload = json.loads(manifest.read_text())
    payload["checksums"][archive.name] = sha256(archive)
    manifest.write_text(json.dumps(payload))
    sums.write_text(
        f"{sha256(archive)}  {archive.name}\n{sha256(manifest)}  {manifest.name}\n"
    )
    record["manifestSha256"] = sha256(manifest)


@pytest.mark.parametrize(
    "names,kind",
    [
        (["codex", "rg"], tarfile.REGTYPE),
        (["../codex"], tarfile.REGTYPE),
        (["/codex"], tarfile.REGTYPE),
        (["nested/codex"], tarfile.REGTYPE),
        (["codex"], tarfile.SYMTYPE),
        (["codex"], tarfile.LNKTYPE),
    ],
)
def test_standalone_rejects_resigned_unsafe_archive(
    tmp_path: Path,
    names: list[str],
    kind: bytes,
) -> None:
    record = _record(tmp_path)
    _replace_archive(tmp_path, record, names, kind)
    with pytest.raises(LifecycleError, match="archive"):
        verify_build_record(tmp_path, record)


@pytest.mark.parametrize("mutation", ["extra", "symlink", "nonexecutable"])
def test_standalone_local_directory_rejects_unsafe_output(
    tmp_path: Path, mutation: str
) -> None:
    record = _record(tmp_path)
    binary = tmp_path / "artifact/codex"
    if mutation == "extra":
        (binary.parent / "rg").write_bytes(BINARY)
    elif mutation == "symlink":
        binary.unlink()
        binary.symlink_to(tmp_path / "other")
    else:
        binary.chmod(0o644)
    with pytest.raises(LifecycleError):
        verify_build_record(tmp_path, record)


def _repository_record(tmp_path: Path) -> tuple[Path, dict]:
    from test_evidence import _records

    root, previous = _records(tmp_path)
    directory = root / ".states/co/build/standalone"
    directory.mkdir()
    assets = make_assets(directory, previous["sourceRev"])
    record = {
        **previous,
        "schemaVersion": 3,
        "sourceVersion": VERSION,
        "platform": "x86_64-linux",
        "artifact": DESCRIPTOR,
        "artifactDir": str((directory / "artifact").relative_to(root)),
        "manifestSha256": sha256(assets[1]),
        "assets": [str(p.relative_to(root)) for p in assets],
    }
    del record["package"]
    del record["packageDir"]
    write_json(root / ".states/co/build/latest.json", record)
    return root, record


def test_standalone_publication_keeps_three_pinned_assets(tmp_path: Path) -> None:
    from publication import publish

    root, record = _repository_record(tmp_path)
    with (
        patch("publication._require_origin"),
        patch("publication.ensure_release") as release,
    ):
        tag = publish(root, dry_run=True)
    assert tag.endswith(record["sourceRev"][:10])
    assert release.call_args.args[2] == tuple(root / p for p in record["assets"])
    assert not (root / ".states/co/publish").exists()


def test_standalone_host_gate_uses_root_binary_and_independent_host(
    tmp_path: Path,
) -> None:
    import host_integration

    root, record = _repository_record(tmp_path)
    host_store = root / ".states/host"
    with (
        patch("host_integration.build_official_host", return_value=str(host_store)),
        patch("host_integration.official_host_lock", return_value={"narHash": "fixed"}),
        patch(
            "host_integration.test_candidate_binaries", return_value=["host-gate"]
        ) as gate,
    ):
        result = host_integration.test_host_integration(root, root)
    gate.assert_called_once_with(
        root,
        root / record["artifactDir"] / "codex",
        host_store / "bin/codex-code-mode-host",
    )
    assert json.loads(result.read_text())["manifestSha256"] == record["manifestSha256"]
