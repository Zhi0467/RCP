from __future__ import annotations

import pytest

from rcp.storage.artifact_models import Artifact
from tests.helpers import create_named_app, signed_in_client


@pytest.fixture
def comments_app(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    store = app.state.background_tasks.store
    project = app.state.default_project_id
    artifact = store.create_artifact(
        Artifact(
            artifact_id="comment-target",
            project_id=project,
            supplier="turn",
            supplier_id="turn",
            source_name="note.txt",
            media_type="text/plain",
            created_at=store.now(),
        ),
        data=b"A note",
    )
    return app, client, store, project, artifact


def _create(client, project, artifact, text="First note"):
    return client.post(
        f"/api/projects/{project}/comment-threads",
        json={
            "target": {"kind": "artifact", "id": artifact.artifact_id},
            "anchor": {"kind": "whole"},
            "text": text,
        },
    )


def test_comment_routes_lifecycle_and_mentions(comments_app):
    _, client, store, project, artifact = comments_app
    base = f"/api/projects/{project}"
    response = _create(client, project, artifact, f"Hello <@member:{store.local_owner.user_id}>")
    assert response.status_code == 200, response.text
    thread = response.json()["thread"]
    path = base + "/comment-threads/" + thread["thread_id"]
    assert client.get(base + "/comment-mentions").json()["mentions"]
    listed = client.get(
        base + "/comment-threads",
        params={
            "target_kind": "artifact",
            "target_id": artifact.artifact_id,
        },
    ).json()["threads"]
    assert listed[0]["thread_id"] == thread["thread_id"]
    assert client.get(path).status_code == 200
    assert client.post(path + "/resolve", json={}).json()["thread"]["state"] == "resolved"
    assert client.get(base + "/comment-mentions").json()["mentions"] == []
    reply = client.post(path + "/posts", json={"text": "Reply"})
    assert reply.status_code == 200, reply.text
    assert client.get(path).json()["thread"]["state"] == "open"
    post = thread["posts"][0]["post_id"]
    assert (
        client.patch(base + "/comment-posts/" + post, json={"text": "No mention"}).status_code
        == 200
    )
    assert client.get(base + "/comment-mentions").json()["mentions"] == []
    assert client.post(path + "/resolve", json={}).status_code == 200
    assert client.post(path + "/reopen", json={}).json()["thread"]["state"] == "open"
    assert client.delete(base + "/comment-posts/" + post).status_code == 200
    assert client.get(path).status_code == 404


def test_comment_post_budget_and_agent_tokens(comments_app):
    _, client, _, project, artifact = comments_app
    assert _create(client, project, artifact, "<@agent:chat:one>").status_code == 422
    response = _create(client, project, artifact, "x" * 2048)
    assert response.status_code == 200, response.text
    path = f"/api/projects/{project}/comment-threads/{response.json()['thread']['thread_id']}/posts"
    assert client.post(path, json={"text": "x" * 2048}).status_code == 200
    assert client.post(path, json={"text": "x" * 2048}).status_code == 422
    assert client.post(path, json={"text": "x" * 2049}).status_code == 422


@pytest.mark.parametrize("kind", ["artifact", "paper", "reply"])
def test_comment_target_not_readable_is_404(comments_app, kind):
    _, client, store, project, artifact = comments_app
    thread = _create(client, project, artifact).json()["thread"]
    with store.connection() as connection:
        connection.execute(
            "UPDATE comment_threads SET target_kind=? WHERE thread_id=?",
            (kind, thread["thread_id"]),
        )
    from rcp.storage import AgentTaskRecord

    private_chat = next(
        chat
        for chat in store.ensure_agent_threads("another-member", [project], default_names=("Pip",))
        if chat.project_id == project
    )
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=artifact.artifact_id,
            project_id=project,
            kind="project_chat",
            status="failed",
            request={"chat_id": private_chat.thread_id},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="failed",
        )
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=json_set(metadata, '$.chat_id', ?) WHERE artifact_id=?",
            (private_chat.thread_id, artifact.artifact_id),
        )
    base = f"/api/projects/{project}"
    path = base + "/comment-threads/" + thread["thread_id"]
    assert (
        client.get(
            base + "/comment-threads",
            params={"target_kind": kind, "target_id": artifact.artifact_id},
        ).status_code
        == 404
    )
    assert client.get(path).status_code == 404
    assert (
        client.post(
            base + "/comment-threads",
            json={
                "target": {"kind": kind, "id": artifact.artifact_id},
                "anchor": {"kind": "whole"},
                "text": "private",
            },
        ).status_code
        == 404
    )
    assert client.post(path + "/posts", json={"text": "no"}).status_code == 404
    assert client.post(path + "/resolve", json={}).status_code == 404
    assert client.post(path + "/reopen", json={}).status_code == 404
    post_path = base + "/comment-posts/" + thread["posts"][0]["post_id"]
    assert client.patch(post_path, json={"text": "no"}).status_code == 404
    assert client.delete(post_path).status_code == 404


