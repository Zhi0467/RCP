from __future__ import annotations

import json

import pytest

from rcp.storage import AgentTaskRecord, AppStore
from tests.helpers import create_named_app, signed_in_client

_OLD_COACH_AUTHORITY = {
    "profile": "ordinary",
    "scope": {
        "run_truth_scope": [],
        "episode_id": None,
        "chat_scope": None,
        "chat_id": None,
        "node_id": None,
        "patch_kind": None,
        "control_node_id": None,
        "control_episode_id": None,
    },
}


def _old_coach_task(
    store: AppStore, project_id: str, status: str, contract: str = "paper_suggest"
) -> str:
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=f"coach-{status}",
            project_id=project_id,
            kind="paper_coach",
            status=status,
            request={"message": "Tighten the abstract.", "mode": "review", "run_on": "laptop"},
            created_at=now,
            updated_at=now,
            status_message="Stored coach turn.",
            result={"messages": ["The abstract is shorter now."]},
        )
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET dispatch_authority_json=? WHERE operation_id=?",
            (json.dumps({**_OLD_COACH_AUTHORITY, "task_contract": contract}), f"coach-{status}"),
        )
    return f"coach-{status}"


@pytest.mark.parametrize("contract", ["paper_readonly", "paper_suggest"])
def test_old_coach_rows_load_without_their_retired_contract(tmp_path, contract) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    operation_id = _old_coach_task(store, "project", "succeeded", contract)
    store.close()
    store = AppStore(tmp_path / "rcp.sqlite3")

    record = store.agent_task(operation_id)

    assert record is not None
    assert record.kind == "paper_coach"
    assert record.dispatch_authority is None
    assert record.request["message"] == "Tighten the abstract."
    assert record.result == {"messages": ["The abstract is shorter now."]}


def test_coach_turns_neither_start_nor_run_again(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.service.history.project_id
    operation_id = _old_coach_task(store, project_id, "interrupted")
    with signed_in_client(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/tasks/paper_coach",
            json={"message": "Review the introduction."},
        )
        listed = client.get(f"/api/projects/{project_id}/tasks/{operation_id}")
        resumed = client.post(f"/api/projects/{project_id}/tasks/{operation_id}/resume", json={})

    assert started.status_code == 405
    assert listed.status_code == 200
    assert listed.json()["kind"] == "paper_coach"
    assert resumed.status_code == 409
