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
    assert stage(store, tmp_path) == rendered
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
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version >= 90")
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
    assert replayed == original
    assert first["post_id"] in replayed and later["post_id"] not in replayed


def test_next_turn_receives_remaining_posts(store, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_DELTA_MAX_POSTS", 1)
    first, second = member_post(store, "first"), member_post(store, "second")
    stage(store, tmp_path)
    task = store.agent_task("turn")
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='turn'")
        store._insert_agent_task(
            connection, task.model_copy(update={"operation_id": "next"}), continuation_cause="fresh"
        )
    execution = AgentTaskExecution(operation_id="next", store=store, control=AgentProcessControl())
    stage_bulletin_pointer(execution, tmp_path, None)
    rendered = (tmp_path / "workspace" / "bulletin.md").read_text()
    assert first["post_id"] not in rendered and second["post_id"] in rendered


def test_command_read_advances_only_own_channel_cursor(store, tmp_path):
    first = member_post(store, "first")
    page = store.execute_bulletin_command("project", "turn", "read")
    assert first["post_id"] in {post["post_id"] for post in page["posts"]}
    assert first["post_id"] not in stage(store, tmp_path)


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


def test_concurrent_delta_claims_are_disjoint(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from rcp.runs.bulletin import render_bulletin

    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_DELTA_MAX_POSTS", 1)
    posts = [member_post(store, str(index)) for index in range(2)]

    def claim(operation):
        snapshot, _ = store.stage_bulletin_delta(
            "project", "main", "chat:chat", [operation], {}, render_bulletin
        )
        return snapshot["posts"][0]["post_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ["one", "two"]))
    assert set(claimed) == {post["post_id"] for post in posts}


def test_render_failure_rolls_back_cursor_and_delta(store):
    member_post(store, "first")

    def refuse(snapshot):
        raise ValueError("render failed")

    with pytest.raises(ValueError):
        store.stage_bulletin_delta("project", "main", "chat:chat", ["turn"], {}, refuse)
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
