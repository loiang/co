"""Release tests run without compiling Rust targets."""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from testing import run_tests  # noqa: E402


def test_tests_use_locked_nix_shell_without_compilation(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def capture(command: list[str], **_kwargs: object) -> None:
        commands.append(command)

    with (
        patch("testing.require_repo", return_value=tmp_path),
        patch("testing.run", side_effect=capture),
        patch("testing.source_identity", return_value={}),
        patch("testing.write_json"),
    ):
        run_tests(tmp_path)

    assert commands == [
        [
            "nix",
            "develop",
            f"git+file://{tmp_path}",
            "--no-update-lock-file",
            "--command",
            "python3",
            "-m",
            "pytest",
            "-q",
            "test/co",
        ]
    ]
