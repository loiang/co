"""CLI orchestration for fail-closed, whole-session Codex archiving.

Preview and execution use the same profile, loaded-thread query, State DB, and
grouping rules.  Only execution resolves and invokes native ``codex archive``.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

from .execution import ArchiveCommandError, ArchiveResult, execute_archive_plan
from .groups import build_archive_plan
from .loaded import load_loaded_ids
from .models import ArchivePlan
from .state import StateDatabaseError, find_state_db, load_thread_records

__all__ = [
    "ArchiveCommandError",
    "ArchiveResult",
    "entrypoint",
    "execute_archive_plan",
    "main",
]

_PREVIEW_DETAIL_GROUP_LIMIT = 50


def _parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按主会话及其全部后代整组归档安全的 Codex 会话"
    )
    parser.add_argument(
        "--preview",
        "--dry-run",
        dest="preview",
        action="store_true",
        help="显示组、线程数和保护原因，不执行归档",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="本次最多归档的未归档线程数；不足容纳整组时跳过该组",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="目标 Codex profile；默认使用 CODEX_HOME 或 ~/.codex",
    )
    parser.add_argument("--codex-bin", type=Path, help=argparse.SUPPRESS)
    parsed = parser.parse_args(arguments)
    if parsed.limit is not None and parsed.limit < 1:
        parser.error("--limit 必须大于 0")
    return parsed


def _initial_plan(codex_home: Path, state_db: Path) -> ArchivePlan:
    loaded_ids = load_loaded_ids(codex_home)
    return build_archive_plan(load_thread_records(state_db), loaded_ids)


def _preview_status(plan: ArchivePlan, limit: int | None) -> dict[str, str]:
    remaining = limit
    statuses: dict[str, str] = {}
    for group in plan.groups:
        if group.protection_reasons:
            reasons = ",".join(reason.value for reason in group.protection_reasons)
            statuses[group.root_id] = f"protected:{reasons}"
        elif not group.target_ids:
            statuses[group.root_id] = "archived"
        elif remaining is not None and len(group.target_ids) > remaining:
            statuses[group.root_id] = "limit-skip"
        else:
            statuses[group.root_id] = "eligible"
            if remaining is not None:
                remaining -= len(group.target_ids)
    return statuses


def _print_preview(plan: ArchivePlan, limit: int | None) -> None:
    statuses = _preview_status(plan, limit)
    eligible = sum(status == "eligible" for status in statuses.values())
    target_count = sum(
        len(group.target_ids)
        for group in plan.groups
        if statuses[group.root_id] == "eligible"
    )
    protected = sum(status.startswith("protected:") for status in statuses.values())
    print(
        f"组 {len(plan.groups)}；可归档组 {eligible}；"
        f"未归档线程 {target_count}；保护组 {protected}"
    )
    if len(plan.groups) > _PREVIEW_DETAIL_GROUP_LIMIT:
        _print_preview_summary(plan, statuses)
        return
    for group in plan.groups:
        print(
            f"root={group.root_id} members={len(group.member_ids)} "
            f"targets={len(group.target_ids)} status={statuses[group.root_id]}"
        )


def _print_preview_summary(plan: ArchivePlan, statuses: dict[str, str]) -> None:
    buckets: dict[str, list[int]] = {}
    for group in plan.groups:
        status = statuses[group.root_id]
        counts = buckets.setdefault(status, [0, 0, 0])
        counts[0] += 1
        counts[1] += len(group.member_ids)
        counts[2] += len(group.target_ids)
    for status in sorted(buckets):
        groups, members, targets = buckets[status]
        print(f"status={status} groups={groups} members={members} targets={targets}")
    for group in plan.groups:
        if statuses[group.root_id] == "eligible":
            print(
                f"root={group.root_id} members={len(group.member_ids)} "
                f"targets={len(group.target_ids)} status=eligible"
            )


def _codex_home(args: argparse.Namespace) -> Path:
    configured = args.codex_home or os.environ.get("CODEX_HOME")
    path = Path(configured) if configured is not None else Path.home() / ".codex"
    return path.expanduser().resolve(strict=False)


def main(arguments: list[str] | None = None) -> int:
    """Run preview or native whole-session archive for one explicit profile.

    Args:
        arguments: Optional argv override used by tests and embedding callers.

    Returns:
        Zero after a complete preview or verified archive execution.

    Raises:
        ArchiveCommandError: Native Codex is unavailable or the batch stops.
        StateDatabaseError: Required read-only session state is unavailable.
        RuntimeError: Loaded-thread state cannot be proven safely.
    """
    args = _parse_args(arguments)
    codex_home = _codex_home(args)
    state_db = find_state_db(codex_home)
    plan = _initial_plan(codex_home, state_db)
    if args.preview:
        _print_preview(plan, args.limit)
        return 0
    selected_bin = args.codex_bin or shutil.which("codex")
    if selected_bin is None:
        raise ArchiveCommandError("缺少必需命令: codex")
    result = execute_archive_plan(state_db, plan, Path(selected_bin), args.limit)
    print(
        f"完成组 {len(result.completed_group_ids)}；"
        f"已归档线程 {len(result.archived_ids)}；"
        f"跳过整组 {len(result.skipped_group_ids)}"
    )
    return 0


def entrypoint() -> int:
    """Translate expected operational failures into a concise CLI exit status."""
    try:
        return main()
    except ArchiveCommandError as error:
        print(f"codex-archive-subagents: {error}", file=sys.stderr)
        print(
            f"已确认归档 {len(error.archived_ids)}: {','.join(error.archived_ids) or '-'}",
            file=sys.stderr,
        )
        print(
            f"仍未归档 {len(error.remaining_ids)}: {','.join(error.remaining_ids) or '-'}",
            file=sys.stderr,
        )
        return 1
    except (StateDatabaseError, RuntimeError, OSError) as error:
        print(f"codex-archive-subagents: {error}", file=sys.stderr)
        return 1
