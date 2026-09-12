"""State DB and side-effect tests for whole-group session archiving."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from codex_sessions.cli import (  # noqa: E402
    ArchiveCommandError,
    execute_archive_plan,
    main,
)
from codex_sessions.groups import build_archive_plan  # noqa: E402
from codex_sessions.state import (  # noqa: E402
    PINNED_THREAD_SECTION_ID,
    load_thread_records,
)


def create_state_db(path: Path, *, modern: bool = True) -> None:
    """Create the relevant subset of the real Codex State DB schema."""
    section_column = "thread_section_id TEXT," if modern else ""
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            {section_column}
            created_at_ms INTEGER
        );
        CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT NOT NULL,
            child_thread_id TEXT NOT NULL PRIMARY KEY,
            status TEXT NOT NULL
        );
        """
    )
    connection.close()


def insert_thread(
    path: Path,
    thread_id: str,
    *,
    parent_id: str | None = None,
    status: str = "closed",
    source: str = '"cli"',
    archived: bool = False,
    legacy_pinned: bool = False,
    section_id: str | None = None,
    created_at_ms: int = 0,
) -> None:
    """Insert isolated test state without touching the user's profile."""
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
    if "thread_section_id" in columns:
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, source, archived, legacy_pinned, section_id, created_at_ms),
        )
    else:
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
            (thread_id, source, archived, legacy_pinned, created_at_ms),
        )
    if parent_id is not None:
        connection.execute(
            "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
            (parent_id, thread_id, status),
        )
    connection.commit()
    connection.close()


def archive_subtree(path: Path, root_id: str) -> None:
    """Simulate native recursive archive only inside a temporary database."""
    connection = sqlite3.connect(path)
    rows = connection.execute(
        """
        WITH RECURSIVE descendants(id) AS (
            VALUES (?)
            UNION ALL
            SELECT edge.child_thread_id
            FROM thread_spawn_edges AS edge
            JOIN descendants ON edge.parent_thread_id = descendants.id
        )
        SELECT id FROM descendants
        """,
        (root_id,),
    ).fetchall()
    connection.executemany("UPDATE threads SET archived = 1 WHERE id = ?", rows)
    connection.commit()
    connection.close()


def successful_native(path: Path, calls: list[list[str]]):
    """Return a subprocess fake that recursively archives each requested root."""

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["CODEX_HOME"] == str(path.parent.resolve())
        archive_subtree(path, command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def test_modern_schema_uses_thread_section_as_pinned_authority(tmp_path: Path) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "legacy-only", legacy_pinned=True)
    insert_thread(state_db, "section-pinned", section_id=PINNED_THREAD_SECTION_ID)

    records = load_thread_records(state_db)

    by_id = {record.thread_id: record for record in records}
    assert by_id["legacy-only"].pinned is False
    assert by_id["section-pinned"].pinned is True


def test_legacy_schema_falls_back_to_is_pinned(tmp_path: Path) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db, modern=False)
    insert_thread(state_db, "legacy-pinned", legacy_pinned=True)

    records = load_thread_records(state_db)

    assert records[0].pinned is True


def test_limit_skips_large_group_then_archives_smaller_group(tmp_path: Path) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "large", created_at_ms=1)
    insert_thread(state_db, "large-a", parent_id="large")
    insert_thread(state_db, "large-b", parent_id="large")
    insert_thread(state_db, "small", created_at_ms=2)
    plan = build_archive_plan(load_thread_records(state_db))
    calls: list[list[str]] = []

    with (
        patch("codex_sessions.execution.load_loaded_ids", return_value=set()),
        patch(
            "codex_sessions.execution.subprocess.run",
            side_effect=successful_native(state_db, calls),
        ),
    ):
        result = execute_archive_plan(state_db, plan, Path("/usr/bin/codex"), 2)

    assert result.archived_ids == ("small",)
    assert result.completed_group_ids == ("small",)
    assert result.skipped_group_ids == ("large",)
    assert calls == [["/usr/bin/codex", "archive", "small"]]


def test_fresh_db_membership_rechecks_group_size_against_limit(tmp_path: Path) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "main")
    plan = build_archive_plan(load_thread_records(state_db))
    insert_thread(state_db, "new-child", parent_id="main")

    with (
        patch("codex_sessions.execution.load_loaded_ids", return_value=set()),
        patch("codex_sessions.execution.subprocess.run") as native,
    ):
        result = execute_archive_plan(state_db, plan, Path("/usr/bin/codex"), 1)

    assert result.archived_ids == ()
    assert result.skipped_group_ids == ("main",)
    native.assert_not_called()


def test_edge_status_change_skips_whole_group_before_native_call(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "main")
    insert_thread(state_db, "child", parent_id="main")
    plan = build_archive_plan(load_thread_records(state_db))
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE thread_spawn_edges SET status = 'unknown'")
    connection.commit()
    connection.close()

    with (
        patch("codex_sessions.execution.load_loaded_ids", return_value=set()),
        patch("codex_sessions.execution.subprocess.run") as native,
    ):
        result = execute_archive_plan(state_db, plan, Path("/usr/bin/codex"))

    assert result.skipped_group_ids == ("main",)
    native.assert_not_called()


