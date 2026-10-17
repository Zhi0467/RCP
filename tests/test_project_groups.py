from __future__ import annotations

import pytest

from rcp.storage import ProjectRecord

from .helpers import create_named_app, signed_in_client
from .test_project_membership import _create_project, _team_app


def test_group_visibility_follows_live_project_membership(tmp_path):
    _, client, store, people, acting = _team_app(tmp_path)
    group = client.post("/api/project-groups", json={"name": "Physics"}).json()
    group_id = group["id"]
    assert set(group) == {"id", "name", "created_at"}
    assert [item["id"] for item in client.get("/api/project-groups").json()] == [group_id]
    acting[0] = people[1].user_id
    assert client.get("/api/project-groups").json() == []
    acting[0] = people[0].user_id
    project_id = _create_project(client, tmp_path / "project")
    assert (
        client.put(f"/api/projects/{project_id}/group", json={"group_id": group_id}).status_code
        == 204
    )
    acting[0] = people[1].user_id
    assert client.get("/api/projects").json() == []
    assert client.get("/api/project-groups").json() == []
    store.seat_project_member(project_id, people[1].user_id)
    assert [item["id"] for item in client.get("/api/project-groups").json()] == [group_id]


def test_move_requires_project_membership_and_visible_group(tmp_path):
    _, client, _, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "project")
    acting[0] = people[1].user_id
    group_id = client.post("/api/project-groups", json={"name": "Private empty"}).json()["id"]
    assert (
        client.put(f"/api/projects/{project_id}/group", json={"group_id": group_id}).status_code
        == 404
    )
    acting[0] = people[0].user_id
    response = client.put(f"/api/projects/{project_id}/group", json={"group_id": group_id})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "project_group_not_found"


def test_group_lifecycle_cards_and_registration_preserve_assignment(tmp_path):
    _, client, store, _, _ = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "project")
    group_id = client.post("/api/project-groups", json={"name": "Physics"}).json()["id"]
    assert (
        client.patch(f"/api/project-groups/{group_id}", json={"name": "Datasets"}).status_code
        == 200
    )
    assert (
        client.put(f"/api/projects/{project_id}/group", json={"group_id": group_id}).status_code
        == 204
    )
    record = store.project(project_id)
    assert record is not None
    store.upsert_project(ProjectRecord.model_validate({**record.model_dump(), "group_id": None}))
    assert client.get("/api/projects").json()[0]["group_id"] == group_id
    response = client.delete(f"/api/project-groups/{group_id}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_group_not_empty"
    assert (
        client.put(f"/api/projects/{project_id}/group", json={"group_id": None}).status_code == 204
    )
    assert client.get("/api/projects").json()[0]["group_id"] is None
    assert client.delete(f"/api/project-groups/{group_id}").status_code == 204
    assert client.delete(f"/api/project-groups/{group_id}").status_code == 404


@pytest.mark.parametrize("name", ["", " " * 2, "x" * 201])
def test_invalid_group_names_use_structured_error(tmp_path, name):
    _, client, _, _, _ = _team_app(tmp_path)
    response = client.post("/api/project-groups", json={"name": name})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "project_group_name_invalid"


def test_group_mutations_invalidate_other_clients_settings(tmp_path):
    _, client, store, _, _ = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "project")

    def generation():
        with store.connection() as connection:
            row = connection.execute(
                "SELECT generation FROM resource_versions WHERE scope='' AND resource='settings' AND entity=''"
            ).fetchone()
            return row[0] if row else 0

    previous = generation()
    response = client.post("/api/project-groups", json={"name": "Physics"})
    assert response.status_code == 201
    group_id = response.json()["id"]
    mutations = [
        lambda: client.patch(f"/api/project-groups/{group_id}", json={"name": "New"}),
        lambda: client.put(f"/api/projects/{project_id}/group", json={"group_id": group_id}),
        lambda: client.put(f"/api/projects/{project_id}/group", json={"group_id": None}),
        lambda: client.delete(f"/api/project-groups/{group_id}"),
    ]
    assert generation() > previous
    for mutate in mutations:
        previous = generation()
        assert mutate().is_success
        assert generation() > previous


def test_personal_registration_accepts_group_and_rejects_missing_group_before_registration(
    tmp_path,
):
    app = create_named_app(data_dir=tmp_path / "data")
    client = signed_in_client(app)
    group_id = client.post("/api/project-groups", json={"name": "Physics"}).json()["id"]
    project_id = _create_project(client, tmp_path / "project")
    locator = client.get("/api/projects").json()[0]["locator"]
    response = client.post("/api/projects", json={"locator": locator, "group_id": group_id})
    assert response.status_code == 200
    assert response.json()["group_id"] == group_id
    assert client.post("/api/projects", json={"locator": locator}).json()["group_id"] == group_id
    response = client.post("/api/projects", json={"locator": "missing", "group_id": "missing"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "project_group_not_found"
    assert client.get("/api/projects").json()[0]["id"] == project_id
