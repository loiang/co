"""Fast-forward the local customized main while preserving candidate state."""

from dataclasses import dataclass
from pathlib import Path

from common import LifecycleError, git, head, require_clean, require_repo


@dataclass(frozen=True, slots=True)
class PromotionCandidate:
    """Resolved candidate branch, commit, and optional attached worktree."""

    branch: str
    revision: str
    worktree: Path | None


def _branch_worktree(root: Path, branch: str) -> Path | None:
    expected = f"refs/heads/{branch}"
    for entry in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        values = dict(
            line.split(" ", 1)
            for line in entry.splitlines()
            if " " in line and line.split(" ", 1)[0] in {"worktree", "branch"}
        )
        if values.get("branch") == expected:
            return Path(values["worktree"]).resolve()
    return None


def _resolve_candidate(root: Path, value: str) -> PromotionCandidate:
    possible_path = Path(value).expanduser()
    if not possible_path.is_absolute():
        possible_path = root / possible_path
    if possible_path.exists():
        worktree = require_repo(possible_path)
        branch = git(worktree, "branch", "--show-current")
    else:
        branch = value
        worktree = _branch_worktree(root, branch)
    if not branch.startswith("upgrade/"):
        raise LifecycleError(f"promotion candidate 必须是 upgrade/*: {branch}")
    try:
        revision = git(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    except LifecycleError as error:
        raise LifecycleError(f"promotion candidate branch 不存在: {branch}") from error
    if worktree is not None:
        require_clean(worktree)
        if head(worktree) != revision:
            raise LifecycleError("candidate worktree HEAD 与 branch ref 不一致")
    return PromotionCandidate(branch, revision, worktree)


def promote(repository: Path, candidate: str) -> str:
    """Fast-forward local main without pushing or deleting candidate artifacts.

    Args:
        repository: Clean checkout currently attached to local ``main``.
        candidate: Existing ``upgrade/*`` branch name or attached worktree path.

    Returns:
        Full promoted source revision.

    Raises:
        LifecycleError: Either worktree is dirty or main cannot fast-forward.
    """
    root = require_repo(repository)
    if git(root, "branch", "--show-current") != "main":
        raise LifecycleError("co-promote 必须从本地 main worktree 运行")
    require_clean(root)
    resolved = _resolve_candidate(root, candidate)
    main_revision = head(root)
    try:
        git(root, "merge-base", "--is-ancestor", main_revision, resolved.revision)
    except LifecycleError as error:
        raise LifecycleError(
            "本地 main 已前进或分歧，拒绝非 fast-forward promotion"
        ) from error
    git(root, "merge", "--ff-only", resolved.revision, capture=False)
    if head(root) != resolved.revision:
        raise LifecycleError("fast-forward promotion 后 main identity 不一致")
    return resolved.revision
