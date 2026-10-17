from __future__ import annotations

import pytest

from rcp.storage import ProjectRecord
from rcp.storage.bulletin import BulletinError

from .test_lessons_store import store as store


def post(store, key, **kwargs):
    return store.execute_bulletin_command(
        "project", "turn", "post", key=key, text=kwargs.pop("text", "note"), **kwargs
    )["post"]


def test_receipts_remain_idempotent_after_retention(store, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_POSTS_PER_TARGET_MAX", 1)
    first = post(store, "a")
    second = post(store, "b")
    assert post(store, "a") == first
    assert store.list_bulletin("project", "main")["posts"] == [second]
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_key_conflict"):
        post(store, "a", text="different")


def test_target_and_project_isolation_and_reply_depth(store):
    first = post(store, "a")
    reply = post(store, "b", reply_to=first["post_id"])
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_reply_invalid"):
        post(store, "c", reply_to=reply["post_id"])
    for project_id, target in [("other", "main"), ("project", "branch:other")]:
        assert store.list_bulletin(project_id, target)["posts"] == []
    store.upsert_project(
        ProjectRecord(
            project_id="other",
            locator="other",
            name="Other",
            state_location="other",
            state_remote=False,
            added_at=store.now(),
        )
    )
    store.seat_project_member("other", store.local_owner.user_id)
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_reply_invalid"):
        store.add_bulletin_post(
            "other",
            "main",
            "reply",
            user_id=store.local_owner.user_id,
            key="a",
            reply_to=first["post_id"],
        )
    member = store.add_bulletin_post(
        "other", "main", "hello", user_id=store.local_owner.user_id, key="b"
    )
    assert member["author"]["user_id"] == store.local_owner.user_id
    assert member["author"]["kind"] == "member"
    assert store.list_bulletin("other", "main")["posts"] == [member]


def test_discuss_reads_but_cannot_post(store):
    post(store, "a")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET dispatch_authority_json='{}' WHERE operation_id='turn'"
        )
    assert store.execute_bulletin_command("project", "turn", "read")["total"] == 1
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_forbidden"):
        post(store, "b")


def test_limits_and_cursor_pagination(store, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_PAGE_SIZE", 1)
    first = post(store, "a")
    second = post(store, "b")
    page = store.list_bulletin("project", "main")
    assert page["posts"] == [second]
    assert store.list_bulletin("project", "main", cursor=page["next_cursor"])["posts"] == [first]
    for key, kwargs, code in [
        ("blank", {"text": " "}, "bulletin_text_invalid"),
        ("refs", {"refs": [""]}, "bulletin_refs_invalid"),
    ]:
        with pytest.raises(BulletinError, check=lambda exc, expected=code: exc.code == expected):
            post(store, key, **kwargs)


def test_bulletin_transfer_and_project_identity_lifecycle(store):
    from uuid import uuid4

    from rcp.transfer import TRANSFER_EXCLUDED_PROJECT_TABLES

    first = post(store, "a")
    assert {"bulletin_posts", "bulletin_command_receipts"} <= TRANSFER_EXCLUDED_PROJECT_TABLES
    store.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    assert store.list_bulletin("project", "main")["posts"] == [first]
    canonical = str(uuid4())
    store.migrate_project_identity("project", canonical, str(uuid4()))
    assert store.list_bulletin(canonical, "main")["posts"] == [first]
    counts = store.delete_project_records(canonical)
    assert counts["bulletin_posts"] == counts["bulletin_command_receipts"] == 1


def test_old_episode_writer_defaults_enabled_and_lineage_off_refuses(store):
    from rcp.storage import EpisodeRecord
    from rcp.storage.bulletin import migrate_graph_bulletin

    legacy = dict(
        episode_id="legacy",
        project_id="project",
        mode="auto_research",
        status="running",
        invocation_ceiling=2,
        created_at=store.now(),
        updated_at=store.now(),
    )
    assert EpisodeRecord.model_validate(legacy).bulletin_enabled is True
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO episodes(episode_id,project_id,mode,status,invocation_ceiling,created_at,updated_at) "
            "VALUES ('legacy','project','auto_research','running',2,?,?)",
            (store.now(), store.now()),
        )
        # Recreate the pre-migration column shape with a persisted old-writer row.
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND (tbl_name='episodes' OR name LIKE '%_bulletin_%')"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{row[0]}"')
        connection.execute("DROP TABLE bulletin_posts")
        connection.execute("DROP TABLE bulletin_command_receipts")
        connection.execute("ALTER TABLE episodes DROP COLUMN bulletin_enabled")
        migrate_graph_bulletin(connection)
        assert (
            connection.execute(
                "SELECT bulletin_enabled FROM episodes WHERE episode_id='legacy'"
            ).fetchone()[0]
            == 1
        )
        connection.execute("UPDATE graph_runs SET episode_id='legacy' WHERE operation_id='turn'")
        connection.execute("UPDATE episodes SET bulletin_enabled=0 WHERE episode_id='legacy'")
    for verb in ("read", "post"):
        with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_disabled"):
            store.execute_bulletin_command("project", "turn", verb, key=verb, text="note")
    assert not store.bulletin_enabled_for_task("turn")
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET episode_id=NULL WHERE operation_id='turn'")
    assert store.bulletin_enabled_for_task("turn")
    post(store, "unrelated")
