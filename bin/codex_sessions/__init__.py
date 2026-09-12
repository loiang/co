"""Safe Codex session maintenance primitives.

The package keeps selection, State DB reads, and destructive CLI effects separate
so archive eligibility can be tested without touching real session history.
"""

from .groups import build_archive_plan
from .models import (
    ArchiveGroup,
    ArchivePlan,
    ProtectionReason,
    ThreadRecord,
)

__all__ = [
    "ArchiveGroup",
    "ArchivePlan",
    "ProtectionReason",
    "ThreadRecord",
    "build_archive_plan",
]
