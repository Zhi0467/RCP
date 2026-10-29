from __future__ import annotations

import asyncio
import threading
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent
from rcp.api.tasks import AgentArtifactResponse
from rcp.storage import ACTIVE_AGENT_TASK_STATUSES
from tests.helpers import sign_in_async_client, signed_in_client

from .helpers import (
    TASK_SETTLE_TIMEOUT,
    append_fixture_patch,
    create_named_app,
    seed_patch,
    wait_until,
)


def _event_frame(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _wait_for_run(
    client: TestClient,
    project_id: str,
    operation_id: str,
) -> dict[str, object]:
    def settled_run() -> dict[str, object] | None:
        response = client.get(f"/api/projects/{project_id}/tasks/{operation_id}")
        assert response.status_code == 200
        record = response.json()
        if record["status"] not in ACTIVE_AGENT_TASK_STATUSES:
            return record
        return None

    return wait_until(settled_run, timeout=2, detail="background run did not finish")


@pytest.mark.parametrize("action", ["content", "download"])
def test_remote_artifact_read_does_not_stall_health(
    manifest, tmp_path, monkeypatch, action
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    descriptor = AgentArtifactResponse(
        artifact_id="a" * 24,
        name="plot.png",
        media_type="image/png",
        view="image",
        available=True,
        unavailable_reason=None,
        can_open=True,
        can_download=True,
        can_keep=True,
        can_discuss=False,
    )
    data = b"\x89PNG\r\n\x1a\npreview"
    entered = threading.Event()
    release = threading.Event()

    def blocked_load(*_args):
        entered.set()
        assert release.wait(timeout=TASK_SETTLE_TIMEOUT)
        return descriptor, data

    monkeypatch.setattr("rcp.api.tasks._load_agent_artifact", blocked_load)
    project_id = app.state.default_project_id
    url = f"/api/projects/{project_id}/tasks/operation/artifacts/{descriptor.artifact_id}/{action}"

    async def drive_concurrently():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            artifact = asyncio.create_task(client.get(url))
            try:
                assert await asyncio.to_thread(entered.wait, TASK_SETTLE_TIMEOUT)
                health = await asyncio.wait_for(
                    client.get("/api/health"), timeout=TASK_SETTLE_TIMEOUT
                )
                # The promise is ordering, not latency: health answered while the
                # artifact read was still parked in the patched loader. An absolute
                # bound instead measures how loaded the runner is, and a real stall
                # still fails here because the loader holds until this block exits.
                assert not artifact.done()
            finally:
                release.set()
            return health, await artifact

    health, artifact = asyncio.run(drive_concurrently())

    assert health.status_code == 200
    assert artifact.status_code == 200


def test_task_list_and_detail_cover_every_agent_kind(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    project_id = app.state.default_project_id

    async def stream(_project_id, kind, _request, _execution):
        yield _event_frame(AgentEvent(event="message", text=f"{kind} answer"))
        if kind != "paper_coach":
            yield _event_frame(AgentEvent(event="message", text='{"applied_revision": 7}'))
        yield _event_frame(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    requests = {
        "seed": {},
        "refresh": {},
        "node_chat": {
            "node_id": "hyp/replanning-restores-plasticity",
            "chat_id": str(uuid.uuid4()),
            "message": "Explain this node.",
        },
        "project_chat": {
            "chat_id": str(uuid.uuid4()),
            "message": "Explain this project.",
        },
        "paper_coach": {"message": "Review this introduction."},
    }
    operation_ids: dict[str, str] = {}
    for kind, body in requests.items():
        response = client.post(
            f"/api/projects/{project_id}/tasks/{kind}",
            json=body,
        )
        assert response.status_code == 202
        operation_id = response.json()["operation_id"]
        operation_ids[kind] = operation_id
        assert _wait_for_run(client, project_id, operation_id)["status"] == "succeeded"

    listed = client.get(f"/api/projects/{project_id}/tasks")
    assert listed.status_code == 200
    assert {task["kind"] for task in listed.json()} == set(requests)
    for kind, operation_id in operation_ids.items():
        detail = client.get(f"/api/projects/{project_id}/tasks/{operation_id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["kind"] == kind
        assert payload["result"] == {"messages": [f"{kind} answer"]}
        assert payload["events"]
        assert payload["debug_receipts"]


@pytest.mark.parametrize("kind", ["project_chat", "paper_coach"])
def test_composer_admission_claims_uploads_and_project_references(manifest, tmp_path, kind):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    project_id = app.state.default_project_id
    scope_id, client_id = str(uuid.uuid4()), str(uuid.uuid4())

    async def stream(_project_id, _kind, _request, _execution):
        yield _event_frame(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    with signed_in_client(app) as client:
        uploaded = client.post(
            f"/api/projects/{project_id}/chats/{scope_id}/attachments",
            data={"client_id": client_id},
            files={"file": ("notes.txt", b"temporary input", "text/plain")},
        )
        assert uploaded.status_code == 200
        upload = uploaded.json()
        started = client.post(
            f"/api/projects/{project_id}/tasks/{kind}",
            json={
                "chat_id" if kind == "project_chat" else "attachment_scope_id": scope_id,
                "message": "Use the uploaded note and hypothesis.",
                "attachment_set_id": upload["attachment_set_id"],
                "attachment_client_id": client_id,
                "references": [{"kind": "node", "node_id": "hyp/replanning-restores-plasticity"}],
                "attachment_batch_id": "client-forged-batch",
                "attachments": [{"invalid": "client-forged-descriptor"}],
            },
        )
        assert started.status_code == 202, started.text
        request = started.json()["request"]
        assert request["attachment_set_id"] is None
        assert request["attachment_client_id"] is None
        assert request["references"] == []
        assert request["attachment_batch_id"] == upload["attachment_set_id"]
        assert len(request["attachments"]) == 2
        assert request["attachments"][0]["attachment_id"] == upload["attachment"]["attachment_id"]
        assert request["attachments"][1]["reference"]["source_id"] == (
            "hyp/replanning-restores-plasticity"
        )
        assert _wait_for_run(client, project_id, started.json()["operation_id"])["status"] == (
            "succeeded"
        )
        removed = client.delete(
            f"/api/projects/{project_id}/chats/{scope_id}/attachments/"
            f"{upload['attachment']['attachment_id']}",
            params={"client_id": client_id, "attachment_set_id": upload["attachment_set_id"]},
        )
        assert removed.status_code == 422


@pytest.mark.parametrize("scope", [None, "invalid", "different"])
def test_coach_attachment_scope_refusal_preserves_upload(manifest, tmp_path, scope):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    scope_id, client_id = str(uuid.uuid4()), str(uuid.uuid4())
    with signed_in_client(app) as client:
        uploaded = client.post(
            f"/api/projects/{project_id}/chats/{scope_id}/attachments",
            data={"client_id": client_id},
            files={"file": ("notes.txt", b"temporary input", "text/plain")},
        )
        assert uploaded.status_code == 200
        upload = uploaded.json()
        response = client.post(
            f"/api/projects/{project_id}/tasks/paper_coach",
            json={
                "message": "Read the note.",
                "attachment_scope_id": str(uuid.uuid4()) if scope == "different" else scope,
                "attachment_set_id": upload["attachment_set_id"],
                "attachment_client_id": client_id,
            },
        )
        assert response.status_code == 422
        removed = client.delete(
            f"/api/projects/{project_id}/chats/{scope_id}/attachments/"
            f"{upload['attachment']['attachment_id']}",
            params={"client_id": client_id, "attachment_set_id": upload["attachment_set_id"]},
        )
        assert removed.status_code == 200
