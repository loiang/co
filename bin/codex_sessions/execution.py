"""Execute whole-session archive groups through the native Codex CLI.

Each native call is preceded by a fresh loaded-thread and State DB snapshot.
The command never writes SQLite directly and stops immediately if native Codex
fails, archives only part of a subtree, or safety state changes mid-group.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .groups import build_archive_plan
from .loaded import load_loaded_ids
from .models import ArchiveGroup, ArchivePlan
from .state import load_archive_statuses, load_thread_records


class ArchiveCommandError(RuntimeError):
    """Report a stopped batch together with confirmed and remaining targets."""

    def __init__(
        self,
        message: str,
        archived_ids: tuple[str, ...] = (),
        remaining_ids: tuple[str, ...] = (),
    ) -> None:
        """Preserve resumable state without pretending the group was atomic."""
        super().__init__(message)
        self.archived_ids = archived_ids
        self.remaining_ids = remaining_ids


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """Summarize confirmed threads and completed or skipped whole groups."""

    archived_ids: tuple[str, ...]
    completed_group_ids: tuple[str, ...]
    skipped_group_ids: tuple[str, ...]


def _fresh_group(state_db: Path, root_id: str) -> ArchiveGroup | None:
    codex_home = state_db.parent.resolve()
    loaded_ids = load_loaded_ids(codex_home)
    plan = build_archive_plan(load_thread_records(state_db), loaded_ids)
    try:
        return plan.group_for(root_id)
    except KeyError:
        return None


def _native_archive(
    codex_bin: Path, codex_home: Path, thread_id: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        [str(codex_bin), "archive", thread_id],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _remaining_targets(
    state_db: Path, target_ids: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    statuses = load_archive_statuses(state_db, target_ids)
    archived = tuple(
        thread_id for thread_id in target_ids if statuses.get(thread_id) is True
    )
    remaining = tuple(
        thread_id for thread_id in target_ids if statuses.get(thread_id) is not True
    )
    return archived, remaining


def _state_change_error(
    root_id: str,
    archived: list[str],
    group: ArchiveGroup | None,
) -> ArchiveCommandError:
    remaining = () if group is None else group.target_ids
    return ArchiveCommandError(
        f"组 {root_id} 在归档期间状态变化，批次已停止",
        tuple(archived),
        remaining,
    )


def _archive_one_call(
    state_db: Path,
    codex_bin: Path,
    group: ArchiveGroup,
    archived: list[str],
) -> None:
    call = group.archive_calls[0]
    result = _native_archive(codex_bin, state_db.parent.resolve(), call.thread_id)
    confirmed, call_remaining = _remaining_targets(state_db, call.target_ids)
    archived.extend(thread_id for thread_id in confirmed if thread_id not in archived)
    remaining = tuple(
        thread_id for thread_id in group.target_ids if thread_id not in confirmed
    )
    if result.returncode:
        raise ArchiveCommandError(
            f"原生归档 {call.thread_id} 失败（退出码 {result.returncode}）",
            tuple(archived),
            remaining,
        )
    if call_remaining:
        raise ArchiveCommandError(
            f"原生归档 {call.thread_id} 未归档完整子树",
            tuple(archived),
            remaining,
        )


def _archive_group(
    state_db: Path,
    root_id: str,
    codex_bin: Path,
    limit: int | None,
    archived: list[str],
) -> bool:
    started_count = len(archived)
    while True:
        group = _fresh_group(state_db, root_id)
        if group is None or not group.target_ids:
            return len(archived) > started_count
        if group.protection_reasons:
            if len(archived) > started_count:
                raise _state_change_error(root_id, archived, group)
            return False
        if limit is not None and len(archived) + len(group.target_ids) > limit:
            if len(archived) > started_count:
                raise _state_change_error(root_id, archived, group)
            return False
        if not group.archive_calls:
            raise _state_change_error(root_id, archived, group)
        _archive_one_call(state_db, codex_bin, group, archived)


def execute_archive_plan(
    state_db: Path,
    plan: ArchivePlan,
    codex_bin: Path,
    limit: int | None = None,
) -> ArchiveResult:
    """Archive eligible groups without splitting one group to satisfy a limit.

    Groups retain the initial stable order, but membership, loaded state,
    protection reasons, remaining limit, and archive-call roots are rebuilt
    before every native invocation.  A too-large group is skipped so a later
    smaller group may still fit.

    Args:
        state_db: Read-only verification source under the target Codex profile.
        plan: Initial snapshot that determines stable group iteration order.
        codex_bin: Native Codex executable used for supported archive writes.
        limit: Maximum number of previously unarchived targets in this run.

    Returns:
        Confirmed archived target IDs and skipped group root IDs.

    Raises:
        ArchiveCommandError: A group becomes unsafe or native archive is partial.
        RuntimeError: Loaded-thread state cannot be queried completely.
    """
    archived: list[str] = []
    completed_groups: list[str] = []
    skipped: list[str] = []
    for planned_group in plan.eligible_groups:
        completed = _archive_group(
            state_db,
            planned_group.root_id,
            codex_bin,
            limit,
            archived,
        )
        if completed:
            completed_groups.append(planned_group.root_id)
        else:
            skipped.append(planned_group.root_id)
    return ArchiveResult(tuple(archived), tuple(completed_groups), tuple(skipped))
