from __future__ import annotations

import pytest

from rcp.storage.comments import CommentError

from .test_lessons_store import store as store


def thread(store, text="note"):
    return store.create_comment_thread(
        "project",
        target_kind="reply",
        target_id="turn",
        anchor={"kind": "whole", "comment": ""},
        text=text,
        user_id=store.local_owner.user_id,
    )


def test_lifecycle_and_send_replay(store):
    item = thread(store)
    tid = item["thread_id"]
    actor = store.local_owner.user_id
    store.add_comment_post("project", tid, user_id=actor, text="second")
    store.mark_threads_sent([tid], "op", "key", actor)
    with store.connection() as connection:
        connection.execute("UPDATE comment_posts SET created_at='9999' WHERE text='second'")
    sent, _route = store.sendable_threads("project", actor, [tid], "key")[0]
    assert sent["sent_operation_id"] == "op"
    assert "note" in sent["rendered"] and "second" not in sent["rendered"]
    with pytest.raises(CommentError):
        store.sendable_threads("project", actor, [tid], "other")
    with pytest.raises(CommentError) as error:
        store.sendable_threads("elsewhere", actor, [tid], "key")
    assert error.value.status_code == 404
    store.mark_threads_sent([tid], "other", "other", actor)
    assert store.get_comment_thread("project", tid)["sent_operation_id"] == "op"
    item = store.add_comment_post("project", tid, user_id=actor, text="reply")
    assert item["state"] == "open"
    assert item["send_key"] is None
    store.resolve_comment_thread("project", tid, user_id=actor)
    store.reopen_comment_thread("project", tid, user_id=actor)
    store.delete_comment_post("project", item["posts"][0]["post_id"], user_id=actor)
    assert store.list_comment_threads("project", "reply", "turn") == []


def test_mentions_follow_edits_without_reobservation(store):
    actor = store.local_owner.user_id
    item = thread(store, f"hello <@member:{actor}>")
    pid = item["posts"][0]["post_id"]
    with store.connection() as connection:
        connection.execute("UPDATE comment_mentions SET observed_at='seen'")
    store.edit_comment_post("project", pid, user_id=actor, text=f"again <@member:{actor}>")
    assert store.open_comment_mentions("project", actor)[0]["observed_at"] == "seen"
    store.edit_comment_post("project", pid, user_id=actor, text="gone")
    assert store.open_comment_mentions("project", actor) == []
    with pytest.raises(CommentError, match="agent"):
        thread(store, "<@agent:chat:c>")


def test_budget_is_atomic_and_author_only(store):
    item = thread(store, "a" * 2048)
    tid = item["thread_id"]
    actor = store.local_owner.user_id
    store.add_comment_post("project", tid, user_id=actor, text="b" * 2048)
    with pytest.raises(CommentError) as error:
        store.add_comment_post("project", tid, user_id=actor, text="c" * 2048)
    assert error.value.status_code == 422
    assert len(store.get_comment_thread("project", tid)["posts"]) == 2
    from .test_bulletin_store import other_member

    member = other_member(store)
    store.seat_project_member("project", member.user_id)
    with pytest.raises(CommentError) as error:
        store.edit_comment_post(
            "project", item["posts"][0]["post_id"], user_id=member.user_id, text="x"
        )
    assert error.value.status_code == 403


def test_paper_disappears_and_returns(store):
    actor = store.local_owner.user_id
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_files VALUES ('project','file','a.md','text','now',NULL,NULL)"
        )
    item = store.create_comment_thread(
        "project",
        target_kind="paper",
        target_id="file",
        anchor={"kind": "whole", "comment": ""},
        text=f"<@member:{actor}>",
        user_id=actor,
    )
    with store.connection() as connection:
        connection.execute("UPDATE paper_files SET deleted_at='now'")
    assert store.open_comment_mentions("project", actor) == []
    assert store.comment_target_route(actor, "paper", "file", project_id="project") is None
    with pytest.raises(CommentError) as error:
        store.add_comment_post("project", item["thread_id"], user_id=actor, text="gone")
    assert error.value.status_code == 404
    with store.connection() as connection:
        connection.execute("UPDATE paper_files SET deleted_at=NULL")
    assert store.open_comment_mentions("project", actor)[0]["thread_id"] == item["thread_id"]
    assert store.comment_target_route(actor, "paper", "file", project_id="project") is not None


