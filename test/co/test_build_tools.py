"""Regression tests for native build-tool resolution."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from build_tools import resolve_make_bin  # noqa: E402
from common import LifecycleError  # noqa: E402
from native_package import PackageRequest, _environment  # noqa: E402


def _make_fixture(path: Path, *, version: str = "4.4") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf 'GNU Make {version}\\n'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _store_make(
    store_root: Path,
    name: str,
    *,
    version: str,
    executable: bool = True,
) -> Path:
    path = store_root / name / "bin" / "make"
    _make_fixture(path, version=version)
    if not executable:
        path.chmod(0o644)
    return path


def test_path_make_has_priority_over_a_newer_store_make(tmp_path: Path) -> None:
    path_make = _make_fixture(tmp_path / "path-bin" / "make", version="4.3")
    _store_make(tmp_path / "store", "hash-gnumake-4.4.0", version="4.4")

    selected = resolve_make_bin(
        {"PATH": str(path_make.parent)},
        store_root=tmp_path / "store",
        platform_name="Linux",
    )

    assert selected == path_make


def test_store_fallback_selects_highest_version_deterministically(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    _store_make(store, "aaa-gnumake-4.10.0", version="4.10")
    expected = _store_make(store, "zzz-gnumake-4.10.0", version="4.10")
    _store_make(store, "new-gnumake-4.9.9", version="4.9")

    selected = resolve_make_bin({"PATH": ""}, store_root=store, platform_name="Linux")

    assert selected == expected


def test_store_fallback_rejects_fake_and_non_executable_candidates(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    fake = _store_make(store, "fake-gnumake-9.0.0", version="not GNU Make")
    fake.write_text("#!/bin/sh\nprintf 'fake make\\n'\n", encoding="utf-8")
    fake.chmod(0o755)
    _store_make(store, "old-gnumake-4.0.0", version="4.0", executable=False)

    with pytest.raises(LifecycleError, match="GNU Make"):
        resolve_make_bin({"PATH": ""}, store_root=store, platform_name="Linux")


def test_store_fallback_rejects_candidate_symlink_outside_store(
    tmp_path: Path,
) -> None:
    outside = _make_fixture(tmp_path / "outside" / "make")
    candidate = tmp_path / "store/hash-gnumake-9.0.0/bin/make"
    candidate.parent.mkdir(parents=True)
    candidate.symlink_to(outside)

    with pytest.raises(LifecycleError, match="GNU Make"):
        resolve_make_bin(
            {"PATH": ""},
            store_root=tmp_path / "store",
            platform_name="Linux",
        )


def test_missing_make_fails_without_touching_real_store(tmp_path: Path) -> None:
    with pytest.raises(LifecycleError, match="GNU Make"):
        resolve_make_bin(
            {"PATH": ""}, store_root=tmp_path / "injected-store", platform_name="Linux"
        )


def test_non_linux_does_not_scan_injected_store(tmp_path: Path) -> None:
    _store_make(tmp_path / "store", "hash-gnumake-4.4.0", version="4.4")

    with pytest.raises(LifecycleError, match="PATH"):
        resolve_make_bin(
            {"PATH": ""}, store_root=tmp_path / "store", platform_name="Darwin"
        )


def test_builder_environment_prepends_make_without_nix_command(tmp_path: Path) -> None:
    make = _make_fixture(tmp_path / "make-bin" / "make")
    request = PackageRequest(tmp_path, tmp_path, "a" * 40, "0.154.0")

    with (
        patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False),
        patch("native_package.resolve_make_bin", return_value=make),
    ):
        env = _environment(request)

    assert env["PATH"].split(os.pathsep) == [str(make.parent), "/usr/bin"]
    assert "MAKE" not in env
