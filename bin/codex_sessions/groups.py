"""Build fail-closed archive groups from Codex thread relationships.

Archived records remain in the graph because they still connect a main session
to live descendants.  Selection happens for a complete connected session tree,
so one loaded, pinned, active, or ambiguous member protects the whole tree.
"""

from collections import defaultdict
from dataclasses import dataclass

from .models import (
    ArchiveCall,
    ArchiveGroup,
    ArchivePlan,
    ProtectionReason,
    ThreadRecord,
)


@dataclass(slots=True)
class _Graph:
    records: dict[str, ThreadRecord]
    parents: dict[str, str]
    children: dict[str, set[str]]
    adjacency: dict[str, set[str]]
    missing_parent: set[str]
    ambiguous: set[str]


def _record_key(record: ThreadRecord) -> tuple[int, str]:
    return (record.created_at_ms, record.thread_id)


def _build_graph(rows: tuple[ThreadRecord, ...] | list[ThreadRecord]) -> _Graph:
    records: dict[str, ThreadRecord] = {}
    relations: dict[str, set[tuple[str | None, str | None]]] = defaultdict(set)
    occurrences: dict[str, int] = defaultdict(int)
    for row in rows:
        records.setdefault(row.thread_id, row)
        relations[row.thread_id].add((row.parent_id, row.edge_status))
        occurrences[row.thread_id] += 1
    ambiguous = {
        thread_id for thread_id, values in relations.items() if len(values) > 1
    }
    ambiguous.update(thread_id for thread_id, count in occurrences.items() if count > 1)

    parents: dict[str, str] = {}
    children: dict[str, set[str]] = defaultdict(set)
    adjacency = {thread_id: set() for thread_id in records}
    missing_parent: set[str] = set()
    for thread_id, relation_set in relations.items():
        known_parents = {
            parent for parent, _status in relation_set if parent is not None
        }
        if len(known_parents) == 1:
            parents[thread_id] = next(iter(known_parents))
        for parent_id in known_parents:
            if parent_id not in records:
                missing_parent.add(thread_id)
                continue
            adjacency[thread_id].add(parent_id)
            adjacency[parent_id].add(thread_id)
            children[parent_id].add(thread_id)
    return _Graph(records, parents, children, adjacency, missing_parent, ambiguous)


def _components(graph: _Graph) -> list[set[str]]:
    remaining = set(graph.records)
    components: list[set[str]] = []
    while remaining:
        pending = [min(remaining)]
        component: set[str] = set()
        while pending:
            thread_id = pending.pop()
            if thread_id in component:
                continue
            component.add(thread_id)
            pending.extend(graph.adjacency[thread_id] - component)
        remaining -= component
        components.append(component)
    return components


def _has_cycle(component: set[str], parents: dict[str, str]) -> bool:
    for start in component:
        seen: set[str] = set()
        current: str | None = start
        while current in component:
            if current in seen:
                return True
            seen.add(current)
            current = parents.get(current)
    return False


def _choose_root(component: set[str], graph: _Graph) -> str:
    roots = [
        thread_id
        for thread_id in component
        if graph.parents.get(thread_id) not in component
    ]
    candidates = roots or list(component)
    return min(candidates, key=lambda thread_id: _record_key(graph.records[thread_id]))


def _ordered_members(
    root_id: str, component: set[str], graph: _Graph
) -> tuple[str, ...]:
    ordered: list[str] = []
    pending = [root_id]
    while pending:
        thread_id = pending.pop()
        if thread_id in ordered:
            continue
        ordered.append(thread_id)
        descendants = graph.children.get(thread_id, set()) & component
        pending.extend(
            sorted(
                descendants,
                key=lambda item: _record_key(graph.records[item]),
                reverse=True,
            )
        )
    unseen = component - set(ordered)
    ordered.extend(sorted(unseen, key=lambda item: _record_key(graph.records[item])))
    return tuple(ordered)


def _is_subagent(source: str | None) -> bool:
    return source is not None and "subagent" in source.casefold().replace("_", "")


def _protection_reasons(
    component: set[str], root_id: str, graph: _Graph, loaded_ids: frozenset[str]
) -> tuple[ProtectionReason, ...]:
    records = [graph.records[thread_id] for thread_id in component]
    reasons: set[ProtectionReason] = set()
    if component & loaded_ids:
        reasons.add(ProtectionReason.LOADED)
    if any(record.pinned for record in records):
        reasons.add(ProtectionReason.PINNED)
    if any(
        not record.archived
        and record.parent_id is not None
        and record.edge_status != "closed"
        for record in records
    ):
        reasons.add(ProtectionReason.EDGE_NOT_CLOSED)
    if graph.records[root_id].parent_id is None and _is_subagent(
        graph.records[root_id].source
    ):
        reasons.add(ProtectionReason.ORPHAN_SUBAGENT)
    if component & graph.missing_parent:
        reasons.add(ProtectionReason.MISSING_PARENT)
    if component & graph.ambiguous:
        reasons.add(ProtectionReason.AMBIGUOUS_RELATION)
    if _has_cycle(component, graph.parents):
        reasons.add(ProtectionReason.CYCLE)
    return tuple(reason for reason in ProtectionReason if reason in reasons)


def _archive_calls(
    target_ids: tuple[str, ...], graph: _Graph
) -> tuple[ArchiveCall, ...]:
    targets = set(target_ids)
    roots: list[str] = []
    for thread_id in target_ids:
        ancestor = graph.parents.get(thread_id)
        while ancestor is not None and ancestor not in targets:
            ancestor = graph.parents.get(ancestor)
        if ancestor is None:
            roots.append(thread_id)
    calls = []
    for root_id in roots:
        covered = tuple(
            thread_id
            for thread_id in target_ids
            if _descends_from(thread_id, root_id, graph.parents)
        )
        calls.append(ArchiveCall(root_id, covered))
    return tuple(calls)


def _descends_from(thread_id: str, root_id: str, parents: dict[str, str]) -> bool:
    current: str | None = thread_id
    seen: set[str] = set()
    while current is not None and current not in seen:
        if current == root_id:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def _make_group(
    component: set[str], graph: _Graph, loaded_ids: frozenset[str]
) -> ArchiveGroup:
    root_id = _choose_root(component, graph)
    member_ids = _ordered_members(root_id, component, graph)
    target_ids = tuple(
        thread_id for thread_id in member_ids if not graph.records[thread_id].archived
    )
    return ArchiveGroup(
        root_id=root_id,
        member_ids=member_ids,
        target_ids=target_ids,
        archive_calls=_archive_calls(target_ids, graph),
        protection_reasons=_protection_reasons(component, root_id, graph, loaded_ids),
        created_at_ms=graph.records[root_id].created_at_ms,
    )


def build_archive_plan(
    records: tuple[ThreadRecord, ...] | list[ThreadRecord],
    loaded_ids: set[str] | frozenset[str] = frozenset(),
) -> ArchivePlan:
    """Group all records and apply whole-tree protection conservatively.

    Args:
        records: Complete thread/edge snapshot, including archived threads.
        loaded_ids: Threads reported as currently loaded by the local app server.

    Returns:
        Every group in stable root creation/id order, including protected groups.
    """
    graph = _build_graph(records)
    loaded = frozenset(loaded_ids)
    groups = [_make_group(component, graph, loaded) for component in _components(graph)]
    groups.sort(key=lambda group: (group.created_at_ms, group.root_id))
    return ArchivePlan(tuple(groups))
