"""Lifecycle command-line resource argument tests."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from cli import _parser  # noqa: E402


@pytest.mark.parametrize("command", ["build", "upgrade", "upgrade-finalize"])
def test_building_commands_default_to_all_cores(command: str) -> None:
    """Keep all available cores as the public default."""
    arguments = [command, "--repo", str(ROOT)]
    if command == "upgrade-finalize":
        arguments.extend(("--upstream-rev", "a" * 40))

    assert _parser().parse_args(arguments).cores == 0


def test_build_accepts_per_invocation_core_limit() -> None:
    """Expose a temporary resource override without global configuration."""
    args = _parser().parse_args(["build", "--repo", str(ROOT), "--cores", "4"])

    assert args.cores == 4


def test_parser_uses_stable_co_executable_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose the Rust façade name in direct Python backend help output."""
    with pytest.raises(SystemExit):
        _parser().parse_args(["--help"])

    assert capsys.readouterr().out.startswith("usage: co ")


def test_upgrade_rejects_negative_cores_before_dispatch() -> None:
    """Fail argument parsing before any upgrade worktree mutation."""
    with pytest.raises(SystemExit):
        _parser().parse_args(["upgrade", "--repo", str(ROOT), "--cores", "-1"])
