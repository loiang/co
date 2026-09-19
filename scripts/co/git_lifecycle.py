"""Create isolated upgrade candidates by merging a fixed upstream commit.

The published main line is never rewritten. Each upgrade starts from that line
in a new worktree and merges upstream history, preserving customization commits.
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _git_merge import CandidateConflict as CandidateConflict
from _git_merge import _merge_upstream
from common import (
    LifecycleError,
    git,
    git_flake,
    head,
    require_clean,
    require_repo,
    run,
    timestamp,
)

UPSTREAM_FILE = Path(".co/upstream-rev")
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class Candidate:
    """Describe a candidate worktree without making it the active checkout.

    Attributes:
        root: Candidate worktree path.
        branch: Candidate branch name.
        upstream_rev: Full fetched upstream commit ID.
        changed: Whether a new candidate was created.
    """

    root: Path
    branch: str
    upstream_rev: str
    changed: bool


def _succeeds(root: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        cwd=root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _recorded_upstream(root: Path) -> str:
    path = root / UPSTREAM_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise LifecycleError(f"缺少 tracked upstream baseline: {path}") from error
    if not SHA_RE.fullmatch(value):
        raise LifecycleError(f"upstream baseline 不是完整 Git SHA: {value}")
    if not _succeeds(root, "cat-file", "-e", f"{value}^{{commit}}"):
        raise LifecycleError(f"upstream baseline object 不存在: {value}")
    return value


def _resolve_target(root: Path, revision: str) -> str:
    git(root, "fetch", "--prune", "upstream", capture=False)
    target = git(root, "rev-parse", f"{revision}^{{commit}}")
    if not SHA_RE.fullmatch(target):
        raise LifecycleError(f"目标 revision 未解析为完整 Git SHA: {target}")
    if not _succeeds(root, "merge-base", "--is-ancestor", target, "upstream/main"):
        raise LifecycleError(f"目标 revision 不在 fetched upstream/main 上: {target}")
    return target


def _candidate_identity(root: Path, target: str, created_at: str) -> tuple[str, Path]:
    suffix = f"{created_at}-{target[:10]}"
    branch = f"upgrade/{suffix}"
    worktree = root / ".states" / "worktrees" / suffix
    if _succeeds(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
        raise LifecycleError(f"candidate branch 已存在，拒绝覆盖: {branch}")
    if worktree.exists():
        raise LifecycleError(f"candidate worktree 已存在，拒绝覆盖: {worktree}")
    return branch, worktree


def create_upgrade_candidate(
    repository: Path,
    revision: str = "upstream/main",
    created_at: str | None = None,
    ni_repository: Path = Path("/repo/ni"),
    build_cores: int = 0,
) -> Candidate:
    """Merge a fixed upstream commit into a new isolated worktree.

    Args:
        repository: Current published candidate checkout.
        revision: Upstream ref or commit to resolve after fetching upstream.
        created_at: Fixed UTC name component used by deterministic tests.
        ni_repository: Consumer checkout that owns the official host package.
        build_cores: Cores exposed to the candidate Nix build; zero means all.

    Returns:
        Candidate identity; ``changed`` is false for the same upstream SHA.

    Raises:
        CandidateConflict: Git leaves a conflicted merge for manual recovery.
        LifecycleError: Repository, ancestry, cleanliness, or identity fails.
    """
    if build_cores < 0:
        raise LifecycleError("Nix cores 必须是非负整数")
    root = require_repo(repository)
    require_clean(root)
    source_branch = git(root, "branch", "--show-current")
    if source_branch != "main" and not source_branch.startswith("custom/"):
        raise LifecycleError(
            f"upgrade 只能从 main 或初始化 custom/* 启动: {source_branch}"
        )
    source_head = head(root)
    baseline = _recorded_upstream(root)
    target = _resolve_target(root, revision)
    if not _succeeds(root, "merge-base", "--is-ancestor", baseline, source_head):
        raise LifecycleError("tracked upstream baseline 不是当前 candidate 的 ancestor")
    if not _succeeds(root, "merge-base", "--is-ancestor", baseline, target):
        raise LifecycleError("目标 upstream revision 早于 tracked baseline，拒绝降级")
    if target == baseline:
        return Candidate(root, source_branch, target, changed=False)

    branch, worktree = _candidate_identity(root, target, created_at or timestamp())
    candidate = Candidate(worktree, branch, target, changed=True)
    _merge_upstream(root, source_head, candidate, ni_repository, build_cores)
    return candidate


def finalize_candidate(candidate: Candidate) -> None:
    """Pin the new baseline, refresh flake inputs, and commit only those locks.

    Args:
        candidate: Successfully merged candidate to finalize.
    """
    root = require_repo(candidate.root)
    if _succeeds(root, "rev-parse", "--verify", "MERGE_HEAD"):
        raise LifecycleError("merge 尚未完成；先解决冲突并执行 merge --continue")
    (root / UPSTREAM_FILE).write_text(candidate.upstream_rev + "\n", encoding="utf-8")
    try:
        run(["nix", "flake", "lock"], cwd=root, capture=False)
        run(
            [
                "nix",
                "develop",
                git_flake(root),
                "--no-update-lock-file",
                "--command",
                "python3",
                "nix/scripts/update-cargo-git-hashes.py",
            ],
            cwd=root,
            capture=False,
        )
        run(
            ["nix", "fmt", "--", "nix/cargo-git-hashes.nix"],
            cwd=root,
            capture=False,
        )
        run(
            [
                "nix",
                "build",
                ".#codex-cargo-deps",
                "--no-link",
                "--no-write-lock-file",
            ],
            cwd=root,
            capture=False,
        )
    except LifecycleError as error:
        raise LifecycleError(
            f"dependency lock 更新或验真失败；candidate 保留于 {root}: {error}"
        ) from error
    git(root, "add", str(UPSTREAM_FILE), "flake.lock", "nix/cargo-git-hashes.nix")
    if _succeeds(root, "diff", "--cached", "--quiet"):
        raise LifecycleError("upstream 变化未产生 baseline/lock staged diff")
    git(
        root,
        "commit",
        "-m",
        f"build(co): advance upstream to {candidate.upstream_rev[:10]}",
    )


def validate_candidate(
    candidate: Candidate,
    ni_repository: Path = Path("/repo/ni"),
    build_cores: int = 0,
) -> None:
    """Run the candidate checkout's lifecycle CLI without registry lookup.

    Args:
        candidate: Candidate whose tracked baseline update is committed.
        ni_repository: Consumer checkout that owns the official host package.
        build_cores: Cores exposed to the candidate Nix build; zero means all.
    """
    if build_cores < 0:
        raise LifecycleError("Nix cores 必须是非负整数")
    root = require_repo(candidate.root)
    cli = str(root / "scripts/co/cli.py")
    run(
        [sys.executable, cli, "test", "--repo", str(root)],
        cwd=root,
        capture=False,
    )
    run(
        [
            sys.executable,
            cli,
            "build",
            "--repo",
            str(root),
            "--cores",
            str(build_cores),
        ],
        cwd=root,
        capture=False,
    )
    run(
        [
            sys.executable,
            cli,
            "test-host",
            "--repo",
            str(root),
            "--ni-repo",
            str(ni_repository),
        ],
        cwd=root,
        capture=False,
    )


def upgrade(
    repository: Path,
    revision: str = "upstream/main",
    ni_repository: Path = Path("/repo/ni"),
    build_cores: int = 0,
) -> Candidate:
    """Create, finalize, test, and build a candidate without publishing it.

    Args:
        repository: Current clean candidate checkout.
        revision: Requested upstream ref; defaults to fetched upstream main.
        ni_repository: Consumer checkout that owns the official host package.
        build_cores: Cores exposed to the candidate Nix build; zero means all.

    Returns:
        The unchanged or newly validated candidate identity.
    """
    candidate = create_upgrade_candidate(
        repository,
        revision,
        ni_repository=ni_repository,
        build_cores=build_cores,
    )
    if not candidate.changed:
        return candidate
    finalize_candidate(candidate)
    validate_candidate(candidate, ni_repository, build_cores)
    return candidate
