"""Run repository Python regressions in locked Nix."""

from pathlib import Path

from common import git_flake, require_repo, run, timestamp, write_json
from evidence import source_identity


def run_tests(repository: Path) -> Path:
    """Run lifecycle regressions in the candidate shell.

    The candidate's locked flake supplies Python so host tool versions cannot
    change the verification result. Compilation is owned by ``co build``.

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
