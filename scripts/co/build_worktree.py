"""Own the fixed build checkout without deleting unrelated Git worktrees.

A nonblocking lock serializes builders. A durable ownership record lets the
next invocation reclaim an interrupted checkout only after Git confirms its
path, branch and unchanged base commit.
"""

import fcntl
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from common import LifecycleError, git, read_json, timestamp, write_json


def _state_dir(root: Path) -> Path:
    state = root / ".states/co/build"
    if state.resolve() != state:
        raise LifecycleError("build state 路径包含 symlink，拒绝使用")
    state.mkdir(parents=True, exist_ok=True)
    return state


@contextmanager
def build_lock(root: Path) -> Iterator[int]:
    """Serialize the full build and publication of its local evidence.

    The lock file stays in place so concurrent processes always lock the same
    inode; closing the descriptor also releases locks after process failure.
    """
    lock_path = _state_dir(root) / "build.lock"
    if lock_path.is_symlink():
        raise LifecycleError("build lock 不允许 symlink")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LifecycleError("另一个 co build 正在运行") from error
        # Close, rather than unlock, so an orphaned Cargo process that
        # inherited this descriptor keeps the build locked until it exits.
        yield lock.fileno()


def _registered(root: Path, path: Path) -> dict[str, str] | None:
    for block in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in block.splitlines())
        if fields.get("worktree") == str(path):
            return fields
    return None


def _cleanup(root: Path, record_path: Path) -> None:
    if record_path.is_symlink():
        raise LifecycleError("build ownership record 不允许 symlink；保留现场")
    record = read_json(record_path)
    path = root / ".states/build"
    revision = record.get("sourceRev")
    branch = record.get("branch")
    if (
        record.get("schemaVersion") != 1
        or record.get("root") != str(root)
        or record.get("worktree") != str(path)
        or not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", revision) is None
        or not isinstance(branch, str)
        or re.fullmatch(r"build/co-[0-9]{8}T[0-9]{6}Z-" + revision[:10], branch) is None
    ):
        raise LifecycleError("build ownership record 无效；保留现场")
    if path.is_symlink():
        raise LifecycleError("build worktree 已被 symlink 替换；保留现场")
    ref = f"refs/heads/{branch}"
    branch_head = git(root, "for-each-ref", "--format=%(objectname)", ref)
    if branch_head and branch_head != revision:
        raise LifecycleError("build branch 已被修改；保留现场")
    registered = _registered(root, path)
    if registered is not None:
        if registered.get("branch") != ref or registered.get("HEAD") != revision:
            raise LifecycleError("build worktree identity 不匹配；保留现场")
        if path.exists() and (
            git(path, "rev-parse", "--show-toplevel") != str(path)
            or git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            != git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ):
            raise LifecycleError("build worktree Git registration 不匹配；保留现场")
        git(root, "worktree", "remove", "--force", str(path))
    elif path.exists():
        raise LifecycleError("build 路径不再是托管 worktree；保留现场")
    if branch_head:
        git(root, "branch", "-D", branch)
    record_path.unlink()


@contextmanager
def build_worktree(root: Path, revision: str) -> Iterator[Path]:
    """Yield a disposable branch at an exact commit while holding build_lock.

    Cleanup runs for normal exits, builder failures and KeyboardInterrupt.
    A stale record is recovered before creating the next checkout; unknown
    paths and modified branch identities are preserved for manual inspection.
    """
    record_path = _state_dir(root) / "run.json"
    if record_path.exists():
        _cleanup(root, record_path)
    path = root / ".states/build"
    if path.exists() or path.is_symlink() or _registered(root, path) is not None:
        raise LifecycleError(f"未托管 build 路径已存在: {path}")
    branch = f"build/co-{timestamp()}-{revision[:10]}"
    if git(root, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}"):
        raise LifecycleError(f"未托管 build branch 已存在: {branch}")
    write_json(
        record_path,
        {
            "schemaVersion": 1,
            "root": str(root),
            "worktree": str(path),
            "branch": branch,
            "sourceRev": revision,
        },
    )
    original_error: BaseException | None = None
    try:
        git(root, "worktree", "add", "-b", branch, str(path), revision)
        yield path
    except BaseException as error:
        original_error = error
        raise
    finally:
        try:
            _cleanup(root, record_path)
        except (LifecycleError, OSError) as cleanup_error:
            if original_error is not None:
                raise LifecycleError(
                    f"build 失败: {type(original_error).__name__}: {original_error}; "
                    f"清理失败: {cleanup_error}"
                ) from original_error
            raise
