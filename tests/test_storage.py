import hashlib
import json
import sqlite3

import pytest

from rcp.artifacts import AgentArtifactDescriptor
from rcp.providers import ProviderUsage
from rcp.storage import AgentTaskRecord, AppStore, ProjectRecord


def _project(project_id: str) -> ProjectRecord:
    return ProjectRecord(
        project_id=project_id,
        locator=f"/tmp/{project_id}/research.yaml",
        name=project_id,
        state_location=f"/tmp/{project_id}/.research",
        state_remote=False,
        added_at="2026-07-31T00:00:00+00:00",
    )


def _task(store: AppStore, project_id: str, operation_id: str, status: str) -> None:
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="refresh",
            status=status,
            request={},
            created_at=now,
            updated_at=now,
            status_message=status,
        )
    )


def test_project_record_deletion_is_atomic_complete_and_project_scoped(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(_project("delete-me"))
    store.upsert_project(_project("keep-me"))
    _task(store, "delete-me", "delete-run", "failed")
    _task(store, "keep-me", "keep-run", "failed")

    for operation_id in ("delete-run", "keep-run"):
        store.record_agent_task_event(operation_id, "event")
        store.record_agent_task_receipt(operation_id, "receipt", {"value": True})
        store.record_agent_task_patch_output(operation_id, "{}")
        store.record_agent_task_contract(operation_id, "base", "contract", "digest")
    with store.connection() as connection:
        for project_id in ("delete-me", "keep-me"):
            connection.execute(
                """
                INSERT INTO paper_drafts(project_id, content, updated_at)
                VALUES (?, 'draft', '2026-07-31T00:00:00+00:00')
                """,
                (project_id,),
            )
            connection.execute(
                """
                INSERT INTO writing_sessions (
                    native_session_id, provider, execution_machine, project_id,
                    model, created_at, last_resumed_at, introduction_hash_examined,
                    graph_revision_examined, research_md_hash_examined
                ) VALUES (?, 'provider', 'local', ?, 'model', ?, ?, 'intro', 1, 'research')
                """,
                (
                    f"{project_id}-session",
                    project_id,
                    "2026-07-31T00:00:00+00:00",
                    "2026-07-31T00:00:00+00:00",
                ),
            )

    counts = store.delete_project_records("delete-me")

    assert counts == {
        "paper_drafts": 1,
        "writing_sessions": 1,
        "watchers": 0,
        "graph_run_outputs": 1,
        "graph_run_events": 1,
        "graph_run_receipts": 1,
        "graph_run_contracts": 1,
        "graph_runs": 1,
        "projects": 1,
    }
    with store.connection() as connection:
        for table in (
            "paper_drafts",
            "writing_sessions",
            "graph_runs",
            "graph_run_outputs",
            "graph_run_contracts",
            "graph_run_events",
            "graph_run_receipts",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    assert store.project("delete-me") is None
    assert store.project("keep-me") is not None

    reopened = AppStore(store.path)
    assert reopened.project("delete-me") is None
    assert reopened.project("keep-me") is not None


def test_agent_usage_is_counted_once_and_snapshot_uses_weighted_cache_share(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(_project("project"))
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="refresh-operation",
            project_id="project",
            kind="refresh",
            status="succeeded",
            request={"provider": "codex", "model": "gpt"},
            created_at=now,
            updated_at=now,
            status_message="done",
        )
    )
    usage = ProviderUsage(
        provider_profile="codex.turn.v1",
        provider_event_type="turn.completed",
        dedupe_key="turn-1",
        processed_input_tokens=1_000,
        generated_tokens=100,
        cached_input_tokens=400,
        provider_fields={"input_tokens": 1_000},
    )

    counted = store.record_agent_usage("refresh-operation", usage)
    duplicate = store.record_agent_usage("refresh-operation", usage)

    assert counted.counted is True
    assert duplicate.counted is False
    assert duplicate.count_reason == "duplicate"
    snapshot = store.agent_usage_snapshot("project")
    assert snapshot.counted_records == 1
    assert snapshot.excluded_records == 1
    assert snapshot.input_processed.total_tokens == 1_000
    assert snapshot.generated.total_tokens == 100
    assert snapshot.input_processed.cache_share == 0.4
    assert snapshot.input_processed.block_tokens == 50
    assert snapshot.input_processed.cells[0].task_kind == "refresh"


@pytest.mark.parametrize("status", ["queued", "running", "pausing"])
def test_project_record_deletion_refuses_active_task(tmp_path, status) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(_project("project"))
    _task(store, "project", "operation", status)

    with pytest.raises(ValueError, match="Pause the active agent task"):
        store.project_deletion_stages("project")
    with pytest.raises(ValueError, match="Pause the active agent task"):
        store.delete_project_records("project")

    assert store.project("project") is not None
    assert store.agent_task("operation") is not None


def test_v02_graph_run_migrates_to_recoverable_interrupted_agent_task(tmp_path) -> None:
    path = tmp_path / "rcp.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE graph_runs (
                operation_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                status_message TEXT NOT NULL,
                error TEXT,
                applied_revision INTEGER
            );
            CREATE UNIQUE INDEX graph_runs_active_project
                ON graph_runs(project_id)
                WHERE status IN ('queued', 'running');
            CREATE TABLE graph_run_receipts (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                tier TEXT NOT NULL,
                category TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO graph_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-operation",
                "project",
                "seed",
                "running",
                json.dumps({"provider": "codex", "run_on": "laptop"}),
                "2026-07-27T10:00:00+00:00",
                "2026-07-27T10:00:01+00:00",
                "2026-07-27T10:00:01+00:00",
                None,
                "Working",
                None,
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO graph_run_receipts (
                operation_id, created_at, tier, category, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "old-operation",
                "2026-07-27T10:00:01+00:00",
                "summary",
                "operation_created",
                '{"kind":"seed"}',
            ),
        )

    store = AppStore(path)
    store.interrupt_active_agent_tasks()
    record = store.agent_task("old-operation")

    assert record is not None
    assert record.status == "interrupted"
    assert record.can_resume is False
    assert record.can_retry is True
    assert record.attempt == 1
    assert "Resume" in record.status_message
    assert record.result is None
    assert store.agent_task_events("old-operation")[0].level == "warning"
    assert [receipt.category for receipt in store.agent_task_receipts("old-operation")] == [
        "operation_created",
        "operation_interrupted",
    ]


