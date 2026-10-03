from __future__ import annotations

from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.limits import LESSON_TEXT_MAX_CHARS

from .helpers import create_named_app


def test_lessons_crud_shapes_and_project_scope(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    url = f"/api/projects/{project_id}/lessons"
    assert client.get(url).json() == {"lessons": []}
    created = client.post(url, json={"text": "Use the project environment."})
    assert created.status_code == 200
    lesson = created.json()["lesson"]
    assert set(lesson) == {"lesson_id", "text", "created_at", "updated_at", "author", "human_owned"}
    owner = app.state.background_tasks.store.local_owner
    assert lesson["author"] == {
        "kind": "human",
        "user_id": owner.user_id,
        "display_name": owner.display_name,
    }
    assert lesson["human_owned"] is True
    assert client.get(url).json() == {"lessons": [lesson]}
    item_url = f"{url}/{lesson['lesson_id']}"
    updated = client.patch(item_url, json={"text": "Use uv for Python."})
    assert updated.status_code == 200
    assert updated.json()["lesson"]["lesson_id"] == lesson["lesson_id"]
    assert updated.json()["lesson"]["text"] == "Use uv for Python."
    assert updated.json()["lesson"]["human_owned"] is True
    assert client.post(url, json={"text": "x" * (LESSON_TEXT_MAX_CHARS + 1)}).status_code == 422
    assert client.post(url, json={"text": "  "}).status_code == 422
    assert client.delete(item_url).json() == {}
    assert client.get(url).json() == {"lessons": []}
    assert (
        client.patch(item_url, json={"text": "Missing"}).json()["detail"]["code"]
        == "lesson_not_found"
    )
    assert client.delete(item_url).status_code == 404


def test_lessons_require_membership_for_every_route(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    url = f"/api/projects/{project_id}/lessons"
    lesson_id = client.post(url, json={"text": "Private lesson"}).json()["lesson"]["lesson_id"]
    with store.connection() as connection:
        connection.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))
    for method, path, body in (
        ("GET", url, None),
        ("POST", url, {"text": "No access"}),
        ("PATCH", f"{url}/{lesson_id}", {"text": "No access"}),
        ("DELETE", f"{url}/{lesson_id}", None),
    ):
        assert client.request(method, path, json=body).status_code == 404
    assert len(store.list_lessons(project_id)) == 1


def test_lessons_mutations_require_named_identity_and_write_admission(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    url = f"/api/projects/{project_id}/lessons"
    assert client.get(url).status_code == 200
    for method, path in (("POST", url), ("PATCH", f"{url}/missing"), ("DELETE", f"{url}/missing")):
        assert client.request(method, path, json={"text": "Needs identity"}).status_code == 428
    store.rename_space_user(store.local_owner.user_id, "Member")
    lesson_id = client.post(url, json={"text": "Preserved"}).json()["lesson"]["lesson_id"]
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO project_transfer_requests (
                request_id, side, phase, project_id, source_space_id,
                target_space_id, record_json, revision, created_at, updated_at
            ) VALUES ('transfer', 'source', 'source_fenced', ?, ?, 'target', '{}', 1, ?, ?)
            """,
            (project_id, store.space_id, store.now(), store.now()),
        )
    for method, path in (
        ("POST", url),
        ("PATCH", f"{url}/{lesson_id}"),
        ("DELETE", f"{url}/{lesson_id}"),
    ):
        assert client.request(method, path, json={"text": "Refused"}).status_code == 409
    assert store.list_lessons(project_id)[0]["text"] == "Preserved"
