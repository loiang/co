"""Exercise owned build worktrees against real Git, including interrupted runs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[2] / "scripts/co"
sys.path.insert(0, str(SCRIPTS))

from build_worktree import build_lock, build_worktree
from common import LifecycleError, git


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text(".states/\n")
    (tmp_path / "source").write_text("original\n")
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "add", ".")
    git(
        tmp_path,
        "-c",
        "user.name=co-test",
        "-c",
        "user.email=co@localhost",
        "commit",
        "--quiet",
        "-m",
        "source",
    )
    return tmp_path


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_disposable_branch_preserves_source_and_removes_worktree(
    repository: Path,
    failure: type[BaseException] | None,
) -> None:
    revision = git(repository, "rev-parse", "HEAD")
    original_branches = git(repository, "branch", "--format=%(refname)")

    def execute() -> None:
        with build_lock(repository), build_worktree(repository, revision) as worktree:
            assert worktree == repository / ".states/build"
            assert git(worktree, "rev-parse", "HEAD") == revision
            assert git(worktree, "branch", "--show-current").startswith("build/co-")
            (worktree / "source").write_text("stamped\n")
            if failure is not None:
                raise failure("builder failed")

    if failure is None:
        execute()
    else:
        with pytest.raises(failure, match="builder failed"):
            execute()
    assert (repository / "source").read_text() == "original\n"
    assert not (repository / ".states/build").exists()
    assert not (repository / ".states/co/build/run.json").exists()
    assert git(repository, "branch", "--format=%(refname)") == original_branches
    assert git(repository, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_unknown_existing_path_is_preserved(repository: Path) -> None:
    path = repository / ".states/build"
    path.mkdir(parents=True)
    (path / "user-content").write_text("keep")
    with build_lock(repository), pytest.raises(LifecycleError, match="未托管"):
        with build_worktree(repository, git(repository, "rev-parse", "HEAD")):
            pytest.fail("unknown directories must not be adopted")
    assert (path / "user-content").read_text() == "keep"


def test_build_lock_fails_immediately_on_contention(repository: Path) -> None:
    with build_lock(repository), pytest.raises(LifecycleError, match="正在运行"):
        with build_lock(repository):
            pytest.fail("concurrent builds must not share the fixed worktree")


def test_inherited_lock_blocks_recovery_until_builder_exits(repository: Path) -> None:
    with build_lock(repository) as descriptor:
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            pass_fds=(descriptor,),
        )
    try:
        with pytest.raises(LifecycleError, match="正在运行"):
            with build_lock(repository):
                pytest.fail("orphaned builder must retain the lock")
    finally:
        child.communicate(timeout=5)
    with build_lock(repository):
        pass


def test_existing_unowned_worktree_is_preserved(repository: Path) -> None:
    path = repository / ".states/build"
    git(repository, "worktree", "add", "-b", "user-build", str(path), "HEAD")
    with build_lock(repository), pytest.raises(LifecycleError, match="未托管"):
        with build_worktree(repository, git(repository, "rev-parse", "HEAD")):
            pytest.fail("unowned worktrees must not be removed")
    assert git(path, "branch", "--show-current") == "user-build"


def test_symlink_build_path_preserves_target(repository: Path, tmp_path: Path) -> None:
    target = tmp_path / "unrelated"
    target.mkdir()
    path = repository / ".states/build"
    path.parent.mkdir(exist_ok=True)
    path.symlink_to(target, target_is_directory=True)
    with build_lock(repository), pytest.raises(LifecycleError, match="未托管"):
        with build_worktree(repository, git(repository, "rev-parse", "HEAD")):
            pytest.fail("symlink paths must not be adopted")
    assert target.is_dir()
    assert path.is_symlink()


def test_changed_build_branch_is_never_deleted(repository: Path) -> None:
    with build_lock(repository), pytest.raises(LifecycleError, match="branch 已被修改"):
        with build_worktree(repository, git(repository, "rev-parse", "HEAD")) as path:
            git(
                path,
                "-c",
                "user.name=co-test",
                "-c",
                "user.email=co@localhost",
                "commit",
                "--allow-empty",
                "--quiet",
                "-m",
                "user-change",
            )
    assert (path / ".git").is_file()
    assert (repository / ".states/co/build/run.json").is_file()


def test_abandoned_owned_worktree_is_recovered(repository: Path) -> None:
    code = (
        "import os, sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(SCRIPTS)!r}); "
        "from common import git; from build_worktree import build_lock, build_worktree; "
        f"root = Path({str(repository)!r}); "
        "lock = build_lock(root); lock.__enter__(); "
        "context = build_worktree(root, git(root, 'rev-parse', 'HEAD')); "
        "tree = context.__enter__(); (tree / 'source').write_text('interrupted'); "
        "os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    record_path = repository / ".states/co/build/run.json"
    previous = json.loads(record_path.read_text())
    with (
        build_lock(repository),
        build_worktree(repository, previous["sourceRev"]) as tree,
    ):
        assert (tree / "source").read_text() == "original\n"
    assert not record_path.exists()
    assert git(repository, "branch", "--list", "build/co-*") == ""


def test_cleanup_failure_reports_original_failure(
    repository: Path, monkeypatch
) -> None:
    import build_worktree as module

    original_git = module.git

    def fail_remove(root: Path, *args: str, **kwargs) -> str:
        if args[:2] == ("worktree", "remove"):
            raise LifecycleError("remove failed")
        return original_git(root, *args, **kwargs)

    with (
        build_lock(repository),
        pytest.raises(LifecycleError, match="builder failed.*remove failed"),
    ):
        with build_worktree(repository, git(repository, "rev-parse", "HEAD")):
            monkeypatch.setattr(module, "git", fail_remove)
            raise RuntimeError("builder failed")
    assert (repository / ".states/co/build/run.json").exists()
