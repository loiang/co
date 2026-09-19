"""Stream merge diagnostics while preserving recoverable candidate worktrees."""

import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from common import LifecycleError, git

if TYPE_CHECKING:
    from git_lifecycle import Candidate


class CandidateConflict(LifecycleError):
    """Preserve a conflicted merge and expose deterministic continuation steps."""


def _merge_upstream(
    source_root: Path,
    source_head: str,
    candidate: "Candidate",
    ni_repository: Path,
    build_cores: int,
) -> None:
    candidate.root.parent.mkdir(parents=True, exist_ok=True)
    git(
        source_root,
        "worktree",
        "add",
        "-b",
        candidate.branch,
        str(candidate.root),
        source_head,
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(candidate.root),
            "merge",
            "--no-edit",
            candidate.upstream_rev,
        ],
        cwd=candidate.root,
        check=False,
        # Let Git write progress and conflict diagnostics directly to the terminal.
        capture_output=False,
        text=True,
    )
    if not result.returncode:
        return
    state = subprocess.run(
        ["git", "-C", str(candidate.root), "rev-parse", "--verify", "MERGE_HEAD"],
        cwd=candidate.root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if state.returncode:
        raise LifecycleError(
            f"upstream merge 失败 (exit {result.returncode})；"
            f"请查看 Git 诊断；worktree 已保留: {candidate.root}"
        )
    quoted_worktree = shlex.quote(str(candidate.root))
    quoted_target = shlex.quote(candidate.upstream_rev)
    candidate_cli = shlex.quote(str(candidate.root / "scripts/co/cli.py"))
    command = (
        f"GIT_EDITOR=true git -C {quoted_worktree} merge --continue\n"
        f"python3 {candidate_cli} upgrade-finalize --repo {quoted_worktree} "
        f"--upstream-rev {quoted_target} "
        f"--ni-repo {shlex.quote(str(ni_repository.resolve()))} "
        f"--cores {build_cores}"
    )
    raise CandidateConflict(
        f"upstream merge 冲突；worktree 已保留: {candidate.root}\n"
        f"Git 冲突诊断已输出到终端。\n续做命令:\n{command}"
    )
