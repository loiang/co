"""Archive CLI error reporting tests."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from codex_sessions.cli import ArchiveCommandError, entrypoint  # noqa: E402


def test_entrypoint_reports_confirmed_and_remaining_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = ArchiveCommandError(
        "partial subtree",
        archived_ids=("done",),
        remaining_ids=("left", "right"),
    )

    with patch("codex_sessions.cli.main", side_effect=failure):
        exit_code = entrypoint()

    error_output = capsys.readouterr().err
    assert exit_code == 1
    assert "已确认归档 1: done" in error_output
    assert "仍未归档 2: left,right" in error_output
