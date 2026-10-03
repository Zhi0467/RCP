from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS
from rcp.runs.chat import (
    _discover_chat_artifacts,
    _record_artifact_discovery_receipt,
    artifact_omissions,
)
from rcp.storage import AgentTaskRecord

from .helpers import create_named_app


@pytest.fixture
def app(manifest, tmp_path):
    return create_named_app(str(manifest.path), data_dir=tmp_path / "data")


def _execution(app) -> AgentTaskExecution:
    store = app.state.background_tasks.store
    now = store.now()
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id="artifact-discovery-test",
            project_id=app.state.default_project_id,
            kind="project_chat",
            status="succeeded",
            request={},
            result={"messages": ["answer"]},
            created_at=now,
            updated_at=now,
            status_message="",
        )
    )
    return AgentTaskExecution(
        operation_id=task.operation_id, store=store, control=AgentProcessControl()
    )


def test_discovery_mixed_types_spends_reads_after_size_checks(app, tmp_path, monkeypatch):
    execution = _execution(app)
    directory = tmp_path / "outputs"
    directory.mkdir()
    files = {
        ".env": b"x",
        "a.bin": b"oversized",
        "b.md": b"# a",
        "c.csv": b"a,b",
        "d.pdf": b"%PDF-",
    }
    for name, data in files.items():
        (directory / name).write_bytes(data)
    (directory / "empty").touch()
    (directory / "nested").mkdir()
    (directory / "link").symlink_to(directory / ".env")
    monkeypatch.setattr("rcp.runs.chat.CHAT_ARTIFACT_MAX_COUNT", 3)
    monkeypatch.setattr("rcp.runs.chat.CHAT_ARTIFACT_MAX_FILE_BYTES", 5)
    monkeypatch.setattr("rcp.runs.chat.CHAT_ARTIFACT_MAX_TOTAL_BYTES", 20)
    artifacts = _discover_chat_artifacts(execution, execution.operation_id, directory, None)
    assert [(item.name, item.media_type) for item in artifacts] == [
        (".env", "application/octet-stream"),
        ("b.md", "text/markdown"),
        ("c.csv", "text/csv"),
    ]
    for item in artifacts:
        (directory / item.name).unlink()
        stored = execution.store.artifact(item.artifact_id)
        assert stored.supplier == "turn"
        assert stored.expires_at is not None
        assert execution.store.read_artifact_bytes(item.artifact_id) == files[item.name]
    receipt = execution.store.agent_task_artifact_discoveries([execution.operation_id])[
        execution.operation_id
    ]
    assert artifact_omissions(receipt) == {
        "file_size_limit": 1,
        "count_limit": 1,
        "empty": 1,
        "discovery_failed": False,
    }


def test_discovery_latest_receipt_wins_and_is_retained(app):
    execution = _execution(app)
    for ignored in ({"empty": 1}, {"count_limit": 2}, {"empty": 1}, {"empty": 1}):
        _record_artifact_discovery_receipt(execution, attached=0, candidates=2, ignored=ignored)
    receipts = [
        r
        for r in execution.store.agent_task_receipts(execution.operation_id)
        if r.category == "artifact_discovery"
    ]
    assert len(receipts) == 3
    assert all(r.tier == "summary" for r in receipts)
    # A long turn's later summary receipts cannot prune the omission notice.
    for index in range(AGENT_TASK_RECEIPT_RETENTION_COUNTS["summary"] + 1):
        execution.store.record_agent_task_receipt(
            execution.operation_id, "later_summary", {"index": index}, tier="summary"
        )
    execution.store.prune_operational_storage(now=datetime.now(UTC) + timedelta(days=366))
    latest = execution.store.agent_task_artifact_discoveries([execution.operation_id, "missing"])
    assert set(latest) == {execution.operation_id}
    assert artifact_omissions(latest[execution.operation_id]) == {
        "empty": 1,
        "discovery_failed": False,
    }


def test_discovery_failure_keeps_answer_and_projects_no_private_detail(app, tmp_path):
    execution = _execution(app)
    assert (
        _discover_chat_artifacts(execution, execution.operation_id, tmp_path / "missing", None)
        == []
    )
    receipt = execution.store.agent_task_artifact_discoveries([execution.operation_id])[
        execution.operation_id
    ]
    assert artifact_omissions(receipt) == {"discovery_failed": True}
    assert execution.store.agent_task(execution.operation_id).result["messages"] == ["answer"]
    _record_artifact_discovery_receipt(
        execution, attached=0, candidates=0, ignored={"unexpected_error": 1}
    )
    receipt = execution.store.agent_task_artifact_discoveries([execution.operation_id])[
        execution.operation_id
    ]
    assert artifact_omissions(receipt) == {"discovery_failed": True}


def test_omissions_filters_unknown_negative_and_noninteger_counts(app):
    execution = _execution(app)
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "artifact_discovery",
        {
            "ignored": {
                "empty": 0,
                "count_limit": -1,
                "file_size_limit": True,
                "total_size_limit": "2",
                "unsupported_type": 9,
                "private/path": 1,
            },
            "detail": "private/path",
        },
    )
    receipt = execution.store.agent_task_artifact_discoveries([execution.operation_id])[
        execution.operation_id
    ]
    assert artifact_omissions(receipt) == {"empty": 0, "discovery_failed": False}


def test_omission_only_turn_is_projected_without_cards(app, tmp_path):
    execution = _execution(app)
    directory = tmp_path / "outputs"
    directory.mkdir()
    (directory / "empty").touch()
    assert _discover_chat_artifacts(execution, execution.operation_id, directory, None) == []
    response = TestClient(app).get(
        f"/api/projects/{app.state.default_project_id}/tasks/{execution.operation_id}"
    )
    assert response.status_code == 200
    assert response.json()["result"]["artifact_omissions"] == {
        "empty": 1,
        "discovery_failed": False,
    }
    assert not response.json()["result"].get("artifacts")


def test_remote_discovery_copies_bytes_without_retaining_stage_access(app, tmp_path):
    execution = _execution(app)

    class RemoteArtifacts:
        def list_artifact_files(self, scope_id):
            assert scope_id == execution.operation_id
            return [("remote.txt", 6)]

        def read_artifact_bytes(self, scope_id, name, *, max_bytes):
            assert (scope_id, name) == (execution.operation_id, "remote.txt")
            return b"remote"

        def write_workspace_text(self, name: str, content: str) -> None:
            self.last_workspace_write = (name, content)

    artifacts = _discover_chat_artifacts(
        execution, execution.operation_id, tmp_path / "absent", RemoteArtifacts()
    )
    assert len(artifacts) == 1
    assert execution.store.read_artifact_bytes(artifacts[0].artifact_id) == b"remote"
