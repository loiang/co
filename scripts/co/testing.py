"""Run repository Python regressions and focused Rust tests in locked Nix."""

from pathlib import Path

from common import git_flake, require_repo, run, timestamp, write_json
from evidence import source_identity

RUST_TESTS = (
    ("codex-skills-extension", "bundled_skills_are_removed_and_never_loaded"),
    ("codex-state", "archive_except"),
    ("codex-tui", "local_db_first_"),
    (
        "codex-tui",
        "remote_picker_starts_from_state_db_with_cwd_filter_without_local_post_filtering",
    ),
    ("codex-tui", "archive_except"),
)
RUST_MIN_STACK = "8388608"


def run_tests(repository: Path) -> Path:
    """Run lifecycle regressions and focused Rust tests in the candidate shell.

    The candidate's locked flake supplies Python, Cargo, and nextest so host
    tool versions cannot change the verification result.

    Args:
        repository: Candidate checkout whose locked environment owns the run.

    Returns:
        Path to the atomic verification record.
    """
    root = require_repo(repository)
    commands: list[list[str]] = [
        [
            "nix",
            "develop",
            git_flake(root),
            "--no-update-lock-file",
            "--command",
            "python3",
            "-m",
            "pytest",
            "-q",
            "test/co",
        ]
    ]
    for package, test_name in RUST_TESTS:
        commands.append(
            [
                "nix",
                "develop",
                git_flake(root),
                "--no-update-lock-file",
                "--command",
                "env",
                f"RUST_MIN_STACK={RUST_MIN_STACK}",
                "NEXTEST_PROFILE=local",
                "cargo",
                "nextest",
                "run",
                "--no-fail-fast",
                "--manifest-path",
                "codex-rs/Cargo.toml",
                "-p",
                package,
                test_name,
            ]
        )
    for command in commands:
        run(command, cwd=root, capture=False)
    record = {
        "schemaVersion": 1,
        **source_identity(root),
        "completedAt": timestamp(),
        "commands": commands,
    }
    path = root / ".states/co/test/latest.json"
    write_json(path, record)
    return path
