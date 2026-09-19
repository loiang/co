"""Shared process, repository, hashing, and state primitives.

Lifecycle modules use these narrow helpers to keep subprocess failures
actionable and verification records atomic without hiding Git side effects.
"""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


class LifecycleError(RuntimeError):
    """Report a lifecycle invariant or external command failure.

    The CLI catches this single boundary type and returns a stable nonzero exit
    while retaining the original worktree and verification records for retry.
    """


class OutputMode(Enum):
    """Choose inherited diagnostics independently from machine-readable stdout."""

    CAPTURE = "capture"
    CAPTURE_STDOUT = "capture_stdout"
    INHERIT = "inherit"


def run(
    command: list[str],
    *,
    cwd: Path,
    capture: bool | OutputMode = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command and raise a diagnostic failure without shell parsing.

    Args:
        command: Argument vector passed directly to the child process.
        cwd: Explicit working directory that owns the operation.
        capture: Output policy; legacy booleans capture both streams or neither.
        env: Optional complete child environment.

    Returns:
        The successful completed process.

    Raises:
        LifecycleError: The command exits unsuccessfully.
    """
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        stdout=(
            subprocess.PIPE
            if capture is not False and capture is not OutputMode.INHERIT
            else None
        ),
        stderr=(
            subprocess.PIPE
            if capture is True or capture is OutputMode.CAPTURE
            else None
        ),
        text=True,
        env=env,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        rendered = " ".join(command)
        suffix = f": {detail}" if detail else ""
        raise LifecycleError(f"命令失败 ({rendered}){suffix}")
    return result


def git(root: Path, *args: str, capture: bool = True) -> str:
    """Run Git against an explicit repository and return stripped stdout.

    Args:
        root: Git worktree root.
        *args: Git subcommand and arguments.
        capture: Capture output for inspection.

    Returns:
        Stripped stdout, or an empty string for streamed commands.
    """
    result = run(["git", "-C", str(root), *args], cwd=root, capture=capture)
    return (result.stdout or "").strip()


def require_repo(root: Path) -> Path:
    """Resolve and verify the exact Git top level used by a lifecycle command.

    Args:
        root: Requested checkout path.

    Returns:
        The canonical checkout root.

    Raises:
        LifecycleError: The path is not the requested Git top level.
    """
    resolved = root.resolve()
    top = Path(git(resolved, "rev-parse", "--show-toplevel")).resolve()
    if top != resolved:
        raise LifecycleError(f"repo 必须是 Git top level: {resolved} != {top}")
    return resolved


def git_flake(root: Path, attribute: str | None = None) -> str:
    """Return a Git-filtered local flake URI that excludes build/state outputs.

    Args:
        root: Local Git checkout used as the flake source.
        attribute: Optional flake output attribute without a leading hash.

    Returns:
        Absolute ``git+file://`` URI with an optional output fragment.
    """
    reference = f"git+{root.resolve().as_uri()}"
    return f"{reference}#{attribute}" if attribute is not None else reference


def head(root: Path) -> str:
    """Return the immutable full commit ID for a checkout.

    Args:
        root: Verified Git worktree.

    Returns:
        Full hexadecimal commit ID.
    """
    return git(root, "rev-parse", "HEAD^{commit}")


def working_dirty(root: Path) -> bool:
    """Detect staged, tracked, or untracked changes outside ignored state.

    Args:
        root: Verified Git worktree.

    Returns:
        True when publishable checkout content differs from ``HEAD``.
    """
    return bool(git(root, "status", "--porcelain=v1"))


def require_clean(root: Path) -> None:
    """Reject candidate mutation or publication from a mutable source tree.

    Args:
        root: Verified Git worktree.

    Raises:
        LifecycleError: Tracked or staged changes exist.
    """
    if working_dirty(root):
        raise LifecycleError("工作树存在 staged、tracked 或 untracked 修改")


def require_no_untracked(root: Path) -> None:
    """Reject non-ignored untracked inputs before a Git-filtered local build.

    Args:
        root: Verified Git worktree used as a local flake source.

    Raises:
        LifecycleError: Git would omit one or more untracked source files.
    """
    output = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    count = len(tuple(path for path in output.split("\0") if path))
    if count:
        raise LifecycleError(
            f"build 检测到 {count} 个非 ignored 未跟踪文件；"
            "确认不含敏感数据后请显式 git add"
        )


def sha256(path: Path) -> str:
    """Hash one artifact without loading it fully into memory.

    Args:
        path: Regular file to hash.

    Returns:
        Lowercase SHA-256 hexadecimal digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def timestamp() -> str:
    """Return a sortable UTC timestamp used in immutable candidate names.

    Returns:
        UTC timestamp in ``YYYYmmddTHHMMSSZ`` form.
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object used as a verification or retry record.

    Args:
        path: JSON record path.

    Returns:
        Parsed JSON object.

    Raises:
        LifecycleError: The file is missing, malformed, or not an object.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError(f"无法读取状态记录 {path}: {error}") from error
    if not isinstance(value, dict):
        raise LifecycleError(f"状态记录不是 JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace a local JSON verification record.

    Args:
        path: Destination under ignored lifecycle state.
        value: JSON object to serialize deterministically.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
