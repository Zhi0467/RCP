from __future__ import annotations

from contextlib import contextmanager

import pytest

from rcp.limits import BULLETIN_KEY_MAX_CHARS, BULLETIN_REPLY_MAX_CHARS
from rcp.storage import Artifact, ProjectRecord
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
        store.mutate_bulletin_post(
            "other",
            "main",
            text="reply",
            user_id=store.local_owner.user_id,
            key="a",
            reply_to=first["post_id"],
        )
    member = store.mutate_bulletin_post(
        "other", "main", text="hello", user_id=store.local_owner.user_id, key="b"
    )
    assert member["author"]["user_id"] == store.local_owner.user_id
    assert member["author"]["kind"] == "member"
    assert store.list_bulletin("other", "main")["posts"] == [member]


def test_agent_author_names_its_seat_not_its_task_kind(store):
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_remove(json_set(request_json, "
            "'$.role', 'worker', '$.node_id', 'exp/seat'), '$.chat_id') WHERE operation_id='turn'"
        )
    author = post(store, "a")["author"]
    assert author["kind"] == "agent" and "exp/seat" in author["actor_label"]


def test_agent_author_keeps_its_conversation_name(store):
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json, '$.chat_id', 'c1') "
            "WHERE operation_id='turn'"
        )
    store.set_chat_title("project", "c1", store.local_owner.user_id, "Probe sweep")
    assert post(store, "a")["author"]["actor_label"] == "Probe sweep"


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


