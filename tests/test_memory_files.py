from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rcp.agents.memory_files import member_memory_root, prepare_project_memory, project_memory_root
from rcp.storage import AppStore
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.state import StateUnavailable


def old_lesson(store, lesson_id, *, human=False, updated="2026-01-01", text="Use the cache."):
    # The old writer's persisted schema, independent of the removed command implementation.
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO operational_lessons VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                lesson_id,
                "project",
                text,
                updated,
                updated,
                "human" if human else "agent",
                None if human else "turn",
                "member" if human else None,
                "Member" if human else None,
                int(human),
            ),
        )


def test_local_root_and_member_path(tmp_path):
    root = project_memory_root(tmp_path, "project")
    assert root == tmp_path / "memory/project"
    assert root.is_dir()
    assert member_memory_root(root, "member") == root / "members/member"
    for value in ("../escape", ".", "", "x/y"):
        with pytest.raises(ValueError):
            member_memory_root(root, value)
        with pytest.raises(ValueError):
            project_memory_root(tmp_path, value)


def test_migrate_old_writer_rows_once_in_order(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    old_lesson(store, "agent-new", updated="2026-03-01")
    old_lesson(store, "human-old", human=True)
    old_lesson(store, "human-new", human=True, updated="2026-02-01")
    shared, size = prepare_project_memory(store, "project")
    index = Path(shared) / "MEMORY.md"
    text = index.read_text()
    assert (
        text.index("lesson-human-new.md")
        < text.index("lesson-human-old.md")
        < text.index("lesson-agent-new.md")
    )
    assert size == index.stat().st_size
    assert (Path(shared) / "notes/lesson-human-new.md").read_text().splitlines()[
        1
    ] == "origin: lesson human-new"
    index.write_text("Edited index\n")
    prepare_project_memory(store, "project")
    assert index.read_text() == "Edited index\n"
    with store.connection() as connection:
        assert connection.execute("SELECT lesson_count FROM memory_migrations").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM operational_lessons").fetchone()[0] == 3


def test_partial_migration_preserves_existing_note(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    old_lesson(store, "existing")
    root = project_memory_root(tmp_path, "project")
    notes = root / "shared/notes"
    notes.mkdir(parents=True, exist_ok=True)
    note = notes / "lesson-existing.md"
    note.write_text("# Kept edit\n")
    prepare_project_memory(store, "project")
    assert note.read_text() == "# Kept edit\n"


def test_remote_uses_shipped_code_and_defers_marker_on_failure(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    old_lesson(store, "remote")
    stage = RemoteRunStage("example.invalid")
    monkeypatch.setattr(
        stage, "_ssh_bytes", lambda *a, **k: subprocess.CompletedProcess([], 255, b"", b"offline")
    )
    with pytest.raises(StateUnavailable):
        prepare_project_memory(store, "project", stage)
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM memory_migrations").fetchone()[0] == 0
    home = tmp_path / "remote-home"
    home.mkdir()

    def remote(arguments, *, input_data=None, **kwargs):
        return subprocess.run(
            [sys.executable, *arguments[1:]],
            input=input_data,
            capture_output=True,
            env={"HOME": str(home)},
        )

    monkeypatch.setattr(stage, "_ssh_bytes", remote)
    shared, size = prepare_project_memory(store, "project", stage)
    assert Path(shared) == home / ".rcp/memory/project/shared"
    assert size > 0
    assert (Path(shared) / "notes/lesson-remote.md").is_file()


def test_memory_refuses_redirected_shared_folder(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    root = project_memory_root(tmp_path, "project")
    outside = tmp_path / "outside"
    (outside / "notes").mkdir(parents=True)
    (root / "shared").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        prepare_project_memory(store, "project")
    assert not (outside / "MEMORY.md").exists()


def test_failure_before_index_commit_retries_without_overwriting_notes(tmp_path, monkeypatch):
    from rcp.agents import memory_file_io

    store = AppStore(tmp_path / "rcp.sqlite3")
    old_lesson(store, "crash")
    original = memory_file_io._write

    def fail_index(path, content, *, replace):
        if path.name == "MEMORY.md":
            raise OSError("interrupted before index publication")
        original(path, content, replace=replace)

    monkeypatch.setattr(memory_file_io, "_write", fail_index)
    with pytest.raises(OSError):
        prepare_project_memory(store, "project")
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM memory_migrations").fetchone()[0] == 0
    note = tmp_path / "memory/project/shared/notes/lesson-crash.md"
    note.write_text("# Preserved recovery edit\n")
    monkeypatch.setattr(memory_file_io, "_write", original)
    prepare_project_memory(store, "project")
    assert note.read_text() == "# Preserved recovery edit\n"


def test_old_database_lesson_command_result_migrates_without_receipt_changes(tmp_path):
    import gzip
    import sqlite3
    from types import SimpleNamespace

    fixture = (
        Path(__file__).parent
        / "fixtures/server_upgrade/pre-member-provider-logins-v36-f78a922/data/rcp.sqlite3.gz"
    )
    database = tmp_path / "rcp.sqlite3"
    database.write_bytes(gzip.decompress(fixture.read_bytes()))
    old_lesson(SimpleNamespace(connection=lambda: sqlite3.connect(database)), "old-command")
    receipt = ("project", "turn", "add", "key", "digest", '{"result":{"lesson_id":"old-command"}}')
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO lesson_command_receipts VALUES (?,?,?,?,?,?)", receipt)
    store = AppStore(database)
    shared, _ = prepare_project_memory(store, "project")
    assert (Path(shared) / "notes/lesson-old-command.md").read_text().endswith("Use the cache.\n")
    with store.connection() as connection:
        assert (
            tuple(connection.execute("SELECT * FROM lesson_command_receipts").fetchone()) == receipt
        )


def test_remote_io_does_not_lock_unrelated_database_writers(tmp_path, monkeypatch):
    import sqlite3

    from rcp.agents import memory_files

    store = AppStore(tmp_path / "rcp.sqlite3")

    def remote(stage, request):
        with sqlite3.connect(store.path, timeout=0.01) as connection:
            connection.execute("INSERT INTO memory_migrations VALUES ('other','now',0)")
        return {"root": "/remote/.rcp/memory/project", "index_bytes": 0}

    monkeypatch.setattr(memory_files, "_remote_memory", remote)
    prepare_project_memory(store, "project", RemoteRunStage("example.invalid"))


def test_voice_reads_only_bounded_shared_and_owner_indexes(tmp_path, monkeypatch):
    from rcp import limits
    from rcp.agents.memory_files import read_project_memory_indexes

    store = AppStore(tmp_path / "rcp.sqlite3")
    root = project_memory_root(tmp_path, "project")
    for folder, text in [
        ("shared", "shared"),
        ("members/member", "漢" * 20),
        ("members/other", "secret"),
    ]:
        path = root / folder
        path.mkdir(parents=True)
        (path / "MEMORY.md").write_text(text)
    monkeypatch.setattr(limits, "MEMORY_INDEX_MAX_BYTES", 10)
    result = read_project_memory_indexes(store, "project", "member")
    assert result == {"shared_index": "shared", "private_index": "漢" * 3, "truncated": True}
    (root / "members/member/MEMORY.md").unlink()
    (root / "members/member/MEMORY.md").symlink_to(root / "members/other/MEMORY.md")
    with pytest.raises(OSError):
        read_project_memory_indexes(store, "project", "member")


def test_voice_memory_remote_reads_the_base_using_shipped_owner(tmp_path, monkeypatch):
    from rcp.agents.memory_files import read_project_memory_indexes

    store = AppStore(tmp_path / "rcp.sqlite3")
    home = tmp_path / "remote"
    root = home / ".rcp/memory/project/shared"
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text("remote shared")
    stage = RemoteRunStage("example.invalid")

    def remote(arguments, *, input_data=None, **kwargs):
        return subprocess.run(
            [sys.executable, *arguments[1:]],
            input=input_data,
            capture_output=True,
            env={"HOME": str(home)},
        )

    monkeypatch.setattr(stage, "_ssh_bytes", remote)
    assert read_project_memory_indexes(store, "project", "member", stage) == {
        "shared_index": "remote shared",
        "truncated": False,
    }
    assert not (tmp_path / "memory").exists()
