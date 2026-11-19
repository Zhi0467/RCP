from __future__ import annotations

import json

import pytest

from rcp.agents import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.bulletin import stage_bulletin_pointer

from .test_lessons_store import store as store


def stage(store, tmp_path):
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    stage_bulletin_pointer(execution, tmp_path, None)
    return (tmp_path / "workspace" / "bulletin.md").read_text()


def member_post(store, key, text="note"):
    return store.mutate_bulletin_post(
        "project", "main", user_id=store.local_owner.user_id, key=key, text=text
    )


def delta(store):
    with store.connection() as connection:
        return dict(connection.execute("SELECT * FROM bulletin_deltas").fetchone())


def test_delta_contiguous_cursor_and_replay(store, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_DELTA_MAX_POSTS", 2)
    posts = [member_post(store, str(index)) for index in range(3)]
    rendered = stage(store, tmp_path)
    assert posts[0]["post_id"] in rendered
    assert posts[1]["post_id"] in rendered
    assert posts[2]["post_id"] not in rendered
    with store.connection() as connection:
        cursor = connection.execute("SELECT through_seq FROM bulletin_actor_reads").fetchone()[0]
        assert (
            cursor
            == connection.execute(
                "SELECT sequence FROM bulletin_posts WHERE post_id=?", (posts[1]["post_id"],)
            ).fetchone()[0]
        )
    member_post(store, "later")
    replayed = stage(store, tmp_path)
    assert all(post["post_id"] in replayed for post in posts[:2])
    assert posts[2]["post_id"] not in replayed
    assert delta(store)["through_seq"] == cursor


def test_delta_excludes_own_posts(store, tmp_path):
    own = store.execute_bulletin_command("project", "turn", "post", key="own", text="own")["post"]
    other = member_post(store, "other")
    rendered = stage(store, tmp_path)
    assert own["post_id"] not in rendered
    assert other["post_id"] in rendered


def test_oversized_delta_advances_and_stops_at_boundary(store, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.runs.bulletin.BULLETIN_RENDER_MAX_BYTES", 1800)
    big = member_post(store, "big", "界" * 7000)
    small = member_post(store, "small")
    rendered = stage(store, tmp_path)
    assert len(rendered.encode()) <= 1800
    assert big["post_id"] in rendered
    assert small["post_id"] not in rendered
    assert delta(store)["through_seq"] > delta(store)["from_seq"]
    records = [json.loads(block.split("\n```", 1)[0]) for block in rendered.split("```json\n")[1:]]
    assert any(record.get("truncated") for record in records)


def test_first_delta_uses_recent_page(store, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_PAGE_SIZE", 2)
    posts = [member_post(store, str(index)) for index in range(4)]
    rendered = stage(store, tmp_path)
    assert all(post["post_id"] not in rendered for post in posts[:2])
    assert all(post["post_id"] in rendered for post in posts[2:])


def test_old_database_creates_actor_tables_without_changing_human_reads(store, tmp_path):
    from rcp.storage import AppStore

    post = member_post(store, "old")
    store.mark_bulletin_read("project", "main", store.local_owner.user_id, post["post_id"])
    with store.connection() as connection:
        before = list(connection.execute("SELECT * FROM bulletin_reads"))
        connection.execute("DROP TABLE bulletin_actor_reads")
        connection.execute("DROP TABLE bulletin_deltas")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version >= 91")
    reopened = AppStore(store.path)
    with reopened.connection() as connection:
        assert list(connection.execute("SELECT * FROM bulletin_reads")) == before
    assert post["post_id"] in stage(reopened, tmp_path)


@pytest.mark.parametrize("cause", ["retry", "resume"])
def test_continuation_replays_recorded_range(store, tmp_path, cause):
    first = member_post(store, "first")
    original = stage(store, tmp_path)
    later = member_post(store, "later")
    task = store.agent_task("turn")
    assert task is not None
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='failed' WHERE operation_id='turn'")
        store._insert_agent_task(
            connection,
            task.model_copy(
                update={
                    "operation_id": "continued",
                    "parent_operation_id": "turn",
                }
            ),
            continuation_cause=cause,
        )
    execution = AgentTaskExecution(
        operation_id="continued", store=store, control=AgentProcessControl()
    )
    stage_bulletin_pointer(execution, tmp_path, None)
    replayed = (tmp_path / "workspace" / "bulletin.md").read_text()
    assert first["post_id"] in original
    assert first["post_id"] in replayed and later["post_id"] not in replayed


def test_next_turn_receives_remaining_posts(store, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_DELTA_MAX_POSTS", 1)
    first, second = member_post(store, "first"), member_post(store, "second")
    stage(store, tmp_path)
    task = store.agent_task("turn")
    assert task is not None
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='turn'")
        store._insert_agent_task(
            connection, task.model_copy(update={"operation_id": "next"}), continuation_cause="fresh"
        )
    execution = AgentTaskExecution(operation_id="next", store=store, control=AgentProcessControl())
    stage_bulletin_pointer(execution, tmp_path, None)
    rendered = (tmp_path / "workspace" / "bulletin.md").read_text()
    assert first["post_id"] not in rendered and second["post_id"] in rendered


def test_command_read_does_not_advance_actor_cursor(store, tmp_path):
    first = member_post(store, "first")
    page = store.execute_bulletin_command("project", "turn", "read")
    assert first["post_id"] in {post["post_id"] for post in page["posts"]}
    assert first["post_id"] in stage(store, tmp_path)


def test_stage_write_failure_replays_committed_delta(store, tmp_path, monkeypatch):
    from rcp.transport.workspace_mailbox import RunStageMailbox

    post = member_post(store, "first")
    original = RunStageMailbox.write_text

    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(RunStageMailbox, "write_text", fail)
    with pytest.raises(OSError):
        stage(store, tmp_path)
    saved = delta(store)
    member_post(store, "later")
    monkeypatch.setattr(RunStageMailbox, "write_text", original)
    from rcp.storage import AppStore

    reopened = AppStore(store.path)
    assert post["post_id"] in stage(reopened, tmp_path)
    assert delta(reopened) == saved


def test_delta_migration_preserves_old_records(store):
    from rcp.storage.bulletin import migrate_bulletin_deltas

    posted = member_post(store, "old")
    with store.connection() as connection:
        connection.execute("DROP TABLE bulletin_actor_reads")
        connection.execute("DROP TABLE bulletin_deltas")
        migrate_bulletin_deltas(connection)
        migrate_bulletin_deltas(connection)
        assert connection.execute("SELECT count(*) FROM bulletin_actor_reads").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM bulletin_deltas").fetchone()[0] == 0
    assert store.list_bulletin("project", "main")["posts"] == [posted]


def test_concurrent_delta_claims_replay_one_range(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from rcp.runs.bulletin import render_bulletin

    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_DELTA_MAX_POSTS", 1)
    posts = [member_post(store, str(index)) for index in range(2)]

    def claim(operation):
        snapshot, _ = store.stage_bulletin_delta(
            "project", "main", "chat:chat", operation, {}, render_bulletin
        )
        return snapshot["posts"][0]["post_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ["turn", "turn"]))
    assert claimed == [posts[0]["post_id"]] * 2
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM bulletin_deltas").fetchone()[0] == 1


def test_render_failure_rolls_back_cursor_and_delta(store):
    member_post(store, "first")

    def refuse(snapshot):
        raise ValueError("render failed")

    with pytest.raises(ValueError):
        store.stage_bulletin_delta("project", "main", "chat:chat", "turn", {}, refuse)
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM bulletin_actor_reads").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM bulletin_deltas").fetchone()[0] == 0


def test_budget_does_not_skip_middle_post(store, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.runs.bulletin.BULLETIN_RENDER_MAX_BYTES", 2000)
    first = member_post(store, "first")
    middle = member_post(store, "middle", "界" * 7000)
    last = member_post(store, "last")
    rendered = stage(store, tmp_path)
    assert first["post_id"] in rendered
    assert middle["post_id"] not in rendered
    assert last["post_id"] not in rendered
    with store.connection() as connection:
        assert (
            delta(store)["through_seq"]
            == connection.execute(
                "SELECT sequence FROM bulletin_posts WHERE post_id=?", (first["post_id"],)
            ).fetchone()[0]
        )


def test_replay_uses_fresh_context_and_only_stores_ids(store):
    from rcp.runs.bulletin import render_bulletin

    post = member_post(store, "first")
    context = {"mentions": [], "members": [{"user_id": "old"}], "roster": []}
    store.stage_bulletin_delta("project", "main", "chat:chat", "turn", context, render_bulletin)
    store.mutate_bulletin_post(
        "project",
        "main",
        user_id=store.local_owner.user_id,
        key="edit",
        action="edit",
        post_id=post["post_id"],
        text="changed",
    )
    fresh = {
        "mentions": [{"post_id": "mention"}],
        "members": [{"user_id": "new"}],
        "roster": [{"actor_id": "live", "state": "working"}],
    }
    replay, _ = store.stage_bulletin_delta(
        "project", "main", "chat:chat", "turn", fresh, render_bulletin
    )
    assert replay["posts"][0]["text"] == "changed"
    for key in fresh:
        assert replay[key] == fresh[key]
    saved = delta(store)
    assert json.loads(saved["post_ids_json"]) == [post["post_id"]]
    assert "snapshot_json" not in saved


@pytest.mark.parametrize("status", ["queued", "running"])
def test_failed_stage_then_fresh_turn_keeps_posts(store, tmp_path, monkeypatch, status):
    from rcp.storage import AppStore
    from rcp.transport.workspace_mailbox import RunStageMailbox

    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status=? WHERE operation_id='turn'", (status,))
    first = member_post(store, "first")
    original = RunStageMailbox.write_text

    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(RunStageMailbox, "write_text", fail)
    with pytest.raises(OSError):
        stage(store, tmp_path)
    monkeypatch.setattr(RunStageMailbox, "write_text", original)
    reopened = AppStore(store.path)
    task = reopened.agent_task("turn")
    assert task is not None
    with reopened.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='failed' WHERE operation_id='turn'")
        reopened._insert_agent_task(
            connection,
            task.model_copy(update={"operation_id": "fresh", "status": "running"}),
            continuation_cause="fresh",
        )
    execution = AgentTaskExecution(
        operation_id="fresh", store=reopened, control=AgentProcessControl()
    )
    stage_bulletin_pointer(execution, tmp_path, None)
    assert first["post_id"] in (tmp_path / "workspace" / "bulletin.md").read_text()


def test_truncated_post_preserves_metadata(store, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.runs.bulletin.BULLETIN_RENDER_MAX_BYTES", 1800)
    post = member_post(store, "large", "界" * 7000)
    rendered = stage(store, tmp_path)
    records = [json.loads(block.split("\n```", 1)[0]) for block in rendered.split("```json\n")[1:]]
    clipped = next(row for row in records if row.get("truncated"))
    assert {key: value for key, value in clipped.items() if key not in {"text", "truncated"}} == {
        key: value for key, value in post.items() if key != "text"
    }


def test_actorless_stage_does_not_persist_delta(store, tmp_path):
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='refresh', request_json=json_object('provider', 'codex') WHERE operation_id='turn'"
        )
    member_post(store, "first")
    stage(store, tmp_path)
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM bulletin_actor_reads").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM bulletin_deltas").fetchone()[0] == 0


def test_queued_delta_is_consumed_after_running_stage(store, tmp_path):
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='queued' WHERE operation_id='turn'")
    member_post(store, "first")
    stage(store, tmp_path)
    saved = delta(store)
    with store.connection() as connection:
        assert (
            connection.execute("SELECT through_seq FROM bulletin_actor_reads").fetchone()[0]
            == saved["from_seq"]
        )
    assert store.mark_agent_task_running("turn")
    assert not store.mark_agent_task_running("turn")
    stage(store, tmp_path)
    with store.connection() as connection:
        assert (
            connection.execute("SELECT through_seq FROM bulletin_actor_reads").fetchone()[0]
            == saved["through_seq"]
        )


def test_long_retry_lineage_reuses_one_delta_even_without_intermediate_stages(store, tmp_path):
    post = member_post(store, "first")
    stage(store, tmp_path)
    task = store.agent_task("turn")
    assert task is not None
    parent = "turn"
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='failed' WHERE operation_id='turn'")
        for index in range(101):
            current = f"retry-{index}"
            store._insert_agent_task(
                connection,
                task.model_copy(
                    update={
                        "operation_id": current,
                        "parent_operation_id": parent,
                        "status": "failed",
                    }
                ),
                continuation_cause="retry",
            )
            parent = current
    execution = AgentTaskExecution(operation_id=parent, store=store, control=AgentProcessControl())
    stage_bulletin_pointer(execution, tmp_path, None)
    assert post["post_id"] in (tmp_path / "workspace" / "bulletin.md").read_text()
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM bulletin_deltas").fetchone()[0] == 1


def test_old_admission_without_bulletin_operation_replays(store, tmp_path):
    from rcp.storage import AppStore

    post = member_post(store, "old")
    stage(store, tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_run_receipts SET payload_json=json_remove(payload_json, '$.bulletin_operation_id') WHERE category='operation_admitted'"
        )
    reopened = AppStore(store.path)
    assert json.loads(delta(reopened)["post_ids_json"]) == [post["post_id"]]
    assert post["post_id"] in stage(reopened, tmp_path)
    assert delta(AppStore(store.path)) == delta(reopened)


def test_thread_page_read_cannot_skip_off_page_reply(store, tmp_path, monkeypatch):
    first = member_post(store, "first")
    stage(store, tmp_path)
    second = member_post(store, "second")
    reply = store.mutate_bulletin_post(
        "project",
        "main",
        user_id=store.local_owner.user_id,
        key="reply",
        text="off page",
        reply_to=first["post_id"],
    )
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_PAGE_SIZE", 1)
    page = store.execute_bulletin_command("project", "turn", "read")
    assert second["post_id"] in {post["post_id"] for post in page["posts"]}
    assert reply["post_id"] not in {post["post_id"] for post in page["posts"]}
    with store.connection() as connection:
        task = store.agent_task("turn")
        assert task is not None
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='turn'")
        store._insert_agent_task(
            connection, task.model_copy(update={"operation_id": "next"}), continuation_cause="fresh"
        )
    execution = AgentTaskExecution(operation_id="next", store=store, control=AgentProcessControl())
    stage_bulletin_pointer(execution, tmp_path, None)
    rendered = (tmp_path / "workspace" / "bulletin.md").read_text()
    assert reply["post_id"] in rendered and second["post_id"] in rendered


def test_replay_growth_only_consumes_currently_rendered_posts(store, tmp_path, monkeypatch):
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='queued' WHERE operation_id='turn'")
    first, second = member_post(store, "first"), member_post(store, "second")
    stage(store, tmp_path)
    store.mutate_bulletin_post(
        "project",
        "main",
        user_id=store.local_owner.user_id,
        key="edit",
        action="edit",
        post_id=first["post_id"],
        text="界" * 7000,
    )
    monkeypatch.setattr("rcp.runs.bulletin.BULLETIN_RENDER_MAX_BYTES", 1800)
    assert store.mark_agent_task_running("turn")
    rendered = stage(store, tmp_path)
    assert first["post_id"] in rendered and second["post_id"] not in rendered
    with store.connection() as connection:
        cursor = connection.execute("SELECT through_seq FROM bulletin_actor_reads").fetchone()[0]
        assert (
            cursor
            == connection.execute(
                "SELECT sequence FROM bulletin_posts WHERE post_id=?", (first["post_id"],)
            ).fetchone()[0]
        )


def test_mentions_leave_room_for_truncated_post_metadata(store, tmp_path, monkeypatch):
    from rcp.runs.bulletin import render_bulletin

    monkeypatch.setattr("rcp.runs.bulletin.BULLETIN_RENDER_MAX_BYTES", 1800)
    post = member_post(store, "first", "界" * 7000)
    snapshot = {"posts": [post], "mentions": [{"text": "x" * 900}, {"text": "y" * 500}]}
    rendered = render_bulletin(snapshot)
    assert snapshot["shown"] == 1
    assert len(rendered.encode()) <= 1800


def test_replay_omits_deleted_and_expired_posts(store):
    from rcp.runs.bulletin import render_bulletin

    posts = [member_post(store, str(index)) for index in range(3)]
    store.stage_bulletin_delta("project", "main", "chat:chat", "turn", {}, render_bulletin)
    store.mutate_bulletin_post(
        "project",
        "main",
        user_id=store.local_owner.user_id,
        key="delete",
        action="delete",
        post_id=posts[0]["post_id"],
    )
    with store.connection() as connection:
        connection.execute("DELETE FROM bulletin_posts WHERE post_id=?", (posts[1]["post_id"],))
    snapshot, _ = store.stage_bulletin_delta(
        "project", "main", "chat:chat", "turn", {}, render_bulletin
    )
    assert [post["post_id"] for post in snapshot["posts"]] == [posts[2]["post_id"]]
    assert snapshot["shown"] == snapshot["total"] == 1