def test_pages_hold_whole_threads_and_retention_drops_replies_with_parent(store, monkeypatch):
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_PAGE_SIZE", 1)
    parent = post(store, "parent")
    newer = post(store, "newer")
    reply = post(store, "reply", reply_to=parent["post_id"])
    page = store.list_bulletin("project", "main")
    assert [item["post_id"] for item in page["posts"]] == [newer["post_id"]]
    older = store.list_bulletin("project", "main", cursor=page["next_cursor"])
    assert [item["post_id"] for item in older["posts"]] == [reply["post_id"], parent["post_id"]]
    assert older["posts"][1]["reply_count"] == 1
    assert older["posts"][0]["reply_parent"] == {
        key: parent[key] for key in ("post_id", "author", "text")
    }
    assert older["next_cursor"] is None
    # The newest page still names the channel's latest post for read tracking.
    assert page["newest_post_id"] == reply["post_id"]
    # Retention evicts whole threads, least recently active first.
    monkeypatch.setattr("rcp.storage.bulletin.BULLETIN_POSTS_PER_TARGET_MAX", 3)
    late = post(store, "late reply", reply_to=parent["post_id"])

    def kept():
        with store.connection() as connection:
            return {row[0] for row in connection.execute("SELECT post_id FROM bulletin_posts")}

    assert kept() == {parent["post_id"], reply["post_id"], late["post_id"]}
    newest = post(store, "newest")
    assert kept() == {newest["post_id"]}


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
    parent = store.mutate_bulletin_post(
        "project", "main", text="#hyp/original", user_id=owner, key="parent"
    )
    reply = post(store, "reply", reply_to=parent["post_id"])
    other = other_member(store)
    store.seat_project_member("project", other.user_id)
    for action in ("edit", "delete"):
        with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_not_author"):
            store.mutate_bulletin_post(
                "project",
                "main",
                post_id=parent["post_id"],
                user_id=other.user_id,
                key=action,
                text="changed",
                action=action,
            )
    edited = store.mutate_bulletin_post(
        "project",
        "main",
        post_id=parent["post_id"],
        user_id=owner,
        key="edit",
        text="#hyp/updated",
        action="edit",
    )
    assert edited["edited_at"] and edited["refs"] == ["hyp/updated"] and edited["reply_count"] == 1
    deleted = store.mutate_bulletin_post(
        "project", "main", post_id=parent["post_id"], user_id=owner, key="delete", action="delete"
    )
    assert (
        deleted["deleted_at"]
        and deleted["text"] == ""
        and deleted["refs"] == deleted["mentions"] == []
    )
    assert (
        store.mutate_bulletin_post(
            "project",
            "main",
            post_id=parent["post_id"],
            user_id=owner,
            key="delete",
            action="delete",
        )
        == deleted
    )
    assert len(store.list_bulletin("project", "main")["posts"]) == 2
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_post_deleted"):
        store.mutate_bulletin_post(
            "project",
            "main",
            post_id=parent["post_id"],
            user_id=owner,
            key="later",
            text="later",
            action="edit",
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
    store.mutate_bulletin_post("project", "main", text="own", user_id=owner, key="own")
    store.mutate_bulletin_post(
        "project", "branch:recent", text="recent", user_id=owner, key="recent"
    )
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


def test_agent_mention_resolves_across_channels_and_delete_skips_parsing(store, monkeypatch):
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
    # A main post may mention the agent working on another branch; an unknown id drops.
    with store.connection() as connection:
        _, mentions = _parse(connection, "project", "<@agent:chat:other-chat> <@agent:chat:nobody>")
    assert mentions == [{"kind": "agent", "id": "chat:other-chat"}]
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


def test_artifact_links_resolve_from_text_within_the_project(store):
    for artifact_id, project_id in (
        ("figure", "project"),
        ("foreign", "other"),
        ("lapsed", "project"),
    ):
        store.create_artifact(
            Artifact(
                artifact_id=artifact_id,
                project_id=project_id,
                supplier="turn",
                supplier_id="another-chat",
                source_name="figure.svg",
                media_type="image/svg+xml",
                created_at=store.now(),
                expires_at=store.now() if artifact_id == "lapsed" else None,
            ),
            data=b"<svg />",
        )
    # A repeated link counts once; a foreign, expired, or unknown id is not refused and
    # stays prose.
    first = post(
        store,
        "artifact",
        text="#evidence/one <artifact:figure> <artifact:figure> <artifact:foreign> <artifact:lapsed> <artifact:gone>",
    )
    expected = [
        {"artifact_id": "figure", "source_name": "figure.svg", "media_type": "image/svg+xml"}
    ]
    assert first["artifacts"] == expected
    assert first["refs"] == ["evidence/one"]
    assert store.list_bulletin("project", "main")["posts"][0]["artifacts"] == expected
    # Linking saves the artifact, so the link cannot expire under the post.
    assert store.artifact("figure").kept_at is not None
    assert store.artifact("lapsed").kept_at is None
    edited = store.execute_bulletin_command(
        "project", "turn", "edit", post_id=first["post_id"], key="edit-artifact", text="No link"
    )
    assert edited["post"]["artifacts"] == []
    # Links resolve at read time, so an artifact change refreshes open Bulletins.
    bulletin = (
        "SELECT generation FROM resource_versions WHERE scope='project' AND resource='bulletin'"
    )
    with store.connection() as connection:
        before = connection.execute(bulletin).fetchone()[0]
        connection.execute("DELETE FROM artifacts WHERE artifact_id='lapsed'")
        assert connection.execute(bulletin).fetchone()[0] > before


def test_agent_posts_to_named_channel_or_own_default(store):
    from rcp.core.transition_models import GraphTargetRef

    with store.connection() as connection:
        for branch_id, name in (("own", "Own"), ("other", "Other"), ("collision", "other")):
            connection.execute(
                "INSERT INTO graph_branches(project_id,branch_id,parent_target_json,parent_revision,created_by,created_at,name) VALUES('project',?,'{}',0,'member',?,?)",
                (branch_id, store.now(), name),
            )
        connection.execute(
            "UPDATE graph_runs SET graph_target_json=? WHERE operation_id='turn'",
            (GraphTargetRef(kind="branch", branch_id="own").model_dump_json(),),
        )
    own = post(store, "own")
    assert store.list_bulletin("project", "branch:own")["posts"] == [own]
    for selector in ("Other", "other", "branch:other", "main"):
        entry = post(store, selector, channel=selector)
        target_key = "main" if selector == "main" else "branch:other"
        assert store.list_bulletin("project", target_key)["posts"][0] == entry
    reply = post(store, "reply-main", reply_to=entry["post_id"])
    assert store.list_bulletin("project", "main")["posts"][0]["post_id"] == reply["post_id"]
    # A branch agent reads another channel through the same resolution.
    read = store.execute_bulletin_command("project", "turn", "read", channel="main")
    assert read == store.list_bulletin("project", "main")
    with pytest.raises(BulletinError, check=lambda exc: exc.code == "bulletin_channel_invalid"):
        post(store, "unknown", channel="missing")


def test_space_task_bulletin_requires_exact_project_binding(store):
    # Upgrade a task produced by the old project-chat writer; project identity
    # in the binding, rather than the bookkeeping row, authorizes this command.
    with store.connection() as connection:
        task = connection.execute("SELECT * FROM graph_runs WHERE operation_id='turn'").fetchone()
        connection.execute(
            "UPDATE graph_runs SET kind='space_chat', project_id=? WHERE operation_id='turn'",
            (store.space_id,),
        )
    with pytest.raises(BulletinError):
        post(store, "space")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO space_turn_project_bindings VALUES(?,?,?,?,?)",
            (
                "turn",
                "project",
                task["dispatch_authority_json"],
                "scope",
                task["graph_target_json"],
            ),
        )
    result = post(store, "space")
    assert result["text"] == "note"
    with pytest.raises(BulletinError):
        store.execute_bulletin_command("unbound", "turn", "read")


