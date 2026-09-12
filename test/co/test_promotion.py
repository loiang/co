"""Local main promotion uses only a clean fast-forward and preserves candidates."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from common import LifecycleError  # noqa: E402
from promotion import promote  # noqa: E402


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    root = tmp_path / "co"
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True)
    _git(root, "config", "user.name", "Co Test")
    _git(root, "config", "user.email", "co-test@example.invalid")
    (root / ".gitignore").write_text(".states/\n", encoding="utf-8")
    (root / "tracked").write_text("base\n", encoding="utf-8")
    base = _commit(root, "base")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "origin", "main")
    candidate = tmp_path / "candidate"
    _git(root, "worktree", "add", "-b", "upgrade/test", str(candidate), base)
    (candidate / "tracked").write_text("candidate\n", encoding="utf-8")
    _commit(candidate, "candidate")
    return root, candidate, remote, base


def test_promote_fast_forwards_without_push_or_cleanup(tmp_path: Path) -> None:
    root, candidate, remote, remote_main = _repository(tmp_path)
    candidate_head = _git(candidate, "rev-parse", "HEAD")
    evidence = candidate / ".states/co/build/latest.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("evidence\n", encoding="utf-8")

    assert promote(root, str(candidate)) == candidate_head

    assert _git(root, "branch", "--show-current") == "main"
    assert _git(root, "rev-parse", "HEAD") == candidate_head
    assert _git(remote, "rev-parse", "refs/heads/main") == remote_main
    assert candidate.is_dir() and evidence.is_file()
    assert _git(root, "show-ref", "--verify", "refs/heads/upgrade/test")


def test_promote_accepts_an_attached_candidate_branch_name(tmp_path: Path) -> None:
    root, candidate, _, _ = _repository(tmp_path)

    assert promote(root, "upgrade/test") == _git(candidate, "rev-parse", "HEAD")


@pytest.mark.parametrize("dirty_worktree", ["main", "candidate"])
def test_promote_rejects_dirty_worktrees(tmp_path: Path, dirty_worktree: str) -> None:
    root, candidate, _, _ = _repository(tmp_path)
    selected = root if dirty_worktree == "main" else candidate
    (selected / "tracked").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(LifecycleError, match="工作树存在"):
        promote(root, str(candidate))


def test_promote_rejects_main_that_advanced_independently(tmp_path: Path) -> None:
    root, candidate, _, _ = _repository(tmp_path)
    (root / "main-only").write_text("advance\n", encoding="utf-8")
    main_head = _commit(root, "main advance")

    with pytest.raises(LifecycleError, match="已前进或分歧"):
        promote(root, str(candidate))

    assert _git(root, "rev-parse", "HEAD") == main_head
    assert candidate.is_dir()
