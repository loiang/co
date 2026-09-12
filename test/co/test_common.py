"""Local flake inputs are Git-filtered without omitting unknown source files."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from common import LifecycleError, git_flake, require_no_untracked  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True)


def test_local_flake_uri_encodes_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "co checkout"
    root.mkdir()

    assert git_flake(root, "codex") == f"git+{root.as_uri()}#codex"


def test_build_input_rejects_nonignored_untracked_source(tmp_path: Path) -> None:
    root = tmp_path / "co"
    root.mkdir()
    _git(root, "init")
    (root / ".gitignore").write_text("ignored\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    (root / "ignored").write_text("state\n", encoding="utf-8")
    require_no_untracked(root)
    (root / "new-source.rs").write_text("source\n", encoding="utf-8")

    with pytest.raises(LifecycleError, match="显式 git add"):
        require_no_untracked(root)


def test_lifecycle_has_no_raw_local_path_flake_inputs() -> None:
    modules = tuple((ROOT / "scripts/co").glob("*.py"))

    assert not any('f"path:' in path.read_text(encoding="utf-8") for path in modules)
