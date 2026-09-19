"""Consumer installation sequencing and lock identity tests."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from common import LifecycleError
from host_integration import build_official_host
from install import (
    _update_consumer,
    _verify_codex_lock,
    _verify_source_url,
    install,
)


def _write_lock(path: Path, tag: str, source_rev: str) -> None:
    path.write_text(
        json.dumps(
            {
                "root": "root",
                "nodes": {
                    "root": {"inputs": {"codex": "codex-source"}},
                    "codex-source": {
                        "locked": {
                            "ref": tag,
                            "rev": source_rev,
                            "url": "ssh://git@github.com/loiang/co.git",
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _write_host_lock(path: Path, reference: str, source_rev: str) -> None:
    path.write_text(
        json.dumps(
            {
                "root": "host",
                "nodes": {
                    "host": {"inputs": {"root": "ni-root"}},
                    "ni-root": {"inputs": {"codex": "codex-source"}},
                    "codex-source": {
                        "locked": {
                            "ref": reference,
                            "rev": source_rev,
                            "url": "ssh://git@github.com/loiang/co.git",
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_lock_verification_requires_exact_tag_and_source(tmp_path: Path) -> None:
    lock = tmp_path / "flake.lock"
    _write_lock(lock, "co-release", "a" * 40)

    _verify_codex_lock(lock, "co-release", "a" * 40)


def test_host_lock_verification_follows_root_to_codex(tmp_path: Path) -> None:
    lock = tmp_path / "flake.lock"
    _write_host_lock(lock, "main", "a" * 40)

    _verify_codex_lock(lock, "main", "a" * 40, ("root", "codex"))


@pytest.mark.parametrize("reference", ["main", "co-release"])
def test_source_url_distinguishes_tracking_from_tag_pin(
    tmp_path: Path, reference: str
) -> None:
    suffix = f"?ref={reference}"
    if reference != "main":
        suffix += f"&rev={'a' * 40}"
    (tmp_path / "flake.nix").write_text(
        "inputs = {\n"
        "  codex = {\n"
        f'    url = "git+ssh://git@github.com/loiang/co.git{suffix}";\n'
        "  };\n"
        "};\n",
        encoding="utf-8",
    )

    _verify_source_url(tmp_path, reference, "a" * 40)


def test_explicit_install_updates_builds_then_switches(tmp_path: Path) -> None:
    root = tmp_path / "co"
    ni_root = tmp_path / "ni"
    root.mkdir()
    ni_root.mkdir()
    integration = root / ".states/co/host-integration/latest.json"
    events: list[str] = []

    def capture(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        events.append("rebuild" if "rebuild" in command else "build")
        return subprocess.CompletedProcess(command, 0, "", "")

    def validated(*_args: object) -> Path:
        events.append("gate")
        return integration

    def resolved(*_args: object) -> str:
        events.append("resolve")
        return "co-20260912T154829Z-aaaaaaaaaa"

    def fetched(*_args: object) -> object:
        events.append("fetch")
        return object()

    def updated(*_args: object) -> None:
        events.append("update")

    with (
        patch("install._preflight", return_value=(root, ni_root, "a" * 40)),
        patch("install.resolve_release_tag", side_effect=resolved),
        patch("install.fetch_release_bundle", side_effect=fetched),
        patch("install.head", side_effect=["b" * 40, "c" * 40]),
        patch("install.run", side_effect=capture),
        patch("install._validate_candidate_source", side_effect=validated),
        patch("install._update_consumer", side_effect=updated),
        patch("install._record_install"),
    ):
        switch = install(root, ni_root, "dev", "co-release")

    assert events == ["resolve", "fetch", "gate", "update", "build", "rebuild"]
    assert "rebuild dev switch --no-update" in switch


def test_install_dry_run_stops_before_consumer_mutation(tmp_path: Path) -> None:
    root = tmp_path / "co"
    ni_root = tmp_path / "ni"
    root.mkdir()
    ni_root.mkdir()

    with (
        patch("install._preflight", return_value=(root, ni_root, "a" * 40)),
        patch(
            "install.resolve_release_tag",
            return_value="co-20260912T154829Z-aaaaaaaaaa",
        ),
        patch("install.fetch_release_bundle", return_value=object()),
        patch(
            "install._validate_candidate_source", return_value=root / "record"
        ) as gate,
        patch("install.run") as run,
    ):
        command = install(root, ni_root, "dev", dry_run=True)

    gate.assert_called_once()
    run.assert_not_called()
    assert "rebuild dev switch --no-update" in command


def test_candidate_failure_occurs_before_ni_update(tmp_path: Path) -> None:
    root = tmp_path / "co"
    ni_root = tmp_path / "ni"
    root.mkdir()
    ni_root.mkdir()

    with (
        patch("install._preflight", return_value=(root, ni_root, "a" * 40)),
        patch(
            "install.resolve_release_tag",
            return_value="co-20260912T154829Z-aaaaaaaaaa",
        ),
        patch("install.fetch_release_bundle", return_value=object()),
        patch(
            "install._validate_candidate_source",
            side_effect=LifecycleError("incompatible"),
        ),
        patch("install._update_consumer") as update,
        pytest.raises(LifecycleError, match="incompatible"),
    ):
        install(root, ni_root, "dev")

    update.assert_not_called()


def test_missing_main_release_fails_before_consumer_mutation(tmp_path: Path) -> None:
    root = tmp_path / "co"
    ni_root = tmp_path / "ni"
    root.mkdir()
    ni_root.mkdir()

    with (
        patch("install._preflight", return_value=(root, ni_root, "a" * 40)),
        patch(
            "install.resolve_release_tag",
            side_effect=LifecycleError("main has no release"),
        ),
        patch("install.fetch_release_bundle") as fetch,
        patch("install._validate_candidate_source") as gate,
        patch("install._update_consumer") as update,
        pytest.raises(LifecycleError, match="no release"),
    ):
        install(root, ni_root, "dev")

    fetch.assert_not_called()
    gate.assert_not_called()
    update.assert_not_called()


def test_official_host_build_never_rewrites_consumer_lock(tmp_path: Path) -> None:
    store = tmp_path / "host-store"
    commands: list[list[str]] = []

    def capture(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, str(store) + "\n", "")

    with patch("host_integration.run", side_effect=capture):
        assert build_official_host(tmp_path, tmp_path / "ni") == str(store)

    assert "--no-write-lock-file" in commands[0]


def test_update_passes_expected_revision_to_ni(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def capture(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch("install.run", side_effect=capture),
        patch("install._locked_input", return_value={"narHash": "same"}),
        patch("install._verify_source_url"),
        patch("install._verify_codex_lock"),
    ):
        _update_consumer(
            tmp_path,
            "dev",
            "main",
            "a" * 40,
            "co-20260912T154829Z-aaaaaaaaaa",
        )

    command = commands[0]
    assert command[-4:] == [
        "--expected-rev",
        "a" * 40,
        "--release-tag",
        "co-20260912T154829Z-aaaaaaaaaa",
    ]


def test_native_host_gate_receives_complete_package_without_static_elf_gate(
    tmp_path: Path,
) -> None:
    from common import sha256
    from install import _validate_candidate_source
    from native_fixtures import make_assets
    from release_asset import Artifact, fetch_release_bundle

    source = "a" * 40
    assets = make_assets(tmp_path, source, "b" * 40)
    files = {
        path.name: Artifact(
            path.name,
            "https://release.invalid",
            path,
            sha256(path),
            path.stat().st_size,
        )
        for path in assets
    }
    with patch(
        "release_asset._prefetch", side_effect=lambda _root, _tag, name: files[name]
    ):
        bundle = fetch_release_bundle(
            tmp_path, f"co-20260919T000000Z-{source[:10]}", source
        )
    host = tmp_path / "host/bin/codex-code-mode-host"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"official-host")

    def gate(root: Path, binary: Path, host_binary: Path) -> list[str]:
        assert root == tmp_path
        assert binary.parent.name == "bin"
        assert (binary.parent.parent / "codex-resources/bwrap").is_file()
        assert host_binary == host
        return ["validated"]

    with (
        patch("install.build_official_host", return_value=str(host.parent.parent)),
        patch("install.test_candidate_binaries", side_effect=gate),
        patch("install.official_host_lock", return_value={"narHash": "fixed"}),
        patch("install.head", return_value="c" * 40),
    ):
        record = _validate_candidate_source(tmp_path, tmp_path, bundle)
    evidence = json.loads(record.read_text())
    assert evidence["release"]["cli"]["binaryPath"] == "bin/codex"
    assert evidence["release"]["package"]["target"] == "x86_64-unknown-linux-gnu"
    assert "cliArchiveStorePath" not in evidence
