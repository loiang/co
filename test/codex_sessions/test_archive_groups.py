"""Business tests for whole-tree Codex archive grouping."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from codex_sessions.groups import (  # noqa: E402
    ProtectionReason,
    ThreadRecord,
    build_archive_plan,
)


def record(
    thread_id: str,
    *,
    parent_id: str | None = None,
    status: str | None = None,
    source: str = '"cli"',
    archived: bool = False,
    pinned: bool = False,
    created_at_ms: int = 0,
) -> ThreadRecord:
    """Create terse records so each test exposes only relevant graph facts."""
    return ThreadRecord(
        thread_id=thread_id,
        parent_id=parent_id,
        edge_status=status,
        source=source,
        archived=archived,
        pinned=pinned,
        created_at_ms=created_at_ms,
    )


def subagent(
    thread_id: str, parent_id: str | None, status: str | None, **values: object
) -> ThreadRecord:
    """Create a record using the JSON-shaped source stored by Codex."""
    return record(
        thread_id,
        parent_id=parent_id,
        status=status,
        source='{"subAgent":{"threadSpawn":{}}}',
        **values,
    )


def test_loaded_descendant_protects_main_session_and_all_descendants() -> None:
    records = [
        record("main", created_at_ms=1),
        subagent("parent", "main", "closed"),
        subagent("loaded", "parent", "closed"),
        subagent("sibling", "main", "closed"),
        record("other", created_at_ms=2),
    ]

    plan = build_archive_plan(records, {"loaded"})

    protected = plan.group_for("main")
    assert protected.member_ids == ("main", "parent", "loaded", "sibling")
    assert protected.protection_reasons == (ProtectionReason.LOADED,)
    assert tuple(group.root_id for group in plan.eligible_groups) == ("other",)


def test_pinned_below_archived_middle_protects_original_whole_group() -> None:
    records = [
        record("main"),
        subagent("archived-middle", "main", "closed", archived=True),
        subagent("pinned", "archived-middle", "closed", pinned=True),
        subagent("sibling", "main", "closed"),
    ]

    group = build_archive_plan(records).group_for("main")

    assert group.target_ids == ("main", "pinned", "sibling")
    assert group.protection_reasons == (ProtectionReason.PINNED,)


def test_unknown_open_edge_protects_group_but_edge_less_normal_root_is_legal() -> None:
    records = [
        record("main", created_at_ms=2),
        subagent("future-status", "main", "future-state"),
        record("ordinary-root", created_at_ms=1),
    ]

    plan = build_archive_plan(records)

    assert plan.group_for("main").protection_reasons == (
        ProtectionReason.EDGE_NOT_CLOSED,
    )
    assert tuple(group.root_id for group in plan.eligible_groups) == ("ordinary-root",)


def test_orphan_subagent_and_missing_parent_subtree_are_protected() -> None:
    records = [
        subagent("orphan", None, None, created_at_ms=1),
        subagent("missing-parent", "absent", "closed", created_at_ms=2),
        subagent("descendant", "missing-parent", "closed"),
    ]

    plan = build_archive_plan(records)

    assert plan.group_for("orphan").protection_reasons == (
        ProtectionReason.ORPHAN_SUBAGENT,
    )
    assert plan.group_for("missing-parent").member_ids == (
        "missing-parent",
        "descendant",
    )
    assert plan.group_for("missing-parent").protection_reasons == (
        ProtectionReason.MISSING_PARENT,
    )
    assert plan.eligible_groups == ()


def test_cycle_is_one_protected_component_not_independent_safe_roots() -> None:
    records = [
        subagent("a", "c", "closed", created_at_ms=1),
        subagent("b", "a", "closed"),
        subagent("c", "b", "closed"),
    ]

    plan = build_archive_plan(records)

    assert len(plan.groups) == 1
    assert set(plan.groups[0].member_ids) == {"a", "b", "c"}
    assert plan.groups[0].protection_reasons == (ProtectionReason.CYCLE,)


def test_groups_sort_by_root_creation_then_id_and_keep_archived_links() -> None:
    records = [
        record("z", created_at_ms=1),
        record("b", created_at_ms=1),
        subagent("b-child", "b", "closed", archived=True),
        subagent("b-grandchild", "b-child", "closed"),
        record("a", created_at_ms=1),
    ]

    plan = build_archive_plan(records)

    assert tuple(group.root_id for group in plan.eligible_groups) == ("a", "b", "z")
    assert plan.group_for("b").archive_calls[0].thread_id == "b"
    assert plan.group_for("b").archive_calls[0].target_ids == (
        "b",
        "b-grandchild",
    )


def test_archived_root_creates_one_native_call_per_unarchived_subtree() -> None:
    records = [
        record("main", archived=True),
        subagent("archived-middle", "main", "closed", archived=True),
        subagent("left", "archived-middle", "closed"),
        subagent("left-child", "left", "closed"),
        subagent("right", "main", "closed"),
    ]

    group = build_archive_plan(records).group_for("main")

    assert tuple(call.thread_id for call in group.archive_calls) == ("left", "right")
    assert group.archive_calls[0].target_ids == ("left", "left-child")
    assert group.archive_calls[1].target_ids == ("right",)