def test_patch_recovery_output_is_bounded(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="operation",
            project_id="project",
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )

    store.record_agent_task_patch_output("operation", '{"kind":"refresh"}')

    assert store.agent_task_patch_output("operation") == '{"kind":"refresh"}'
    with pytest.raises(ValueError, match="2 MB"):
        store.record_agent_task_patch_output("operation", "x" * 2_000_001)


def test_agent_task_events_and_tiered_receipts_are_bounded_per_operation(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="operation",
            project_id="project",
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )

    for index in range(225):
        store.record_agent_task_event("operation", f"event {index}")
    for tier, count in (("summary", 70), ("diagnostic", 40), ("trace", 20)):
        for index in range(count):
            store.record_agent_task_receipt(
                "operation",
                f"{tier}-{index}",
                {"index": index},
                tier=tier,
            )

    events = store.agent_task_events("operation", limit=500)
    receipts = store.agent_task_receipts("operation")
    assert len(events) == 200
    assert events[0].message == "event 25"
    assert sum(receipt.tier == "summary" for receipt in receipts) == 64
    assert sum(receipt.tier == "diagnostic" for receipt in receipts) == 32
    assert sum(receipt.tier == "trace" for receipt in receipts) == 16
    assert next(receipt for receipt in receipts if receipt.tier == "summary").category == (
        "summary-6"
    )


