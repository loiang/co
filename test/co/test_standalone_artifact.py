"""Validate standalone CLI identity and reject unsafe single-binary archives."""

import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/co"))
from common import LifecycleError, sha256  # noqa: E402
from standalone_artifact import (
    artifact_metadata,
    validate_cli_directory,
    verified_cli_archive,
)  # noqa: E402


def payload() -> dict:
    return {
        "schemaVersion": 3,
        "sourceVersion": "0.162.0-alpha.4",
        "platform": "x86_64-linux",
        "artifact": {
            "kind": "standalone-cli",
            "version": "0.162.0-alpha.4",
            "target": "x86_64-unknown-linux-gnu",
            "entrypoint": "codex",
        },
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "codex"),
        ("version", "0.0.0"),
        ("target", "../linux"),
        ("entrypoint", "../codex"),
        ("entrypoint", "bin/codex"),
    ],
)
def test_rejects_artifact_identity_mismatch(field: str, value: str) -> None:
    data = payload()
    data["artifact"][field] = value
    with pytest.raises(LifecycleError):
        artifact_metadata(data)


def test_requires_platform_and_valid_semver() -> None:
    data = payload()
    data["platform"] = "aarch64-linux"
    with pytest.raises(LifecycleError):
        artifact_metadata(data)
    data = payload()
    data["sourceVersion"] = data["artifact"]["version"] = "0.162"
    with pytest.raises(LifecycleError):
        artifact_metadata(data)


def archive(
    path: Path,
    *,
    name: str = "codex",
    kind: bytes = tarfile.REGTYPE,
    mode: int = 0o755,
    extra: bool = False,
) -> None:
    with tarfile.open(path, "w:gz") as output:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.mode = mode
        member.size = 3 if kind == tarfile.REGTYPE else 0
        member.linkname = "codex"
        output.addfile(member, io.BytesIO(b"cli") if member.size else None)
        if extra:
            output.addfile(tarfile.TarInfo("extra"))


def test_verifies_single_executable_and_cleans_up(tmp_path: Path) -> None:
    source = tmp_path / "cli.tar.gz"
    archive(source)
    metadata = artifact_metadata(payload())
    binary = tmp_path / "expected"
    binary.write_bytes(b"cli")
    with verified_cli_archive(source, metadata, sha256(binary)) as directory:
        validate_cli_directory(directory, metadata)
        assert (directory / "codex").read_bytes() == b"cli"
    assert not directory.exists()


@pytest.mark.parametrize(
    "options",
    [
        {"name": "../codex"},
        {"name": "/codex"},
        {"name": "bin/codex"},
        {"kind": tarfile.SYMTYPE},
        {"kind": tarfile.LNKTYPE},
        {"kind": tarfile.DIRTYPE},
        {"mode": 0o644},
        {"mode": 0o4755},
        {"extra": True},
    ],
)
def test_rejects_unsafe_or_extra_members(tmp_path: Path, options: dict) -> None:
    source = tmp_path / "cli.tar.gz"
    archive(source, **options)
    with pytest.raises(LifecycleError):
        with verified_cli_archive(source, artifact_metadata(payload()), "a" * 64):
            pytest.fail("untrusted archive was accepted")


def test_rejects_digest_tampering(tmp_path: Path) -> None:
    source = tmp_path / "cli.tar.gz"
    archive(source)
    with pytest.raises(LifecycleError, match="SHA-256"):
        with verified_cli_archive(source, artifact_metadata(payload()), "a" * 64):
            pytest.fail("tampered executable was accepted")


def test_rejects_schema_confusion_and_extra_descriptor_fields() -> None:
    data = payload()
    data["package"] = {}
    with pytest.raises(LifecycleError):
        artifact_metadata(data)
    data = payload()
    data["artifact"]["resourcesDir"] = "codex-resources"
    with pytest.raises(LifecycleError):
        artifact_metadata(data)


def test_windows_contract_uses_root_exe() -> None:
    data = payload()
    data["platform"] = "x86_64-windows"
    data["artifact"].update(target="x86_64-pc-windows-msvc", entrypoint="codex.exe")
    assert artifact_metadata(data) == data["artifact"]


def test_rejects_archive_and_member_size_limits(tmp_path: Path) -> None:
    from unittest.mock import patch

    source = tmp_path / "cli.tar.gz"
    archive(source)
    with (
        patch("standalone_artifact.MAX_ARCHIVE_BYTES", 1),
        pytest.raises(LifecycleError),
    ):
        with verified_cli_archive(source, artifact_metadata(payload()), "a" * 64):
            pytest.fail("oversize archive was accepted")
    with (
        patch("standalone_artifact.MAX_BINARY_BYTES", 1),
        pytest.raises(LifecycleError),
    ):
        with verified_cli_archive(source, artifact_metadata(payload()), "a" * 64):
            pytest.fail("oversize executable was accepted")