def test_rechecks_loaded_before_each_native_call(tmp_path: Path) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "main", archived=True)
    insert_thread(state_db, "left", parent_id="main")
    insert_thread(state_db, "right", parent_id="main")
    plan = build_archive_plan(load_thread_records(state_db))
    calls: list[list[str]] = []

    with (
        patch(
            "codex_sessions.execution.load_loaded_ids",
            side_effect=[set(), {"right"}],
        ),
        patch(
            "codex_sessions.execution.subprocess.run",
            side_effect=successful_native(state_db, calls),
        ),
        pytest.raises(ArchiveCommandError) as raised,
    ):
        execute_archive_plan(state_db, plan, Path("/usr/bin/codex"))

    assert raised.value.archived_ids == ("left",)
    assert raised.value.remaining_ids == ("right",)
    assert calls == [["/usr/bin/codex", "archive", "left"]]


def test_partial_native_archive_stops_and_rerun_selects_remainder(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "main")
    insert_thread(state_db, "child", parent_id="main")
    first_plan = build_archive_plan(load_thread_records(state_db))

    def partial(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        connection = sqlite3.connect(state_db)
        connection.execute("UPDATE threads SET archived = 1 WHERE id = 'main'")
        connection.commit()
        connection.close()
        return subprocess.CompletedProcess(command, 0, "warning", "")

    with (
        patch("codex_sessions.execution.load_loaded_ids", return_value=set()),
        patch("codex_sessions.execution.subprocess.run", side_effect=partial),
        pytest.raises(ArchiveCommandError) as raised,
    ):
        execute_archive_plan(state_db, first_plan, Path("/usr/bin/codex"))

    assert raised.value.archived_ids == ("main",)
    assert raised.value.remaining_ids == ("child",)

    rerun_plan = build_archive_plan(load_thread_records(state_db))
    calls: list[list[str]] = []
    with (
        patch("codex_sessions.execution.load_loaded_ids", return_value=set()),
        patch(
            "codex_sessions.execution.subprocess.run",
            side_effect=successful_native(state_db, calls),
        ),
    ):
        result = execute_archive_plan(state_db, rerun_plan, Path("/usr/bin/codex"))

    assert result.archived_ids == ("child",)
    assert result.completed_group_ids == ("main",)
    assert calls == [["/usr/bin/codex", "archive", "child"]]


def test_loaded_query_failure_stops_without_native_archive(tmp_path: Path) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "main")
    plan = build_archive_plan(load_thread_records(state_db))

    with (
        patch(
            "codex_sessions.execution.load_loaded_ids",
            side_effect=RuntimeError("unavailable"),
        ),
        patch("codex_sessions.execution.subprocess.run") as native,
        pytest.raises(RuntimeError, match="unavailable"),
    ):
        execute_archive_plan(state_db, plan, Path("/usr/bin/codex"))

    native.assert_not_called()


def test_native_process_gets_matching_codex_home(tmp_path: Path) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "main")
    plan = build_archive_plan(load_thread_records(state_db))
    captured_environment: dict[str, str] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        captured_environment.update(environment)
        archive_subtree(state_db, command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch("codex_sessions.execution.load_loaded_ids", return_value=set()),
        patch("codex_sessions.execution.subprocess.run", side_effect=run),
        patch.dict(os.environ, {"CODEX_HOME": "/wrong/home"}),
    ):
        execute_archive_plan(state_db, plan, Path("/usr/bin/codex"))

    assert captured_environment["CODEX_HOME"] == str(tmp_path.resolve())


def test_preview_reports_group_counts_limit_and_protection_reasons(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_db = tmp_path / "state_5.sqlite"
    create_state_db(state_db)
    insert_thread(state_db, "protected", created_at_ms=1)
    insert_thread(state_db, "loaded-child", parent_id="protected")
    insert_thread(state_db, "too-large", created_at_ms=2)
    insert_thread(state_db, "too-large-child", parent_id="too-large")
    insert_thread(state_db, "small", created_at_ms=3)

    with (
        patch(
            "codex_sessions.cli.load_loaded_ids", return_value={"loaded-child"}
        ) as loaded,
        patch("codex_sessions.execution.subprocess.run") as native,
    ):
        exit_code = main(["--preview", "--limit", "1", "--codex-home", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "组 3；可归档组 1；未归档线程 1；保护组 1" in output
    assert "root=protected members=2 targets=2 status=protected:loaded" in output
    assert "root=too-large members=2 targets=2 status=limit-skip" in output
    assert "root=small members=1 targets=1 status=eligible" in output
    loaded.assert_called_once_with(tmp_path.resolve())
    native.assert_not_called()
