"""Release tests retain an explicit focused Rust regression scope."""

import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from testing import RUST_TESTS  # noqa: E402


def test_focused_rust_release_tests_are_exact() -> None:
    assert RUST_TESTS == (
        ("codex-skills-extension", "bundled_skills_are_removed_and_never_loaded"),
        ("codex-state", "archive_except"),
        ("codex-tui", "local_db_first_"),
        (
            "codex-tui",
            "remote_picker_starts_from_state_db_with_cwd_filter_without_local_post_filtering",
        ),
        ("codex-tui", "archive_except"),
    )
