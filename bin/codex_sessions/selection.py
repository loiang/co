"""Compatibility imports for the whole-session archive group model.

The implementation moved to :mod:`codex_sessions.groups` so graph decisions
remain separate from SQLite reads and native command side effects.
"""

from .groups import build_archive_plan
from .models import (
    ArchiveCall,
    ArchiveGroup,
    ArchivePlan,
    ProtectionReason,
    ThreadRecord,
)


class SelectionError(ValueError):
    """Retain the former public error type for downstream import compatibility."""


__all__ = [
    "ArchiveCall",
    "ArchiveGroup",
    "ArchivePlan",
    "ProtectionReason",
    "SelectionError",
    "ThreadRecord",
    "build_archive_plan",
]