def test_oversized_agent_task_receipt_omits_values_but_keeps_safe_metadata(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="operation",
            project_id="project",
            kind="seed",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )
    raw_evidence = "sensitive transcript fragment" * 1000

    store.record_agent_task_receipt(
        "operation",
        "context_assembled",
        {"raw_evidence": raw_evidence, "session_count": 12},
        tier="diagnostic",
    )

    receipt = store.agent_task_receipts("operation")[0]
    assert receipt.payload == {
        "omitted": True,
        "reason": "payload_exceeded_limit",
        "byte_length": len(
            json.dumps(
                {"raw_evidence": raw_evidence, "session_count": 12},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        ),
        "keys": ["raw_evidence", "session_count"],
    }
    assert raw_evidence not in json.dumps(receipt.payload)


def test_agent_task_contract_content_is_durable_beyond_receipt_limit(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="operation",
            project_id="project",
            kind="seed",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )
    content = "immutable pointer contract\n" + "x" * 20_000
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    store.record_agent_task_contract("operation", "base", content, digest)
    store.record_agent_task_contract("operation", "base", content, digest)

    assert store.agent_task_contract("operation", "base") == content
    contracts = store.agent_task_contracts("operation")
    assert len(contracts) == 1
    assert contracts[0].operation_id == "operation"
    assert contracts[0].role == "base"
    assert contracts[0].sha256 == digest
    assert contracts[0].content == content
    with pytest.raises(ValueError, match="immutable"):
        store.record_agent_task_contract("operation", "base", content + "changed", digest)


def test_agent_task_result_messages_are_bounded(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="operation",
            project_id="project",
            kind="paper_coach",
            status="running",
            request={"message": "Review this."},
            created_at=now,
            updated_at=now,
            status_message="running",
        )
    )

    store.complete_agent_task(
        "operation",
        applied_revision=None,
        result={"messages": ["x" * 20_000 for _ in range(40)]},
    )

    record = store.agent_task("operation")
    assert record is not None
    assert record.status == "succeeded"
    assert record.applied_revision is None
    assert record.result is not None
    messages = record.result["messages"]
    assert isinstance(messages, list)
    assert len(json.dumps(record.result).encode("utf-8")) < 64 * 1024
    assert all(isinstance(message, str) and len(message) <= 16_000 for message in messages)


def test_resumable_paused_chat_query_is_exact_and_child_attempt_resolves_it(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    chat_id = "1f16a63a-06c8-42b6-a856-c9329e2e9007"
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="paused-chat",
            project_id="project",
            kind="node_chat",
            status="paused",
            request={"chat_id": chat_id, "node_id": "rq/one"},
            created_at=now,
            updated_at=now,
            status_message="paused",
            native_session_id="native-session",
            stage_host="",
            stage_root="/tmp/paused-chat",
        )
    )

    assert store.has_resumable_paused_chat_task("project", "node_chat", chat_id)
    assert not store.has_resumable_paused_chat_task("other", "node_chat", chat_id)
    assert not store.has_resumable_paused_chat_task("project", "project_chat", chat_id)
    assert not store.has_resumable_paused_chat_task(
        "project", "node_chat", "701929b2-50f5-41e6-9614-41e4a82b9e34"
    )

    store.create_agent_task(
        AgentTaskRecord(
            operation_id="retried-chat",
            project_id="project",
            kind="node_chat",
            status="succeeded",
            request={"chat_id": chat_id, "node_id": "rq/one"},
            created_at=now,
            updated_at=now,
            status_message="complete",
            parent_operation_id="paused-chat",
        )
    )

    assert not store.has_resumable_paused_chat_task("project", "node_chat", chat_id)


def test_agent_task_result_retains_only_valid_bounded_artifact_descriptors(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="artifact-operation",
            project_id="project",
            kind="project_chat",
            status="running",
            request={},
            created_at=now,
            updated_at=now,
            status_message="running",
        )
    )
    descriptor = AgentArtifactDescriptor(
        artifact_id="a" * 24,
        name="preview.html",
        media_type="text/html",
    )

    store.complete_agent_task(
        "artifact-operation",
        applied_revision=None,
        result={
            "messages": ["answer"],
            "artifacts": [
                descriptor.model_dump(mode="json"),
                {"artifact_id": "bad", "name": "raw", "media_type": "application/octet-stream"},
            ],
        },
    )

    record = store.agent_task("artifact-operation")
    assert record is not None and record.result is not None
    assert record.result == {
        "messages": ["answer"],
        "artifacts": [descriptor.model_dump(mode="json")],
    }
