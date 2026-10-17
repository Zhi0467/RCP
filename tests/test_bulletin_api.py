from __future__ import annotations

from tests.helpers import create_named_app, signed_in_client


def test_bulletin_member_identity_and_auth(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    url = f"/api/projects/{project_id}/bulletin"
    assert client.get(url).json()["roster"] == []
    response = client.post(url, json={"text": "Observation", "key": "post-1"})
    assert response.status_code == 200, response.text
    post = response.json()["post"]
    assert post["author"]["user_id"] == store.local_owner.user_id
    assert client.get(url).json()["posts"] == [post]
    assert client.post(url, json={"text": "No", "key": "post-2", "author": {}}).status_code == 422
    assert client.get(url + "?branch_id=missing").status_code == 404
    with store.connection() as connection:
        connection.execute("DELETE FROM project_members WHERE project_id=?", (project_id,))
    assert client.get(url).status_code == 404
    assert client.post(url, json={"text": "No", "key": "post-2"}).status_code == 404


def test_bulletin_roster_cap_and_post_admission(manifest, tmp_path, monkeypatch) -> None:
    from rcp.limits import BULLETIN_ROSTER_MAX_COUNT

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    store = app.state.background_tasks.store
    url = f"/api/projects/{app.state.default_project_id}/bulletin"
    monkeypatch.setattr(
        "rcp.api.bulletin.bulletin_roster",
        lambda *_: [{"id": str(i)} for i in range(BULLETIN_ROSTER_MAX_COUNT + 1)],
    )
    page = client.get(url).json()
    assert len(page["roster"]) == BULLETIN_ROSTER_MAX_COUNT
    assert page["roster_omitted"] == 1
    assert page["can_post"] is True

    def refuse(_project):
        raise ValueError("fenced")

    monkeypatch.setattr(store, "require_project_accepts_new_work", refuse)
    assert client.get(url).json()["can_post"] is False
    assert client.post(url, json={"key": "blocked", "text": "note"}).status_code == 409
