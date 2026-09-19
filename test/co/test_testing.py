"""Release tests retain an explicit focused Rust regression scope."""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from testing import RUST_MIN_STACK, RUST_TESTS, run_tests  # noqa: E402


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


def test_focused_tests_use_locked_nix_shell_and_nextest(tmp_path: Path) -> None:
    """Each focused Rust test keeps the former stack and nextest semantics."""
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

    rust_commands = commands[1:]
    assert len(rust_commands) == len(RUST_TESTS)
    assert all(command[:2] == ["nix", "develop"] for command in rust_commands)
    assert all("--no-update-lock-file" in command for command in rust_commands)
    assert all(
        f"RUST_MIN_STACK={RUST_MIN_STACK}" in command for command in rust_commands
    )
    assert all("NEXTEST_PROFILE=local" in command for command in rust_commands)
    assert all(
        command[command.index("cargo") : command.index("cargo") + 4]
        == ["cargo", "nextest", "run", "--no-fail-fast"]
        for command in rust_commands
    )
