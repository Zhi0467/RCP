"""Seed and Refresh runs are retired; what an old writer stored still reads back."""

from __future__ import annotations

import json

import pytest

from rcp.history import HistoryManager
from rcp.storage import AgentTaskRecord
from tests.helpers import (
    create_named_app,
    seed_patch,
    signed_in_client,
    unchecked_write_authority,
)


@pytest.fixture
def app(manifest, tmp_path):
    return create_named_app(str(manifest.path), data_dir=tmp_path / "data")


@pytest.mark.parametrize(("kind", "status"), [("seed", "running"), ("refresh", "paused")])
def test_old_writer_task_row_loads_as_history_and_cannot_run_again(
    app, kind: str, status: str
) -> None:
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    now = store.now()
    operation_id = f"old-{kind}"
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind=kind,
            status=status,
            request={"run_truth_scope": ["repo-a"]},
            created_at=now,
            updated_at=now,
            status_message="The provider stopped.",
            native_session_id="old-native-session",
        )
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET dispatch_authority_json = ?, request_json = ? "
            "WHERE operation_id = ?",
            (
                json.dumps(
                    {
                        "profile": "ordinary",
                        "task_contract": "scratch_patch",
                        "scope": {"run_truth_scope": ["repo-a"], "patch_kind": kind},
                    }
                ),
                json.dumps({"run_truth_scope": ["repo-a"], "active_compute_ids": []}),
                operation_id,
            ),
        )

    record = store.agent_task(operation_id)
    assert record is not None
    assert record.kind == kind
    assert record.dispatch_authority is None
    assert record.request == {"run_truth_scope": ["repo-a"]}

    client = signed_in_client(app)
    shown = client.get(f"/api/projects/{project_id}/tasks/{operation_id}")
    assert shown.status_code == 200
    assert shown.json()["kind"] == kind
    assert shown.json()["active"] is False
    assert shown.json()["awaiting_human"] is False
    assert shown.json()["can_pause"] is False
    assert shown.json()["can_retry"] is False
    assert shown.json()["can_resume"] is False
    listed = client.get(f"/api/projects/{project_id}/tasks").json()
    assert operation_id in json.dumps(listed)

    assert (
        client.post(f"/api/projects/{project_id}/tasks/{operation_id}/retry", json={}).status_code
        == 409
    )
    assert (
        client.post(f"/api/projects/{project_id}/tasks/{operation_id}/resume", json={}).status_code
        == 409
    )
    assert client.post(f"/api/projects/{project_id}/tasks/{kind}", json={}).status_code == 405


@pytest.mark.parametrize("kind", ["seed", "refresh"])
def test_old_writer_ingestion_patch_replays_and_moves_the_watermark(manifest, kind: str) -> None:
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    history.append(seed_patch())
    raw = seed_patch().model_dump(mode="json")
    raw.update(
        kind=kind,
        revision=1,
        source_operation_id=f"old-{kind}",
        processed_cursors={"repo-a/laptop/codex": "2026-01-01T00:00:00Z"},
    )
    patch_dir = manifest.research_dir / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / "000001.json"
    patch_path.write_text(json.dumps(raw), encoding="utf-8")
    original = patch_path.read_bytes()

    result = history.materialize()

    assert result.state.replay_status == "complete", result.state.replay_failure
    assert result.state.revision == 1
    assert "rq/learning-after-shift" in result.state.nodes
    assert result.state.last_refresh_at == history.load_patches()[0].created_at
    assert history.load_patches()[0].kind == kind
    assert patch_path.read_bytes() == original
