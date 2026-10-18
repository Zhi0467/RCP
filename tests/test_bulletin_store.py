from __future__ import annotations

import pytest

from rcp.limits import BULLETIN_KEY_MAX_CHARS, BULLETIN_REPLY_MAX_CHARS
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


def test_agent_author_names_its_seat_not_its_task_kind(store):
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json, '$.role', 'worker', "
            "'$.node_id', 'exp/seat') WHERE operation_id='turn'"
        )
    author = post(store, "a")["author"]
    assert author["kind"] == "agent" and "exp/seat" in author["actor_label"]


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
        ("x" * (BULLETIN_KEY_MAX_CHARS + 1), {}, "bulletin_key_invalid"),
        ("reply", {"reply_to": "x" * (BULLETIN_REPLY_MAX_CHARS + 1)}, "bulletin_reply_invalid"),
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
        connection.execute("DROP TABLE bulletin_reads")
        connection.execute("DROP TABLE bulletin_mention_observations")
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


def test_reply_projection_keeps_parent_context_across_pages(store, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_PAGE_SIZE", 1)
    parent = post(store, "parent")
    reply = post(store, "reply", reply_to=parent["post_id"])
    page = store.list_bulletin("project", "main")
    assert [item["post_id"] for item in page["posts"]] == [reply["post_id"]]
    assert page["posts"][0]["reply_parent"] == {
        key: parent[key] for key in ("post_id", "author", "text")
    }
    with store.connection() as connection:
        connection.execute("DELETE FROM bulletin_posts WHERE post_id=?", (parent["post_id"],))
    assert store.list_bulletin("project", "main")["posts"][0]["reply_parent"] is None


def other_member(store):
    from uuid import uuid4

    user_id = str(uuid4())
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO space_users(user_id,identity_kind,display_name,created_at,updated_at) VALUES(?,'team_member','Other',?,?)",
            (user_id, store.now(), store.now()),
        )
    return store.space_user(user_id)


def test_inline_refs_and_mentions_validate_live_target_roster(store):
    owner = store.local_owner.user_id
    member = other_member(store)
    store.seat_project_member("project", member.user_id)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.chat_id','channel-chat') WHERE operation_id='turn'"
        )
    result = post(
        store,
        "tokens",
        text=f"#exp/fork-b, #hyp/x. #exp/fork-b <@member:{owner}> <@member:{member.user_id}> <@member:missing> <@agent:chat:channel-chat> <@agent:chat:missing>",
    )
    assert result["refs"] == ["exp/fork-b", "hyp/x"]
    assert result["mentions"] == [
        {"kind": "member", "id": owner},
        {"kind": "member", "id": member.user_id},
        {"kind": "agent", "id": "chat:channel-chat"},
    ]
    with store.connection() as connection:
        connection.execute(
            "UPDATE space_users SET removal_started_at=? WHERE user_id=?",
            (store.now(), member.user_id),
        )
    assert post(store, "removed", text=f"<@member:{member.user_id}>")["mentions"] == []


def test_member_edit_delete_author_receipts_and_reply_placeholders(store):
    owner = store.local_owner.user_id
    parent = store.add_bulletin_post(
        "project", "main", "#hyp/original", user_id=owner, key="parent"
    )
    reply = post(store, "reply", reply_to=parent["post_id"])
    other = other_member(store)
    store.seat_project_member("project", other.user_id)
    for action in ("edit", "delete"):
        with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_not_author"):
            store.mutate_bulletin_post(
                "project",
                "main",
                parent["post_id"],
                user_id=other.user_id,
                key=action,
                text="changed",
                action=action,
            )
    edited = store.mutate_bulletin_post(
        "project", "main", parent["post_id"], user_id=owner, key="edit", text="#hyp/updated"
    )
    assert edited["edited_at"] and edited["refs"] == ["hyp/updated"] and edited["reply_count"] == 1
    deleted = store.mutate_bulletin_post(
        "project", "main", parent["post_id"], user_id=owner, key="delete", action="delete"
    )
    assert (
        deleted["deleted_at"]
        and deleted["text"] == ""
        and deleted["refs"] == deleted["mentions"] == []
    )
    assert (
        store.mutate_bulletin_post(
            "project", "main", parent["post_id"], user_id=owner, key="delete", action="delete"
        )
        == deleted
    )
    assert len(store.list_bulletin("project", "main")["posts"]) == 2
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_post_deleted"):
        store.mutate_bulletin_post(
            "project", "main", parent["post_id"], user_id=owner, key="later", text="later"
        )
    store.execute_bulletin_command(
        "project", "turn", "delete", post_id=reply["post_id"], key="delete-reply"
    )
    assert store.list_bulletin("project", "main")["posts"] == []


