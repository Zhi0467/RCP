from __future__ import annotations

from rcp.storage import AgentTaskRecord
from tests.helpers import create_named_app, signed_in_client
from tests.test_api_questions import _question


def test_other_members_private_thread_is_hidden_from_existing_routes(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    client = signed_in_client(app)
    thread = next(
        t
        for t in store.ensure_agent_threads("another-member", [project_id], default_names=("Pip",))
        if t.project_id == project_id
    )
    question = _question(store, project_id, owner_id=thread.thread_id)
    now = store.now()
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id="private-turn",
            project_id=project_id,
            kind="project_chat",
            status="failed",
            request={"chat_id": thread.thread_id, "message": "private"},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )
    base = f"/api/projects/{project_id}"
    for path in (
        f"chats/{thread.thread_id}",
        f"chats/{thread.thread_id}/questions",
        f"tasks/{task.operation_id}",
    ):
        assert client.get(f"{base}/{path}").status_code == 404
    for action in ("answer", "dismiss"):
        assert (
            client.post(f"{base}/questions/{question.question_id}/{action}", json={}).status_code
            == 404
        )
    assert not client.get(f"{base}/tasks").json()
    assert not client.get(f"{base}/chats?inventory=true").json()["items"]


def test_legacy_chat_without_thread_row_stays_public(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    client = signed_in_client(app)
    question = _question(store, project_id)
    base = f"/api/projects/{project_id}"
    assert client.get(f"{base}/chats/chat/questions").status_code == 200
    assert (
        client.post(f"{base}/questions/{question.question_id}/dismiss", json={}).status_code == 200
    )


def test_thread_attribution_is_projection_only_and_follows_parent(tmp_path):
    from rcp.storage import AppStore

    store = AppStore(tmp_path / "app.sqlite3")
    thread = store.ensure_agent_threads("member", ["project"], default_names=("Pip",))[1]
    now = store.now()
    parent = store.create_agent_task(
        AgentTaskRecord(
            operation_id="origin",
            project_id="project",
            kind="project_chat",
            status="failed",
            request={"chat_id": thread.thread_id},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )
    child = store.create_agent_task(
        parent.model_copy(
            update={
                "operation_id": "child",
                "parent_operation_id": parent.operation_id,
                "request": {},
            }
        )
    )
    expected = {"thread_id": thread.thread_id, "name": "Pip 2"}
    assert store.via_agent(parent.operation_id) == expected
    assert store.via_agent(child.operation_id) == expected
    started = store.create_agent_task(
        parent.model_copy(
            update={
                "operation_id": "experiment-root",
                "parent_operation_id": None,
                "request": {"started_from_operation_id": parent.operation_id},
            }
        )
    )
    assert store.via_agent(started.operation_id) == expected
    assert store.via_agent("old-public-operation") is None
    assert store.agent_task(parent.operation_id) == parent


def test_private_thread_cannot_fork_into_public_chat(tmp_path):
    import pytest

    from rcp.storage import AppStore

    store = AppStore(tmp_path / "app.sqlite3")
    thread = next(
        t
        for t in store.ensure_agent_threads("member", ["project"], default_names=("Pip",))
        if t.project_id
    )
    with pytest.raises(ValueError, match="private"):
        store.require_chat_fork_source("project", thread.thread_id)


def test_task_ownership_filter_precedes_recent_limit(tmp_path):
    from rcp.storage import AppStore

    store = AppStore(tmp_path / "app.sqlite3")
    private = next(
        t
        for t in store.ensure_agent_threads("other", ["project"], default_names=("Pip",))
        if t.project_id
    )
    for operation, chat, date in (
        ("public", "public-chat", "2026-01-01"),
        ("private", private.thread_id, "2026-01-02"),
    ):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation,
                project_id="project",
                kind="project_chat",
                status="failed",
                request={"chat_id": chat},
                created_at=date + "T00:00:00+00:00",
                updated_at=date + "T00:00:00+00:00",
                status_message="failed",
            )
        )
    assert [
        task.operation_id for task in store.agent_tasks("project", limit=1, user_id="viewer")
    ] == ["public"]


def test_saved_private_artifacts_are_owner_only(manifest, tmp_path):
    import uuid

    from rcp.artifacts import descriptor_for
    from rcp.storage import Artifact

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    client = signed_in_client(app)
    own = store.local_owner.user_id
    artifacts_by_owner = {}
    for owner in (own, "another-member", None):
        chat_id = (
            next(
                t.thread_id
                for t in store.ensure_agent_threads(owner, [project_id], default_names=("Pip",))
                if t.project_id
            )
            if owner
            else "public-chat"
        )
        operation_id = str(uuid.uuid4())
        now = store.now()
        kept = descriptor_for(operation_id, "legacy.html", media_type="text/html").model_copy(
            update={
                "kept_filename": "legacy.html",
                "kept_at": now,
            }
        )
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=project_id,
                kind="project_chat",
                status="succeeded",
                request={"chat_id": chat_id},
                created_at=now,
                updated_at=now,
                finished_at=now,
                status_message="Done",
                result={"artifacts": [kept.model_dump(mode="json")]},
            )
        )
        artifact = Artifact(
            artifact_id=uuid.uuid4().hex,
            project_id=project_id,
            supplier="turn",
            supplier_id=operation_id,
            origin_operation_id=operation_id,
            source_name="saved.html",
            media_type="text/html",
            created_at=now,
            kept_at=now,
        )
        store.create_artifact(artifact, data=b"<p>saved</p>")
        artifacts_by_owner[owner] = {artifact.artifact_id, kept.artifact_id}
        response = client.get(
            f"/api/projects/{project_id}/artifacts/{artifact.artifact_id}/content"
        )
        assert response.status_code == (404 if owner == "another-member" else 200)
    listed = client.get(f"/api/projects/{project_id}/artifacts")
    assert listed.status_code == 200
    assert {row["artifact_id"] for row in listed.json()} == (
        artifacts_by_owner[own] | artifacts_by_owner[None]
    )
