"""Immutable data contracts shared by archive grouping and execution.

Keeping these declarations free of I/O lets tests and compatibility imports use
the archive model without opening SQLite or connecting to the app server.
"""

from dataclasses import dataclass
from enum import StrEnum


class ProtectionReason(StrEnum):
    """Name stable, user-visible reasons why a whole group cannot be archived."""

    LOADED = "loaded"
    PINNED = "pinned"
    EDGE_NOT_CLOSED = "edge-not-closed"
    ORPHAN_SUBAGENT = "orphan-subagent"
    MISSING_PARENT = "missing-parent"
    AMBIGUOUS_RELATION = "ambiguous-relation"
    CYCLE = "cycle"


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """Carry the read-only State DB fields needed to reconstruct session trees."""

    thread_id: str
    parent_id: str | None
    edge_status: str | None
    source: str | None
    archived: bool
    pinned: bool
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ArchiveCall:
    """Describe one native archive root and every target it must archive."""

    thread_id: str
    target_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchiveGroup:
    """Represent one main session and all descendants as an atomic decision."""

    root_id: str
    member_ids: tuple[str, ...]
    target_ids: tuple[str, ...]
    archive_calls: tuple[ArchiveCall, ...]
    protection_reasons: tuple[ProtectionReason, ...]
    created_at_ms: int

    @property
    def eligible(self) -> bool:
        """Allow execution only when targets exist and no safety reason applies."""
        return bool(self.target_ids) and not self.protection_reasons


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    """Expose deterministically ordered whole-session archive decisions."""

    groups: tuple[ArchiveGroup, ...]

    @property
    def eligible_groups(self) -> tuple[ArchiveGroup, ...]:
        """Return complete groups that are safe candidates in this snapshot."""
        return tuple(group for group in self.groups if group.eligible)

    def group_for(self, root_id: str) -> ArchiveGroup:
        """Find a group by its stable root identity for fresh-plan comparison.

        Args:
            root_id: Root thread identifier assigned while building the graph.

        Returns:
            The matching archive group.

        Raises:
            KeyError: The fresh State DB snapshot no longer contains that root.
        """
        for group in self.groups:
            if group.root_id == root_id:
                return group
        raise KeyError(root_id)
