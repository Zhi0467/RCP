from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.runs.chat import _append_chat_exchange
from rcp.service import RunRequest
from rcp.transport import StateUnavailable

from .helpers import create_named_app


def test_batch_chat_reader_preserves_single_chat_validation_and_ambiguity(
    manifest, tmp_path, monkeypatch
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    good, ambiguous, missing = (str(uuid.uuid4()) for _ in range(3))
    for chat_id, scope, node_id in [
        (good, "project", None),
        (ambiguous, "project", None),
        (ambiguous, "node", "rq/ambiguous"),
    ]:
        _append_chat_exchange(
            service,
            RunRequest(
                chat_id=chat_id, chat_scope=scope, node_id=node_id, message="Read this chat."
            ),
            "A retained answer.",
            None,
            None,
        )
    scans = []
    original = service._canonical_chat_files

    def counted():
        scans.append(True)
        return original()

    monkeypatch.setattr(service, "_canonical_chat_files", counted)
    transcripts = service.chat_transcripts([good, ambiguous, missing, good])
    assert list(transcripts) == [good]
    assert transcripts[good].messages[-1].text == "A retained answer."
    assert len(scans) == 1
    assert service.chat_transcript(ambiguous) is None
    assert service.chat_transcripts([]) == {}
    for invalid in ["not-a-uuid", good.upper()]:
        with pytest.raises(ValueError, match="chat_id must be"):
            service.chat_transcripts([good, invalid])


def test_chat_history_is_paginated_from_full_canonical_transcripts(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    chat_dir = manifest.research_dir / "chat"
    chat_dir.mkdir(exist_ok=True)
    chat_ids: list[str] = []
    long_answer = "full-answer-" + ("x" * 20_000)

    for index in range(23):
        chat_id = str(uuid.uuid4())
        chat_ids.append(chat_id)
        timestamp = f"2026-07-31T{index:02d}:00:00+00:00"
        answer = long_answer if index == 0 else f"answer {index}"
        common = {
            "sessionId": chat_id,
            "nativeSessionId": str(uuid.uuid4()),
            "nodeId": None,
            "chatScope": "project",
            "provider": "codex",
            "model": "provider-default",
            "reasoning": "medium",
            "executionMachine": "laptop",
            "timestamp": timestamp,
        }
        records = [
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "user",
                "role": "user",
                "text": f"question {index}",
            },
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "role": "assistant",
                "text": answer,
                "appliedRevision": None,
            },
        ]
        (chat_dir / f"project-{chat_id}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    # Neither malformed files nor a valid transcript reached through a symlink
    # can enter the canonical list.
    (chat_dir / "malformed.jsonl").write_text("not json\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        (chat_dir / f"project-{chat_ids[0]}.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (chat_dir / "outside-link.jsonl").symlink_to(outside)

    service = app.state.service
    original_read = service._read_chat_transcript
    parsed_paths: list[Path] = []

    def counted_read(path: Path):
        parsed_paths.append(path)
        return original_read(path)

    monkeypatch.setattr(service, "_read_chat_transcript", counted_read)

    first = client.get(f"/api/projects/{project_id}/chats?offset=0&limit=7")
    assert first.status_code == 200
    assert first.json()["total"] == 23
    assert first.json()["offset"] == 0
    assert first.json()["limit"] == 7
    assert len(first.json()["items"]) == 7
    assert first.json()["items"][0]["title"] == "question 22"
    parsed_after_first_page = len(parsed_paths)
    assert parsed_after_first_page > len(first.json()["items"])

    discovered: list[str] = []
    for offset in range(0, 28, 7):
        page = client.get(f"/api/projects/{project_id}/chats?offset={offset}&limit=7").json()
        discovered.extend(item["chat_id"] for item in page["items"])
    assert set(discovered) == set(chat_ids)
    assert len(discovered) == 23
    assert len(parsed_paths) == parsed_after_first_page

    parsed_paths.clear()
    transcript = client.get(f"/api/projects/{project_id}/chats/{chat_ids[0]}")
    assert transcript.status_code == 200
    assert [message["role"] for message in transcript.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert transcript.json()["messages"][-1]["text"] == long_answer
    assert len(transcript.json()["messages"][-1]["text"]) > 16_000
    assert parsed_paths == [chat_dir / f"project-{chat_ids[0]}.jsonl"]

    assert client.get(f"/api/projects/{project_id}/chats/not-a-uuid").status_code == 422
    assert client.get(f"/api/projects/{project_id}/chats/{uuid.uuid4()}").status_code == 404


def test_chat_history_reports_remote_refresh_failure_as_unavailable(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    project_id = app.state.default_project_id
    service.history.workspace.remote = True
    refresh_threads: list[int] = []

    def fail_refresh() -> bool:
        refresh_threads.append(threading.get_ident())
        raise StateUnavailable("remote transcript refresh failed")

    monkeypatch.setattr(service.history.workspace, "refresh", fail_refresh)
    request_thread = threading.get_ident()

    response = TestClient(app).get(f"/api/projects/{project_id}/chats")

    assert response.status_code == 503
    assert response.json()["detail"] == "remote transcript refresh failed"
    assert refresh_threads and refresh_threads[0] != request_thread


def test_non_main_project_route_does_not_build_project_snapshot(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    monkeypatch.setattr(
        app.state.service,
        "project_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("chat history must not serialize the whole project")
        ),
    )

    response = TestClient(app).get(f"/api/projects/{project_id}/chats")

    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.parametrize("graph_receipt", [False, True])
@pytest.mark.parametrize("original_published", [False, True])
def test_collect_chat_publication_is_idempotent_after_durable_append_crash(
    manifest, tmp_path, monkeypatch, graph_receipt, original_published
):
    from rcp.agents.launcher import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.chat import _append_chat_graph_receipt
    from rcp.service import GraphUpdateResult

    from .test_api import _chat_task_execution

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    store = app.state.background_tasks.store
    request = RunRequest(
        chat_id=str(uuid.uuid4()), chat_scope="project", mode="work", message="Do the work."
    )
    original = _chat_task_execution(
        store, operation_id="original", project_id=app.state.default_project_id, request=request
    )

    def publish(execution):
        if graph_receipt:
            _append_chat_graph_receipt(
                service,
                request.model_copy(update={"message": None}),
                "native-session",
                GraphUpdateResult(status="applied", applied_revision=1),
                execution,
            )
        else:
            _append_chat_exchange(
                service, request, "Finished the work.", "native-session", None, execution=execution
            )

    def crash_publish(_paths):
        raise OSError("crash after durable transcript append")

    if original_published:
        with monkeypatch.context() as patch:
            patch.setattr(service.history.workspace, "publish", crash_publish)
            with pytest.raises(OSError, match="durable transcript"):
                publish(original)
    store.fail_agent_task(original.operation_id, "Connection lost.")
    parent = store.agent_task(original.operation_id)
    for index in range(2):
        child = parent.model_copy(
            update={
                "operation_id": f"collect-{index}",
                "parent_operation_id": parent.operation_id,
                "status": "queued",
                "result": None,
                "attempt": parent.attempt + 1,
            }
        )
        store.create_agent_task(child, continuation_cause="collect")
        store.mark_agent_task_running(child.operation_id)
        execution = AgentTaskExecution(
            operation_id=child.operation_id,
            store=store,
            control=AgentProcessControl(),
            continuation="collect",
        )
        if index == 0:
            with monkeypatch.context() as patch:
                patch.setattr(service.history.workspace, "publish", crash_publish)
                with pytest.raises(OSError, match="durable transcript"):
                    publish(execution)
            store.fail_agent_task(child.operation_id, "Delivery interrupted after append.")
        else:
            publish(execution)
        parent = store.agent_task(child.operation_id)

    records = [
        json.loads(line)
        for line in service.chat_path(request.chat_id, chat_scope="project", node_id=None)
        .read_text()
        .splitlines()
    ]
    assert [item["role"] for item in records] == (
        ["assistant"] if graph_receipt else ["user", "assistant"]
    )
    assert {item["operationId"] for item in records} == {original.operation_id}
    assert records[-1]["text"] == ("" if graph_receipt else "Finished the work.")