@pytest.mark.parametrize("expire", [False, True])
def test_artifact_removal_cascades_comments(store, expire):
    from datetime import datetime, timedelta

    from rcp.storage import Artifact

    actor = store.local_owner.user_id
    now = datetime.fromisoformat(store.now())
    artifact = store.create_artifact(
        Artifact(
            artifact_id="comment-artifact",
            project_id="project",
            supplier="turn",
            supplier_id="turn",
            source_name="a.txt",
            media_type="text/plain",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
        ),
        data=b"text",
    )
    store.create_comment_thread(
        "project",
        target_kind="artifact",
        target_id=artifact.artifact_id,
        anchor={"kind": "whole", "comment": ""},
        text=f"<@member:{actor}>",
        user_id=actor,
    )
    if expire:
        assert store.expire_artifacts(as_of=now + timedelta(hours=2)) == 1
    else:
        store.delete_artifact("project", artifact.artifact_id)
    with store.connection() as connection:
        for table in ("comment_threads", "comment_posts", "comment_mentions"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_tables_and_watermark(store):
    from rcp.storage.models import _PROJECT_ID_TABLES
    from rcp.transfer.records import TRANSFER_EXCLUDED_PROJECT_TABLES

    tables = {"comment_threads", "comment_posts", "comment_mentions"}
    assert tables <= set(_PROJECT_ID_TABLES)
    assert tables <= TRANSFER_EXCLUDED_PROJECT_TABLES
    item = thread(store)
    with store.connection() as connection:
        generation = connection.execute(
            "SELECT generation FROM resource_versions WHERE scope='project' AND resource='comments'"
        ).fetchone()[0]
    store.resolve_comment_thread("project", item["thread_id"], user_id=store.local_owner.user_id)
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT generation FROM resource_versions WHERE scope='project' AND resource='comments'"
            ).fetchone()[0]
            > generation
        )


def test_frozen_pre_comments_database_upgrades(tmp_path):
    import gzip
    import sqlite3
    from pathlib import Path

    from rcp.storage import AppStore

    source = (
        Path(__file__).parent
        / "fixtures/server_upgrade/pre-comments-v47-c2b0f07/data/rcp.sqlite3.gz"
    )
    database = tmp_path / "rcp.sqlite3"
    database.write_bytes(gzip.decompress(source.read_bytes()))
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT max(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            == 82
        )
    upgraded = AppStore(database)
    with upgraded.connection() as connection:
        assert (
            connection.execute(
                "SELECT max(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            == 83
        )
        for table in ("comment_threads", "comment_posts", "comment_mentions"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    upgraded.close()


def test_comments_follow_project_identity_and_purge(store):
    from uuid import uuid4

    actor = store.local_owner.user_id
    thread(store, f"<@member:{actor}>")
    store.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    canonical = str(uuid4())
    store.migrate_project_identity("project", canonical, str(uuid4()))
    tables = ("comment_threads", "comment_posts", "comment_mentions")
    with store.connection() as connection:
        for table in tables:
            assert connection.execute(f"SELECT project_id FROM {table}").fetchone()[0] == canonical
    store.delete_project_records(canonical)
    with store.connection() as connection:
        for table in tables:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_edit_adds_mentions_and_resolve_fences_send(store):
    actor = store.local_owner.user_id
    item = thread(store)
    tid = item["thread_id"]
    store.edit_comment_post(
        "project",
        item["posts"][0]["post_id"],
        user_id=actor,
        text=f"<@member:{actor}> <@member:missing>",
    )
    mentions = store.open_comment_mentions("project", actor)
    assert len(mentions) == 1
    assert mentions[0]["observed_at"] is None
    assert [m["id"] for m in mentions[0]["thread"]["posts"][0]["mentions"]] == [actor]
    closed = store.resolve_comment_thread("project", tid, user_id=actor)
    store.mark_threads_sent([tid], "op", "key", actor)
    assert store.get_comment_thread("project", tid) == closed


def test_each_comment_table_invalidates_on_each_mutation(store):
    actor = store.local_owner.user_id
    item = thread(store, f"<@member:{actor}>")
    with store.connection() as connection:

        def generation():
            return connection.execute(
                "SELECT generation FROM resource_versions WHERE scope='project' AND resource='comments'"
            ).fetchone()[0]

        rows = {
            table: dict(connection.execute(f"SELECT * FROM {table}").fetchone())
            for table in ("comment_mentions", "comment_posts", "comment_threads")
        }
        for table in rows:
            before = generation()
            connection.execute(f"UPDATE {table} SET created_at='changed'")
            assert generation() > before
            before = generation()
            connection.execute(f"DELETE FROM {table}")
            assert generation() > before
        for table in ("comment_threads", "comment_posts", "comment_mentions"):
            row = rows[table]
            before = generation()
            columns = ",".join(row)
            placeholders = ",".join("?" for _ in row)
            connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values())
            )
            assert generation() > before
    assert store.get_comment_thread("project", item["thread_id"])["state"] == "open"


