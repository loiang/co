"""Static CLI build command regression tests."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from build import _nix_build  # noqa: E402


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
