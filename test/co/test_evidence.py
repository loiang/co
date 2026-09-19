"""Build evidence permits local dirty builds while publication stays clean-only."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from unittest.mock import patch

import pytest
from common import LifecycleError, sha256, write_json
from evidence import (
    read_build_record,
    require_release_records,
    source_identity,
    verify_build_record,
)
from native_fixtures import METADATA, make_assets
from publication import publish
from test_publication import _git, _repository


def test_matching_dirty_build_record_is_valid_for_local_host_gate(
    tmp_path: Path,
) -> None:
    identity = {
        "schemaVersion": 2,
        "sourceRev": "a" * 40,
        "upstreamRev": "b" * 40,
        "flakeLockSha256": "c" * 64,
        "dirty": True,
    }
    record_path = tmp_path / ".states/co/build/latest.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(identity), encoding="utf-8")

    assert read_build_record(tmp_path, identity) == identity


def _records(tmp_path: Path) -> tuple[Path, dict]:
    root, _, _ = _repository(tmp_path)
    (root / ".co").mkdir()
    (root / ".co/upstream-rev").write_text("b" * 40)
    (root / "flake.lock").write_text("{}")
    _git(root, "add", ".co/upstream-rev", "flake.lock")
    _git(root, "commit", "-m", "identity")
    identity = source_identity(root)
    build = root / ".states/co/build/native"
    build.mkdir(parents=True)
    assets = make_assets(build, identity["sourceRev"], identity["upstreamRev"])
    record = {
        "schemaVersion": 2,
        **identity,
        "completedAt": "20260919T000000Z",
        "packageDir": str((build / "package").relative_to(root)),
        "package": METADATA,
        "manifestSha256": sha256(assets[1]),
        "assets": [str(path.relative_to(root)) for path in assets],
    }
    write_json(root / ".states/co/build/latest.json", record)
    write_json(root / ".states/co/test/latest.json", identity)
    host = root / ".states/host/bin/codex-code-mode-host"
    host.parent.mkdir(parents=True)
    host.write_text("official-host")
    write_json(
        root / ".states/co/host-integration/latest.json",
        {
            **identity,
            "officialHostStorePath": str(host.parent.parent),
            "officialHostSha256": sha256(host),
            "manifestSha256": record["manifestSha256"],
        },
    )
    return root, record


def test_native_records_publish_dry_run_without_mutations(tmp_path: Path) -> None:
    root, record = _records(tmp_path)
    assert len(require_release_records(root)) == 3
    with (
        patch("publication._require_origin"),
        patch("publication.ensure_release") as release,
    ):
        tag = publish(root, dry_run=True)
    assert tag.endswith(record["sourceRev"][:10])
    assert release.call_args.args[2] == tuple(root / path for path in record["assets"])
    assert release.call_args.args[3] is True
    assert not (root / ".states/co/publish").exists()
    assert _git(root, "tag", "--list") == ""


@pytest.mark.parametrize(
    "relative", ["bin/codex", "codex-resources/bwrap", "codex-path/rg"]
)
def test_modified_local_package_cannot_reuse_build_evidence(
    tmp_path: Path, relative: str
) -> None:
    root, record = _records(tmp_path)
    (root / record["packageDir"] / relative).write_text("tampered")
    with pytest.raises(LifecycleError, match="checksum"):
        verify_build_record(root, record)


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside"])
def test_package_directory_must_be_root_relative(tmp_path: Path, path: str) -> None:
    root, record = _records(tmp_path)
    record["packageDir"] = path
    with pytest.raises(LifecycleError, match="packageDir"):
        verify_build_record(root, record)


def test_host_evidence_must_bind_same_manifest(tmp_path: Path) -> None:
    root, _ = _records(tmp_path)
    path = root / ".states/co/host-integration/latest.json"
    record = json.loads(path.read_text())
    record["manifestSha256"] = "0" * 64
    write_json(path, record)
    with pytest.raises(LifecycleError, match="host integration evidence"):
        require_release_records(root)


def test_publication_still_requires_clean_source(tmp_path: Path) -> None:
    root, _ = _records(tmp_path)
    (root / "tracked").write_text("dirty")
    with pytest.raises(LifecycleError, match="clean"):
        require_release_records(root)


@pytest.mark.parametrize(
    "field", ["sourceRev", "upstreamRev", "flakeLockSha256", "dirty"]
)
def test_release_rejects_stale_source_or_lock_identity(
    tmp_path: Path, field: str
) -> None:
    root, record = _records(tmp_path)
    record[field] = True if field == "dirty" else "0" * len(record[field])
    write_json(root / ".states/co/build/latest.json", record)
    with pytest.raises(LifecycleError, match=field):
        require_release_records(root)


def test_host_gate_validates_native_package_and_binds_manifest(tmp_path: Path) -> None:
    import host_integration

    root, record = _records(tmp_path)
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
        root / record["packageDir"] / "bin/codex",
        host_store / "bin/codex-code-mode-host",
    )
    assert json.loads(result.read_text())["manifestSha256"] == record["manifestSha256"]