def test_member_mentions_drop_private_target_nonreaders(store):
    from .test_bulletin_store import other_member

    actor = store.local_owner.user_id
    member = other_member(store)
    store.seat_project_member("project", member.user_id)
    chat = next(
        item
        for item in store.ensure_agent_threads(actor, ["project"], default_names=("Agent",))
        if item.project_id == "project"
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.chat_id',?) WHERE operation_id='turn'",
            (chat.thread_id,),
        )
    item = store.create_comment_thread(
        "project",
        target_kind="reply",
        target_id="turn",
        anchor={"kind": "whole"},
        text=f"<@member:{member.user_id}>",
        user_id=actor,
    )
    assert item["posts"][0]["mentions"] == []
    assert store.comment_target_route(member.user_id, "reply", "turn", project_id="project") is None
    assert store.open_comment_mentions("project", member.user_id) == []


def test_mutators_recheck_readability(store):
    from .test_bulletin_store import other_member

    actor = store.local_owner.user_id
    item = thread(store)
    tid, pid = item["thread_id"], item["posts"][0]["post_id"]
    member = other_member(store)
    store.seat_project_member("project", member.user_id)
    chat = next(
        chat
        for chat in store.ensure_agent_threads(
            member.user_id, ["project"], default_names=("Agent",)
        )
        if chat.project_id == "project"
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.chat_id',?) "
            "WHERE operation_id='turn'",
            (chat.thread_id,),
        )
    for mutate in (
        lambda: store.add_comment_post("project", tid, user_id=actor, text="x"),
        lambda: store.resolve_comment_thread("project", tid, user_id=actor),
        lambda: store.reopen_comment_thread("project", tid, user_id=actor),
        lambda: store.edit_comment_post("project", pid, user_id=actor, text="x"),
        lambda: store.delete_comment_post("project", pid, user_id=actor),
    ):
        with pytest.raises(CommentError) as error:
            mutate()
        assert error.value.status_code == 404


def test_rename_cannot_push_saved_thread_over_budget(store):
    from rcp.comments.render import render_thread
    from rcp.limits import COMMENT_THREAD_MAX_CHARS

    actor = store.local_owner.user_id
    tid = thread(store, "a" * 500)["thread_id"]
    with pytest.raises(CommentError):
        while True:
            store.add_comment_post("project", tid, user_id=actor, text="b" * 500)
    with store.connection() as connection:
        connection.execute(
            "UPDATE space_users SET display_name=? WHERE user_id=?", ("n" * 500, actor)
        )
    assert len(render_thread(store.get_comment_thread("project", tid))) <= (
        COMMENT_THREAD_MAX_CHARS
    )
