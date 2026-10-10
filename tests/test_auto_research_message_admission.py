"""An ended orchestrator's composer exposes the same durable refusal as admission."""

from __future__ import annotations

import uuid

import pytest

from rcp.core.models import EpisodeIsolation
from tests.helpers import signed_in_client
from tests.test_ended_orchestrator_messages import _ended_app
from tests.test_episode_api import create_terminal_auto_episode


@pytest.mark.parametrize(
    ("condition", "code"),
    [
        ("session", "auto_research_session_unavailable"),
        ("stage", "auto_research_session_unavailable"),
        ("history", "auto_research_session_unavailable"),
        ("occupied", "auto_research_project_occupied"),
        ("merge", "episode_merge_reserved"),
        ("removed", "episode_isolation_unavailable"),
        ("wrapping_up", "auto_research_not_ended"),
        ("active", "auto_research_turn_active"),
    ],
)
def test_message_refusal_is_projected_and_enforced(
    manifest, tmp_path, monkeypatch, condition, code
):
    app, store, episode, url = _ended_app(manifest, tmp_path, monkeypatch)
    with signed_in_client(app) as client:
        if condition in {"session", "stage", "history", "active"}:
            updates = {
                "session": "native_session_id = NULL",
                "stage": "stage_root = NULL",
                "history": "history_only = 1",
                "active": "status = 'running'",
            }
            with store.connection() as connection:
                connection.execute(
                    f"UPDATE graph_runs SET {updates[condition]} WHERE operation_id = ?",
                    (episode.root_operation_id,),
                )
        elif condition == "occupied":
            other, _, _ = create_terminal_auto_episode(
                store,
                app.state.catalog.open(episode.project_id).history,
                episode.project_id,
                episode_id="another-episode",
                report_error="Unavailable.",
            )
            with store.connection() as connection:
                connection.execute(
                    "UPDATE episodes SET status = 'running', ending = NULL, "
                    "wrapup_state = 'not_started', ended_at = NULL WHERE episode_id = ?",
                    (other.episode_id,),
                )
        elif condition == "wrapping_up":
            with store.connection() as connection:
                connection.execute(
                    "UPDATE episodes SET status = 'wrapping_up' WHERE episode_id = ?",
                    (episode.episode_id,),
                )
        else:
            store.create_episode_isolation(
                episode.project_id,
                EpisodeIsolation(
                    owner_episode_id=episode.episode_id, graph_branch_id=episode.episode_id
                ),
            )
            store.set_episode_isolation_status(
                episode.project_id, episode.episode_id, expected_status="creating", status="ready"
            )
            store.set_episode_isolation_status(
                episode.project_id,
                episode.episode_id,
                expected_status="ready",
                status="merging" if condition == "merge" else "removing",
            )
            if condition == "removed":
                store.set_episode_isolation_status(
                    episode.project_id,
                    episode.episode_id,
                    expected_status="removing",
                    status="removed",
                )
        projected = client.get(f"/api/projects/{episode.project_id}/episodes")
        assert projected.status_code == 200, projected.text
        item = next(row for row in projected.json() if row["episode_id"] == episode.episode_id)
        assert item["can_message"] is False
        assert item["message_refusal"]["code"] == code
        response = client.post(url, json={"body": "Continue.", "request_id": str(uuid.uuid4())})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == code
    assert store.episode_continuation(episode.episode_id) is None
    assert store.auto_research_messages(episode.episode_id) == []
