from __future__ import annotations

import uuid

import pytest

from rcp.storage import AppStore, ProjectRecord
from rcp.storage.digest import append_digest_event
from rcp.transfer.records import TRANSFER_EXCLUDED_PROJECT_TABLES

from .test_compute_jobs_storage import job_record


@pytest.fixture
def digest_store(tmp_path):
    store, _ = AppStore.initialize_team_space(tmp_path / "digest.sqlite3", "Team")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="/tmp/project/research.yaml",
            name="Project",
            state_location="/tmp/project/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    members = [store.preprovision_team_member(name).user_id for name in ("One", "Two")]
    for member in members:
        store.seat_project_member("project", member)
    with store.connection() as conn:
        conn.execute("INSERT INTO digest_heads VALUES(?,?,?,?)", ("project", "main", 0, ""))
    return store, members


def event(store):
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return append_digest_event(
            conn,
            project_id="project",
            kind="job_ended",
            item_id="job",
            created_at=store.now(),
            payload={
                "title": "Job",
                "status": "exited",
                "deep_link": "#/projects/project?view=runs",
            },
        )


def test_marks_baseline_monotonic_ahead_and_member_independence(digest_store):
    store, (one, two) = digest_store
    initial = event(store)
    assert store.digest_event_batches(["project"], one) == {}
    mark, cursor, events = store.digest_snapshot("project", one)
    assert (mark["seq"], cursor, events) == (initial, initial, [])
    store.digest_snapshot("project", two)
    next_seq = event(store)
    caught = store.catch_up_digest("project", one, next_seq)
    assert store.catch_up_digest("project", one, initial) == caught
    with pytest.raises(ValueError):
        store.catch_up_digest("project", one, next_seq + 1)
    assert store.digest_snapshot("project", one)[2] == []
    assert [e["seq"] for e in store.digest_snapshot("project", two)[2]] == [next_seq]
    store.leave_project("project", one)
    with pytest.raises(KeyError):
        store.digest_snapshot("project", one)


def test_events_follow_commit_order_and_rollback_with_source(digest_store, monkeypatch):
    store, (one, _) = digest_store
    store.create_compute_job(job_record())
    _, displayed, _ = store.digest_snapshot("project", one)
    store.catch_up_digest("project", one, displayed)
    with monkeypatch.context() as patch:

        def fail_append(*args, **kwargs):
            raise RuntimeError("rollback")

        patch.setattr("rcp.storage.compute_jobs.append_digest_event", fail_append)
        with pytest.raises(RuntimeError):
            store.record_compute_job_refresh(
                "job-1", status="exited", ended_at="2000-01-01T00:00:00Z"
            )
    assert store.compute_job("job-1").status == "running"
    assert store.digest_snapshot("project", one)[2] == []
    store.record_compute_job_refresh("job-1", status="exited", ended_at="2000-01-01T00:00:00Z")
    events = store.digest_snapshot("project", one)[2]
    assert [(e["kind"], e["item_id"]) for e in events] == [("job_ended", "job-1")]
    assert events[0]["seq"] > displayed


def test_digest_storage_transfer_rewrite_and_delete(digest_store):
    store, (one, _) = digest_store
    displayed = event(store)
    store.digest_snapshot("project", one)
    with store.connection() as conn:
        conn.execute(
            "UPDATE digest_heads SET revision=2,patch_id='head' WHERE project_id='project'"
        )
    tables = {"digest_events", "digest_marks", "digest_heads"}
    assert tables <= TRANSFER_EXCLUDED_PROJECT_TABLES
    target = str(uuid.uuid4())
    store.migrate_project_identity("project", target, store.space_id)
    with store.connection() as conn:
        for table in tables:
            assert conn.execute(f"SELECT project_id FROM {table}").fetchone()[0] == target
        assert target in conn.execute("SELECT payload_json FROM digest_events").fetchone()[0]
    counts = store.delete_project_records(target)
    assert all(counts[table] == 1 for table in tables)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM digest_events").fetchone()[0] == 0
    store.upsert_project(
        ProjectRecord(
            project_id="remaining",
            locator="/tmp/remaining/research.yaml",
            name="Remaining",
            state_location="/tmp/remaining/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    with store.connection() as conn:
        conn.execute("INSERT INTO digest_heads VALUES(?,?,?,?)", ("remaining", "main", 0, ""))
    store.seat_project_member("remaining", one)
    mark, cursor, _ = store.digest_snapshot("remaining", one)
    assert cursor == mark["seq"] == displayed
    assert store.catch_up_digest("remaining", one, cursor) == mark