@pytest.mark.parametrize("identity", ["chat:c1", "episode:c1", "worker:c1", "space"])
def test_old_post_author_resolves_live_name_and_deleted_fallback(store, identity):
    scope, chat_id = "project", "c1"
    if identity == "space":
        thread = next(
            t
            for t in store.ensure_agent_threads(
                store.local_owner.user_id, [], default_names=("Agent",)
            )
            if t.kind == "space"
        )
        chat_id = thread.thread_id
        with store.connection() as connection:
            scope = connection.execute("SELECT space_id FROM space_identity").fetchone()[0]
        identity = f"chat:{chat_id}"
    original = post(store, "old")
    with store.connection() as connection:
        connection.execute(
            "UPDATE bulletin_posts SET author_actor_id=?, actor_label='Old writer' WHERE post_id=?",
            (identity, original["post_id"]),
        )
    store.set_chat_title(scope, chat_id, store.local_owner.user_id, "Renamed")
    assert store.list_bulletin("project", "main")["posts"][0]["author"]["actor_label"] == "Renamed"
    with store.connection() as connection:
        connection.execute(
            "DELETE FROM chat_display WHERE project_id=? AND chat_id=?", (scope, chat_id)
        )
    assert (
        store.list_bulletin("project", "main")["posts"][0]["author"]["actor_label"] == "Old writer"
    )


def test_post_author_names_are_batched_per_page(store, monkeypatch):
    expected = {}
    for index in range(8):
        entry = post(store, f"post-{index}")
        chat_id = f"author-{index}"
        title = f"Renamed {index}"
        store.set_chat_title("project", chat_id, store.local_owner.user_id, title)
        with store.connection() as connection:
            connection.execute(
                "UPDATE bulletin_posts SET author_actor_id=?, operation_id=NULL WHERE post_id=?",
                (f"chat:{chat_id}", entry["post_id"]),
            )
        expected[entry["post_id"]] = title
    queries = []
    connection_owner = store.connection

    @contextmanager
    def traced():
        with connection_owner() as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(store, "connection", traced)
    posts = store.list_bulletin("project", "main")["posts"]
    assert {entry["post_id"]: entry["author"]["actor_label"] for entry in posts} == expected
    assert len([query for query in queries if "chat_display" in query]) == 1