def test_channels_unread_and_monotone_read_cursor(store, monkeypatch):
    owner = store.local_owner.user_id
    with store.connection() as connection:
        for branch_id, name, archived in [
            ("b", "Branch", 0),
            ("recent", "Recent", 0),
            ("hidden", "Hidden", 1),
        ]:
            connection.execute(
                "INSERT INTO graph_branches(project_id,branch_id,parent_target_json,parent_revision,created_by,created_at,name,archived) VALUES('project',?,'{}',0,'member',?,?,?)",
                (branch_id, store.now(), name, archived),
            )
    first = post(store, "first")
    second = post(store, "second")
    store.add_bulletin_post("project", "main", "own", user_id=owner, key="own")
    store.add_bulletin_post("project", "branch:recent", "recent", user_id=owner, key="recent")
    channels = store.bulletin_channels("project", owner)
    assert [item["target_key"] for item in channels] == ["main", "branch:recent", "branch:b"]
    assert channels[0]["unread"] == 2 and channels[2]["last_post_at"] is None
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_UNREAD_MAX_COUNT", 1)
    assert store.bulletin_channels("project", owner)[0]["unread"] == 1
    store.mark_bulletin_read("project", "main", owner, second["post_id"])
    store.mark_bulletin_read("project", "main", owner, first["post_id"])
    assert store.bulletin_channels("project", owner)[0]["unread"] == 0
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_post_not_found"):
        store.mark_bulletin_read("project", "branch:b", owner, second["post_id"])


def test_agent_receipts_are_per_turn_but_authorship_spans_turns(store):
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.chat_id','shared')"
        )
    first = post(store, "same-key")
    task = store.agent_task("turn")
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='turn'")
    store.create_agent_task(task.model_copy(update={"operation_id": "next-turn"}))
    second = store.execute_bulletin_command(
        "project", "next-turn", "post", key="same-key", text="note"
    )["post"]
    assert first["post_id"] != second["post_id"]
    assert post(store, "same-key") == first
    assert (
        store.execute_bulletin_command("project", "next-turn", "post", key="same-key", text="note")[
            "post"
        ]
        == second
    )
    edited = store.execute_bulletin_command(
        "project", "next-turn", "edit", post_id=first["post_id"], key="edit", text="updated"
    )["post"]
    assert edited["text"] == "updated"
    assert store.execute_bulletin_command(
        "project", "next-turn", "delete", post_id=first["post_id"], key="delete"
    )["post"]["deleted_at"]


def test_agent_mention_resolution_is_target_local_and_delete_skips_parsing(store, monkeypatch):
    import sqlite3

    from rcp.core.transition_models import GraphTargetRef
    from rcp.storage.bulletin import _parse

    with store.connection() as connection:
        connection.execute(
            "INSERT INTO graph_branches(project_id,branch_id,parent_target_json,parent_revision,created_by,created_at,name) "
            "VALUES('project','other','{}',0,'member',?,'Other')",
            (store.now(),),
        )
    task = store.agent_task("turn")
    store.create_agent_task(
        task.model_copy(
            update={
                "operation_id": "other-target",
                "status": "succeeded",
                "request": {**task.request, "chat_id": "other-chat"},
                "graph_target": GraphTargetRef(kind="branch", branch_id="other"),
            }
        )
    )

    def target_rows_only(cursor, values):
        row = sqlite3.Row(cursor, values)
        if "operation_id" in row.keys():  # noqa: SIM118 - sqlite rows iterate values
            assert row["operation_id"] != "other-target"
        return row

    with store.connection() as connection:
        connection.row_factory = target_rows_only
        _, mentions = _parse(connection, "project", "main", "<@agent:chat:chat>")
    assert mentions == [{"kind": "agent", "id": "chat:chat"}]
    first = post(store, "mention", text="<@agent:chat:chat>")

    def refuse_parse(*args):
        pytest.fail("delete must not parse mentions")

    monkeypatch.setattr("rcp.storage.bulletin._parse", refuse_parse)
    assert store.execute_bulletin_command(
        "project", "turn", "delete", post_id=first["post_id"], key="delete"
    )["post"]["deleted_at"]


def test_reply_to_deleted_post_is_refused(store):
    parent = post(store, "parent")
    store.execute_bulletin_command(
        "project", "turn", "delete", post_id=parent["post_id"], key="delete"
    )
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_reply_invalid"):
        post(store, "reply", reply_to=parent["post_id"])


def test_read_cursor_does_not_bump_shared_bulletin_version(store):
    owner = store.local_owner.user_id
    first, second = post(store, "first"), post(store, "second")
    with store.connection() as connection:
        before = connection.execute(
            "SELECT SUM(generation) FROM resource_versions WHERE resource='bulletin'"
        ).fetchone()[0]
    for item in (first, second):
        store.mark_bulletin_read("project", "main", owner, item["post_id"])
    assert store.bulletin_channels("project", owner)[0]["unread"] == 0
    with store.connection() as connection:
        connection.execute("DELETE FROM bulletin_reads")
        assert (
            connection.execute(
                "SELECT SUM(generation) FROM resource_versions WHERE resource='bulletin'"
            ).fetchone()[0]
            == before
        )


def test_retention_prunes_only_observed_orphan_mentions(store, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_POSTS_PER_TARGET_MAX", 1)
    text = f"<@member:{store.local_owner.user_id}>"
    observed = post(store, "observed", text=text)
    with store.connection() as connection:
        connection.execute("UPDATE bulletin_mention_observations SET observed_at=?", (store.now(),))
    pending = post(store, "pending", text=text)
    retained = post(store, "retained", text=text)
    with store.connection() as connection:
        connection.execute(
            "UPDATE bulletin_mention_observations SET observed_at=? WHERE post_id=?",
            (store.now(), retained["post_id"]),
        )
    store.execute_bulletin_command(
        "project", "turn", "edit", post_id=retained["post_id"], key="edit", text=text
    )
    with store.connection() as connection:
        ids = {
            row[0]
            for row in connection.execute("SELECT post_id FROM bulletin_mention_observations")
        }
    assert observed["post_id"] not in ids
    assert ids == {pending["post_id"], retained["post_id"]}
