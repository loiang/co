"""Read Codex session and spawn-edge state without mutating SQLite.

Native ``codex archive`` remains the only writer.  This module selects the
newest State DB, adapts the pinned-field schema transition, and provides fresh
snapshots for fail-closed planning and post-operation verification.
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .models import ThreadRecord

PINNED_THREAD_SECTION_ID = "01984de2-8f74-7c91-a3b2-5c5e937cf318"
STATE_DB_PATTERN = re.compile(r"state_(\d+)\.sqlite")


class StateDatabaseError(RuntimeError):
    """Report State DB absence or schema ambiguity before any archive write."""


def find_state_db(codex_home: Path) -> Path:
    """Select the highest numbered State DB belonging to one Codex profile.

    Args:
        codex_home: Codex profile root used by loaded-state and native archive.

    Returns:
        The highest-versioned ``state_N.sqlite`` path.

    Raises:
        StateDatabaseError: No numbered State DB exists in the profile.
    """
    candidates: list[tuple[int, Path]] = []
    for path in codex_home.glob("state_*.sqlite"):
        match = STATE_DB_PATTERN.fullmatch(path.name)
        if match is not None and path.is_file():
            candidates.append((int(match.group(1)), path))
    if not candidates:
        raise StateDatabaseError(f"未找到 Codex State DB: {codex_home}")
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _connect_read_only(state_db: Path) -> sqlite3.Connection:
    uri = f"{state_db.resolve().as_uri()}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise StateDatabaseError(f"无法只读打开 State DB: {state_db}") from error


def _thread_columns(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute("PRAGMA table_info(threads)").fetchall()
    columns = frozenset(str(row[1]) for row in rows)
    required = {"id", "source", "archived", "created_at_ms"}
    if not rows or not required <= columns:
        raise StateDatabaseError("State DB 缺少批量归档所需 threads 字段")
    return columns


def _selection_query(columns: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    if "thread_section_id" in columns:
        pinned = "t.thread_section_id = ?"
        parameters = (PINNED_THREAD_SECTION_ID,)
    elif "is_pinned" in columns:
        pinned = "t.is_pinned"
        parameters = ()
    else:
        raise StateDatabaseError("State DB 缺少 pinned 状态字段")
    query = f"""
        SELECT t.id, e.parent_thread_id, e.status, t.source, t.archived,
               {pinned}, COALESCE(t.created_at_ms, 0)
        FROM threads AS t
        LEFT JOIN thread_spawn_edges AS e ON e.child_thread_id = t.id
        ORDER BY t.id, e.parent_thread_id
    """
    return query, parameters


def load_thread_records(state_db: Path) -> tuple[ThreadRecord, ...]:
    """Load a consistent thread-and-edge snapshot through a read-only handle.

    ``thread_section_id`` is authoritative when present; ``is_pinned`` is used
    only for pre-section schemas.  Archived rows intentionally remain included
    so they cannot hide relationships to unarchived descendants.

    Args:
        state_db: State DB selected from the archive command's Codex profile.

    Returns:
        Thread records in deterministic database order.

    Raises:
        StateDatabaseError: SQLite cannot provide every required field.
    """
    try:
        with closing(_connect_read_only(state_db)) as connection:
            query, parameters = _selection_query(_thread_columns(connection))
            rows = connection.execute(query, parameters).fetchall()
    except StateDatabaseError:
        raise
    except sqlite3.Error as error:
        raise StateDatabaseError("State DB 缺少批量归档所需关系字段") from error
    return tuple(
        ThreadRecord(
            thread_id=str(row[0]),
            parent_id=row[1],
            edge_status=row[2],
            source=row[3],
            archived=bool(row[4]),
            pinned=bool(row[5]),
            created_at_ms=int(row[6]),
        )
        for row in rows
    )


def load_archive_statuses(
    state_db: Path, thread_ids: tuple[str, ...]
) -> dict[str, bool]:
    """Read exact archive flags for post-native verification.

    Args:
        state_db: State DB written by the native archive subprocess.
        thread_ids: Expected subtree targets from the fresh group snapshot.

    Returns:
        Existing target IDs mapped to their current archive flags.

    Raises:
        StateDatabaseError: The verification query cannot be completed.
    """
    if not thread_ids:
        return {}
    placeholders = ", ".join("?" for _thread_id in thread_ids)
    query = f"SELECT id, archived FROM threads WHERE id IN ({placeholders})"
    try:
        with closing(_connect_read_only(state_db)) as connection:
            rows = connection.execute(query, thread_ids).fetchall()
    except sqlite3.Error as error:
        raise StateDatabaseError("无法验证原生归档结果") from error
    return {str(row[0]): bool(row[1]) for row in rows}


def thread_is_archived(state_db: Path, thread_id: str) -> bool:
    """Retain the single-thread verification API for existing callers."""
    return load_archive_statuses(state_db, (thread_id,)).get(thread_id, False)
