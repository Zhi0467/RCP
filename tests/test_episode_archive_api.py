from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rcp.core.models import AuthorizedHuman
from rcp.storage import AppStore, EpisodeRecord

from .helpers import authorized_human, create_named_app
from .test_episode_api import create_recoverable_auto_episode
from .test_episode_storage import _operational_task
from .test_project_membership import _create_project, _team_app
from .test_project_transfer_request_api import _source_project


def _episode(
    store: AppStore,
    project_id: str,
    authorizer: AuthorizedHuman,
    *,
    ended: bool = True,
    created_at: str | None = None,
) -> EpisodeRecord:
    episode_id = str(uuid.uuid4())
    created_at = created_at or store.now()
    episode = store.create_episode(
        EpisodeRecord(
            episode_id=episode_id,
            project_id=project_id,
            mode="experiment_loop",
            control_node_id=f"exp/{episode_id}",
            status="queued",
            invocation_ceiling=2,
            authorized_by=authorizer,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    if ended:
        return store.end_episode_without_report(
            episode_id, ending="failed", diagnostic="Retained launch failure."
        )
    return episode


def test_archive_round_trip_survives_restart_without_changing_episode_or_graph(
    manifest, tmp_path
) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    episode = _episode(store, project_id, authorized_human(store))
    base = f"/api/projects/{project_id}/episodes"
    client = TestClient(app)
    history_head = app.state.catalog.open(project_id).history.head_ref()

    before = client.get(base, params={"episode_id": episode.episode_id}).json()[0]
    assert before["can_archive"] is True
    assert before["archived"] is False
    archived = client.post(f"{base}/{episode.episode_id}/archive", json={"archived": True})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    assert archived.json()["authorized_by"] == episode.authorized_by.model_dump(mode="json")
    assert store.episode(episode.episode_id) == episode
    assert app.state.catalog.open(project_id).history.head_ref() == history_head

    reopened = create_named_app(str(manifest.path), data_dir=data_dir)
    restarted = TestClient(reopened)
    retained = restarted.get(base).json()
    assert next(item for item in retained if item["episode_id"] == episode.episode_id)["archived"]
    for _ in range(2):
        restored = restarted.post(f"{base}/{episode.episode_id}/archive", json={"archived": False})
        assert restored.status_code == 200, restored.text
        assert restored.json()["archived"] is False
        assert restored.json()["status"] == "failed"
        assert restored.json()["ending_diagnostic"] == "Retained launch failure."
    assert reopened.state.background_tasks.store.episode(episode.episode_id) == episode


@pytest.mark.parametrize("mode", ["experiment_loop", "auto_research"])
@pytest.mark.parametrize(
    ("status", "task_status", "ending", "wrapup_state"),
    [
        ("queued", "queued", None, "not_started"),
        ("running", "running", None, "not_started"),
        ("stopping", "pausing", None, "not_started"),
        ("wrapping_up", "succeeded", "completed", "running"),
        ("needs_action", "paused", None, "not_started"),
        ("completed", "succeeded", "completed", "ready"),
        ("stopped", "succeeded", "stopped", "skipped"),
        ("failed", "failed", "failed", "failed"),
    ],
)
def test_every_episode_state_can_archive_without_changing_retained_work_or_projection(
    manifest, tmp_path, mode, status, task_status, ending, wrapup_state
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    history = app.state.catalog.open(project_id).history
    if mode == "auto_research":
        episode, task = create_recoverable_auto_episode(
            store,
            history,
            project_id,
            episode_id="archivable-auto-episode",
            status="queued",
            stage=tmp_path / "retained-stage",
        )
    else:
        episode = _episode(store, project_id, authorized_human(store), ended=False)
        episode, _invocation, task = store.allocate_episode_invocation(
            episode.episode_id,
            _operational_task(
                store,
                "retained-turn",
                episode_id=episode.episode_id,
                project_id=project_id,
            ),
        )
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status = ?, ending = ?, wrapup_state = ? WHERE episode_id = ?",
            (status, ending, wrapup_state, episode.episode_id),
        )
        connection.execute(
            "UPDATE graph_runs SET status = ? WHERE operation_id = ?",
            (task_status, task.operation_id),
        )
    retained = (
        store.episode(episode.episode_id),
        store.episode_tasks(episode.episode_id, include_hidden=True),
        store.episode_invocations(episode.episode_id),
    )
    main_head = history.head_ref()
    client = TestClient(app)
    base = f"/api/projects/{project_id}/episodes"
    path = f"{base}/{episode.episode_id}/archive"
    before_response = client.get(base, params={"episode_id": episode.episode_id})
    assert before_response.status_code == 200, before_response.text
    [before] = before_response.json()
    assert before["can_archive"] is True
    assert before["archived"] is False
    assert before["status"] == status
    assert before["tasks"]

    for archived in (True, False):
        response = client.post(path, json={"archived": archived})
        assert response.status_code == 200, response.text
        assert response.json() == {**before, "archived": archived}
        assert client.get(base, params={"episode_id": episode.episode_id}).json() == [
            {**before, "archived": archived}
        ]
        assert (
            store.episode(episode.episode_id),
            store.episode_tasks(episode.episode_id, include_hidden=True),
            store.episode_invocations(episode.episode_id),
        ) == retained
        assert history.head_ref() == main_head


def test_archive_rejects_invalid_body_without_changing_visibility(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    episode = _episode(store, project_id, authorized_human(store), ended=False)
    client = TestClient(app)
    path = f"/api/projects/{project_id}/episodes/{episode.episode_id}/archive"

    for invalid in ({"archived": "true"}, {"archived": True, "status": "stopped"}, {}):
        assert client.post(path, json=invalid).status_code == 422
    assert store.episode(episode.episode_id) == episode
    assert store.episode_archive_states(project_id)[episode.episode_id].archived is False


@pytest.mark.parametrize("archived", [True, False])
def test_archive_and_unarchive_preserve_project_write_admission(
    manifest, tmp_path, monkeypatch, archived
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    actor = authorized_human(store)
    episode = _episode(store, project_id, actor, ended=False)
    if not archived:
        store.set_episode_archived(project_id, episode.episode_id, actor.user_id, archived=True)

    def refuse_transferring_project(checked_project_id: str) -> None:
        assert checked_project_id == project_id
        raise ValueError("This project is moving to its admitted team space.")

    monkeypatch.setattr(store, "require_project_accepts_new_work", refuse_transferring_project)
    response = TestClient(app).post(
        f"/api/projects/{project_id}/episodes/{episode.episode_id}/archive",
        json={"archived": archived},
    )
    assert response.status_code == 409, response.text
    assert "moving to its admitted team space" in response.json()["detail"]
    assert store.episode_archive_states(project_id)[episode.episode_id].archived is not archived
    assert store.episode(episode.episode_id) == episode


def test_archived_history_remains_in_episode_list_after_recent_limit(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    authorizer = authorized_human(store)
    now = datetime.fromisoformat(store.now())
    old = _episode(store, project_id, authorizer, created_at=(now - timedelta(days=10)).isoformat())
    store.set_episode_archived(project_id, old.episode_id, authorizer.user_id, archived=True)
    for offset in range(51):
        _episode(
            store,
            project_id,
            authorizer,
            created_at=(now - timedelta(minutes=offset)).isoformat(),
        )

    client = TestClient(app)
    for params in ({}, {"mode": "experiment_loop"}):
        response = client.get(f"/api/projects/{project_id}/episodes", params=params)
        assert response.status_code == 200, response.text
        retained = [item for item in response.json() if item["episode_id"] == old.episode_id]
        assert len(retained) == 1 and retained[0]["archived"] is True


def test_team_archive_is_shared_and_preserves_initiator_when_another_member_restores(
    tmp_path,
) -> None:
    _app, client, store, people, acting = _team_app(tmp_path, members=3)
    starter, teammate, outsider = people
    project_id = _create_project(client, tmp_path / "repo", seat_member=starter.user_id)
    store.seat_project_member(project_id, teammate.user_id, seated_by=starter.user_id)
    authorizer = AuthorizedHuman(
        space_id=store.space_id, user_id=starter.user_id, display_name=starter.display_name
    )
    episode = _episode(store, project_id, authorizer, ended=False)
    base = f"/api/projects/{project_id}/episodes"
    path = f"{base}/{episode.episode_id}/archive"

    acting[0] = teammate.user_id
    archived = client.post(path, json={"archived": True})
    assert archived.status_code == 200, archived.text
    acting[0] = starter.user_id
    [seen] = client.get(base).json()
    assert seen["archived"] is True
    assert seen["authorized_by"] == authorizer.model_dump(mode="json")
    restored = client.post(path, json={"archived": False})
    assert restored.status_code == 200, restored.text
    acting[0] = teammate.user_id
    [seen] = client.get(base).json()
    assert seen["archived"] is False
    assert seen["authorized_by"] == authorizer.model_dump(mode="json")

    acting[0] = outsider.user_id
    assert client.get(base).status_code == 404
    assert client.post(path, json={"archived": True}).status_code == 404


@pytest.mark.parametrize("archived", [True, False])
def test_transfer_preparation_advertises_archived_history_only_when_present(
    tmp_path, archived
) -> None:
    from rcp.api.project_provisioning import ProjectTransferSourceConfigurationRequest

    app = create_named_app(data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = _source_project(app, tmp_path / "project")
    actor = authorized_human(store)
    episode = _episode(store, project_id, actor)
    if archived:
        store.set_episode_archived(project_id, episode.episode_id, actor.user_id, archived=True)

    response = TestClient(app).post(
        "/api/project-transfers/source-requests",
        json={
            "request_id": str(uuid.uuid4()),
            "project_id": project_id,
            "target_space_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    configuration = response.json()["source_configuration"]
    if archived:
        assert configuration["record_schema_version"] == 2
    else:
        assert "record_schema_version" not in configuration
    accepted = ProjectTransferSourceConfigurationRequest.model_validate(
        configuration
    ).storage_model()
    assert accepted.model_dump(mode="json") == configuration
    assert response.json()["source_release_receipt"] is None


@pytest.mark.parametrize("archived", [True, False])
def test_archive_cannot_address_an_episode_through_a_different_project(
    manifest, tmp_path, archived
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    episode = _episode(store, app.state.default_project_id, authorized_human(store))
    client = TestClient(app)
    other_id = _create_project(client, tmp_path / "another-repo")
    response = client.post(
        f"/api/projects/{other_id}/episodes/{episode.episode_id}/archive",
        json={"archived": archived},
    )
    assert response.status_code == 404, response.text