def test_read_only_project_keeps_reads_and_refuses_comment_writes(comments_app, monkeypatch):
    from rcp.history.manager import ReplayHalted

    app, client, _, project, artifact = comments_app
    thread = _create(client, project, artifact).json()["thread"]
    service = app.state.catalog.open(project)
    state = service.history.current_materialization().state

    def read_only(*_args, **_kwargs):
        raise ReplayHalted(state)

    monkeypatch.setattr(service.history, "require_writable", read_only)
    base = f"/api/projects/{project}"
    path = base + "/comment-threads/" + thread["thread_id"]
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["thread"]["can_send"] is False
    assert _create(client, project, artifact).status_code == 409
    assert client.post(path + "/posts", json={"text": "no"}).status_code == 409
    assert client.post(path + "/resolve", json={}).status_code == 409
    assert client.post(path + "/reopen", json={}).status_code == 409
    post_path = base + "/comment-posts/" + thread["posts"][0]["post_id"]
    assert client.patch(post_path, json={"text": "no"}).status_code == 409
    assert client.delete(post_path).status_code == 409


def test_artifact_anchor_must_match_media_type(comments_app):
    _, client, _, project, artifact = comments_app
    response = client.post(
        f"/api/projects/{project}/comment-threads",
        json={
            "target": {"kind": "artifact", "id": artifact.artifact_id},
            "anchor": {"kind": "pdf", "page": 1},
            "text": "PDF on text",
        },
    )
    assert response.status_code == 422, response.text


def test_comment_posts_are_author_only(comments_app):
    _, client, store, project, artifact = comments_app
    thread = _create(client, project, artifact).json()["thread"]
    post_id = thread["posts"][0]["post_id"]
    with store.connection() as connection:
        connection.execute("UPDATE comment_posts SET author_id='other' WHERE post_id=?", (post_id,))
    path = f"/api/projects/{project}/comment-posts/{post_id}"
    assert client.patch(path, json={"text": "not mine"}).status_code == 403
    assert client.delete(path).status_code == 403


@pytest.mark.parametrize("report", [False, True])
def test_can_send_needs_named_identity_and_target_chat(comments_app, report):
    from rcp.storage import AgentTaskRecord

    _, client, store, project, artifact = comments_app
    response = _create(client, project, artifact)
    thread = response.json()["thread"]
    path = f"/api/projects/{project}/comment-threads/{thread['thread_id']}"
    assert thread["can_send"] is False
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="origin",
            project_id=project,
            kind="project_chat",
            status="succeeded",
            request={"chat_id": "public-chat"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="completed",
        )
    )
    if report:
        origin = store.agent_task("origin")
        assert origin is not None
        store.create_agent_task(origin.model_copy(update={"operation_id": "report"}))
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET kind='episode_report', parent_operation_id='origin', "
                "request_json='{}' WHERE operation_id='report'"
            )
    with store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=json_set(metadata, '$.origin_operation_id', ?) WHERE artifact_id=?",
            ("report" if report else "origin", artifact.artifact_id),
        )
    assert client.get(path).json()["thread"]["chat_id"] == "public-chat"
    assert client.get(path).json()["thread"]["can_send"] is True
    own_chat = next(
        chat
        for chat in store.ensure_agent_threads(
            store.local_owner.user_id, [project], default_names=("Pip",)
        )
        if chat.project_id == project
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=json_set(metadata, '$.chat_id', ?) WHERE artifact_id=?",
            (own_chat.thread_id, artifact.artifact_id),
        )
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id='origin'",
            (f'{{"chat_id":"{own_chat.thread_id}"}}',),
        )
    assert client.get(path).json()["thread"]["can_send"] is True
    with store.connection() as connection:
        connection.execute(
            "UPDATE space_users SET display_name=NULL WHERE user_id=?", (store.local_owner.user_id,)
        )
    assert client.get(path).json()["thread"]["can_send"] is False


def test_mentions_include_target_and_member_names(comments_app):
    _, client, store, project, artifact = comments_app
    owner = store.local_owner
    _create(client, project, artifact, f"Hello <@member:{owner.user_id}>")
    row = client.get(f"/api/projects/{project}/comment-mentions").json()["mentions"][0]
    assert row["target_name"] == artifact.source_name
    assert {"user_id": owner.user_id, "display_name": owner.display_name} in row["members"]


@pytest.mark.parametrize("kind", ["paper", "reply"])
def test_mentions_name_paper_and_reply_targets(comments_app, kind):
    from rcp.storage import AgentTaskRecord

    _, client, store, project, _ = comments_app
    user_id = store.local_owner.user_id
    if kind == "paper":
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO paper_files VALUES (?, 'target', 'sections/method.md', 'text', ?, NULL, NULL)",
                (project, store.now()),
            )
        expected = "sections/method.md"
    else:
        store.create_agent_task(
            AgentTaskRecord(
                operation_id="target",
                project_id=project,
                kind="project_chat",
                status="succeeded",
                request={"chat_id": "chat", "message": "Method discussion"},
                created_at=store.now(),
                updated_at=store.now(),
                status_message="completed",
            )
        )
        expected = "Method discussion"
    response = client.post(
        f"/api/projects/{project}/comment-threads",
        json={
            "target": {"kind": kind, "id": "target"},
            "anchor": {"kind": "whole"},
            "text": f"<@member:{user_id}>",
        },
    )
    assert response.status_code == 200, response.text
    if kind == "reply":
        assert response.json()["thread"]["chat_id"] == "chat"
    row = client.get(f"/api/projects/{project}/comment-mentions").json()["mentions"][0]
    assert row["target_name"] == expected
