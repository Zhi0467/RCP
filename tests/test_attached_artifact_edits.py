from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from rcp.agents import AgentLauncher
from rcp.api.dependencies import get_graph_service
from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.work import stream_work_run
from rcp.storage import Artifact
from tests.helpers import (
    create_named_app,
    discuss_turn,
    launch_contract_path,
    signed_in_client,
    wait_for_task_response,
)
from tests.test_chat_prompt_protocol import _RecordingLauncher


def _saved_report(store, project_id):
    artifact = store.create_artifact(
        Artifact(
            artifact_id="a" * 24,
            project_id=project_id,
            supplier="turn",
            supplier_id="origin",
            source_name="report.txt",
            display_title="Report",
            media_type="text/plain",
            created_at=store.now(),
            expires_at=None,
            kept_at=store.now(),
        ),
        data=b"first draft",
    )
    return artifact


class _Editor(_RecordingLauncher):
    def __init__(self, text: bytes | None):
        super().__init__("editor-session")
        self.text = text
        self.seen: list[bytes] = []

    async def stream(self, provider, prompt, **kwargs):
        tracked = Path(kwargs["cwd"]) / "turns" / "origin" / "artifacts" / "report.txt"
        contract = launch_contract_path(prompt)
        assert f"`{tracked}`" in prompt + (contract.read_text() if contract.is_file() else "")
        self.seen.append(tracked.read_bytes())
        if self.text is not None:
            tracked.write_bytes(self.text)
        async for event in super().stream(provider, prompt, **kwargs):
            yield event


@pytest.mark.parametrize("mode", ["discuss", "work"])
@pytest.mark.parametrize("edits", [True, False])
def test_a_chat_turn_edits_an_attached_saved_artifact_in_place(manifest, tmp_path, mode, edits):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    artifact = _saved_report(store, project_id)
    launcher = _Editor(b"second draft" if edits else None)

    async def stream(_project_id, _kind, request, execution):
        service = get_graph_service(app.state.catalog, project_id, None)
        run = stream_work_run if request.mode == "work" else stream_discuss_run
        async for frame in run(service, launcher, request, tmp_path / "data", execution=execution):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    url = f"/api/projects/{project_id}/tasks/project_chat"
    body = {
        "chat_id": str(uuid4()),
        "message": "Tighten the report",
        "references": [{"kind": "artifact", "artifact_id": artifact.artifact_id}],
    }
    response = (
        discuss_turn(client, url, body)
        if mode == "discuss"
        else client.post(url, json=body | {"mode": mode})
    )
    assert response.status_code == 202, response.text
    task = wait_for_task_response(client, project_id, response.json()["operation_id"])
    assert task["status"] == "succeeded", task.get("error")
    assert launcher.seen == [b"first draft"]
    versions = store.artifact_versions(artifact.artifact_id)
    if edits:
        assert store.read_artifact_bytes(artifact.artifact_id) == b"second draft"
        assert len(versions) == 2
        assert [item["status"] for item in task["result"]["artifact_edit_outcomes"]] == [
            "published"
        ]
    else:
        assert len(versions) == 1
        assert not task["result"].get("artifact_edit_outcomes")


@pytest.mark.parametrize("edits", [True, False])
def test_a_retry_never_publishes_its_attached_version_over_a_newer_one(manifest, tmp_path, edits):
    from rcp.agents import AgentEvent

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    artifact = _saved_report(store, project_id)

    class FailsFirst(_Editor):
        async def stream(self, provider, prompt, **kwargs):
            if not self.seen:
                self.seen.append(b"")
                yield AgentEvent(event="error", text="Provider failed")
                return
            async for event in super().stream(provider, prompt, **kwargs):
                yield event

    launcher = FailsFirst(b"agent draft" if edits else None)

    async def stream(_project_id, _kind, request, execution):
        service = get_graph_service(app.state.catalog, project_id, None)
        async for frame in stream_discuss_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    response = discuss_turn(
        client,
        f"/api/projects/{project_id}/tasks/project_chat",
        {
            "chat_id": str(uuid4()),
            "message": "Tighten the report",
            "references": [{"kind": "artifact", "artifact_id": artifact.artifact_id}],
        },
    )
    assert response.status_code == 202, response.text
    failed = wait_for_task_response(client, project_id, response.json()["operation_id"])
    assert failed["status"] == "failed"
    newer = store.publish_artifact_version(
        artifact.artifact_id,
        base_version=artifact.current_version,
        operation_id="viewer-edit",
        data=b"human draft",
    )
    retried = app.state.background_tasks.retry(
        failed["operation_id"],
        authorized_by=store.agent_task(failed["operation_id"]).authorized_by,
    )
    task = wait_for_task_response(client, project_id, retried.operation_id)
    assert task["status"] == "succeeded", task.get("error")
    assert launcher.seen[-1] == b"first draft"
    assert store.artifact(artifact.artifact_id).current_version == newer.version_id
    assert store.read_artifact_bytes(artifact.artifact_id) == b"human draft"
    outcomes = task["result"].get("artifact_edit_outcomes") or []
    assert [item["status"] for item in outcomes] == (["forked"] if edits else [])


def test_a_failed_publish_of_an_attached_edit_settles_as_failed(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    artifact = _saved_report(store, project_id)
    launcher = cast(AgentLauncher, _Editor(b"second draft"))

    def unavailable(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "publish_artifact_version", unavailable)

    async def stream(_project_id, _kind, request, execution):
        service = get_graph_service(app.state.catalog, project_id, None)
        async for frame in stream_discuss_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    response = discuss_turn(
        client,
        f"/api/projects/{project_id}/tasks/project_chat",
        {
            "chat_id": str(uuid4()),
            "message": "Tighten the report",
            "references": [{"kind": "artifact", "artifact_id": artifact.artifact_id}],
        },
    )
    assert response.status_code == 202, response.text
    task = wait_for_task_response(client, project_id, response.json()["operation_id"])
    assert len(store.artifact_versions(artifact.artifact_id)) == 1
    assert [
        (item["status"], item["reason"]) for item in task["result"]["artifact_edit_outcomes"]
    ] == [("failed", "unverified")]
