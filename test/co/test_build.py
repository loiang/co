"""Static CLI build command regression tests."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from build import _nix_build  # noqa: E402
from common import LifecycleError  # noqa: E402


def test_nix_build_uses_all_cores_for_one_derivation(tmp_path: Path) -> None:
    """Keep package concurrency bounded while exposing every CPU core."""
    store = tmp_path / "store"
    result = subprocess.CompletedProcess([], 0, f"{store}\n", "")

    with (
        patch("build.run", return_value=result) as run,
        patch("build.verify_static_elf"),
    ):
        output, binary = _nix_build(tmp_path)

    assert output == str(store)
    assert binary == store / "bin/codex"
    run.assert_called_once_with(
        [
            "nix",
            "build",
            f"git+{tmp_path.as_uri()}#codex",
            "--no-link",
            "--print-out-paths",
            "--no-write-lock-file",
            "--max-jobs",
            "1",
            "--cores",
            "0",
        ],
        cwd=tmp_path,
    )


def test_nix_build_honors_explicit_core_limit(tmp_path: Path) -> None:
    """Allow one invocation to reduce cores without changing the default."""
    store = tmp_path / "store"
    result = subprocess.CompletedProcess([], 0, f"{store}\n", "")

    with (
        patch("build.run", return_value=result) as run,
        patch("build.verify_static_elf"),
    ):
        _nix_build(tmp_path, cores=4)

    assert run.call_args.args[0][-2:] == ["--cores", "4"]


def test_nix_build_rejects_negative_core_limit(tmp_path: Path) -> None:
    """Reject invalid limits before invoking Nix."""
    with (
        patch("build.run") as run,
        pytest.raises(LifecycleError, match="cores"),
    ):
        _nix_build(tmp_path, cores=-1)

    run.assert_not_called()
