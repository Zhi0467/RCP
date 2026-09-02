from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.chat import (
    _chat_stage_name,
    _local_chat_artifact_directory,
    _prepare_local_chat_workspace,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
)
from rcp.runs.shared import _sweep_stale_stages
from rcp.service import RunRequest
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    ProjectRecord,
    WatcherContinuation,
    WatcherRecord,
)


def _register_legacy_project(store: AppStore, project_id: str) -> None:
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            locator=f"/tmp/{project_id}/research.yaml",
            name="Legacy project",
            state_location=f"/tmp/{project_id}/.research",
            state_remote=False,
            added_at="2026-08-01T00:00:00+00:00",
        )
    )


def _request(chat_id: str, *, trigger: str = "human", watcher_ids: list[str] | None = None):
    return {
        "provider": "codex",
        "model": "gpt-5",
        "reasoning": "medium",
        "run_on": "laptop",
        "run_truth_scope": ["state"],
        "chat_scope": "project",
        "node_id": None,
        "message": "Continue.",
        "chat_id": chat_id,
        "session_id": None,
        "mode": "work",
        "trigger": trigger,
        "patch_kind": "work",
        "workflow_ids": [],
        "skill_ids": [],
        "invoked_workflow_ids": [],
        "invoked_skill_ids": [],
        "resolved_skill_packages": [],
        "watcher_ids": watcher_ids or [],
    }


def _task(
    store: AppStore,
    operation_id: str,
    project_id: str,
    chat_id: str,
    *,
    status: str = "succeeded",
    stage_host: str | None = None,
    stage_root: str | None = None,
    parent_operation_id: str | None = None,
    native_session_id: str | None = None,
    request_session_id: str | None = None,
    run_on: str = "laptop",
) -> AgentTaskRecord:
    now = store.now()
    request = _request(chat_id)
    request["session_id"] = request_session_id
    request["run_on"] = run_on
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=project_id,
        kind="project_chat",
        status=status,
        request=request,
        created_at=now,
        updated_at=now,
        status_message="Stored chat turn.",
        attempt=2 if parent_operation_id else 1,
        parent_operation_id=parent_operation_id,
        native_session_id=native_session_id,
        stage_host=stage_host,
        stage_root=stage_root,
    )


@pytest.mark.parametrize("remote", [False, True])
def test_adopted_chat_next_turn_reuses_exact_saved_stage(tmp_path: Path, remote: bool) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    legacy_id = "legacy-project"
    canonical_id = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    stage_name = f"chat-pre-adoption-{chat_id}"
    native_session_id = str(uuid.uuid4())
    if remote:
        stage_host = "worker.example"
        stage_root = f"/tmp/rcp-run.{stage_name}"
    else:
        stage_host = None
        local_stage = tmp_path / "data" / "run-stage" / stage_name
        local_stage.mkdir(parents=True)
        stage_root = str(local_stage)

    _register_legacy_project(store, legacy_id)
    store.create_agent_task(
        _task(
            store,
            "turn-before-adoption",
            legacy_id,
            chat_id,
            stage_host=stage_host,
            stage_root=stage_root,
            native_session_id=native_session_id,
        )
    )
    store.migrate_project_identity(legacy_id, canonical_id, store.space_id)

    candidate = _task(
        store,
        "turn-after-adoption",
        canonical_id,
        chat_id,
        status="queued",
        request_session_id=native_session_id,
    )
    next_turn = store.create_agent_task(candidate)

    assert candidate.stage_host is None
    assert candidate.stage_root is None
    assert next_turn.stage_host == stage_host
    assert next_turn.stage_root == stage_root
    execution = AgentTaskExecution(
        operation_id=next_turn.operation_id,
        store=store,
        control=AgentProcessControl(),
        stage_host=next_turn.stage_host,
        stage_root=next_turn.stage_root,
    )
    request = RunRequest(chat_scope="project", chat_id=chat_id, message="Continue.")
    assert _chat_stage_name(None, request, execution) == stage_name  # type: ignore[arg-type]
    if remote:
        assert (
            _validated_remote_chat_resume_stage(execution, "worker.example", stage_name)
            == stage_root
        )
    else:
        assert _validated_local_chat_resume_stage(execution, Path(stage_root)) == Path(stage_root)


def test_adopted_chat_resume_and_retry_keep_pre_adoption_stage(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    legacy_id = "legacy-project"
    canonical_id = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / f"chat-pre-adoption-{chat_id}"
    stage.mkdir(parents=True)
    _register_legacy_project(store, legacy_id)
    store.create_agent_task(
        _task(
            store,
            "paused-before-adoption",
            legacy_id,
            chat_id,
            status="paused",
            stage_root=str(stage),
        )
    )
    store.migrate_project_identity(legacy_id, canonical_id, store.space_id)

    resumed = store.create_agent_task(
        _task(
            store,
            "resume-after-adoption",
            canonical_id,
            chat_id,
            status="failed",
            stage_root=str(stage),
            parent_operation_id="paused-before-adoption",
        )
    )
    retried = store.create_agent_task(
        _task(
            store,
            "retry-after-adoption",
            canonical_id,
            chat_id,
            status="queued",
            stage_root=str(stage),
            parent_operation_id="resume-after-adoption",
        )
    )

    assert resumed.stage_root == str(stage)
    assert retried.stage_root == str(stage)


def test_split_chat_stage_layout_marker_is_idempotent_and_repairs_duplicate_receipts(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    project_id = "legacy-project"
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / "chat-layout"
    workspace = stage / "workspace"
    workspace.mkdir(parents=True)
    _register_legacy_project(store, project_id)
    task = store.create_agent_task(
        _task(store, "layout-turn", project_id, chat_id, stage_root=str(stage))
    )

    for _ in range(2):
        store.record_chat_stage_layout(
            task.operation_id,
            stage_root=str(stage),
            workspace_root=str(workspace),
        )
    assert (
        store.chat_stage_layout(
            project_id=project_id,
            kind="project_chat",
            chat_id=chat_id,
            stage_host="",
            stage_root=str(stage),
        )
        == "split-v1"
    )

    store.record_agent_task_receipt(
        task.operation_id,
        "chat_stage_layout",
        {
            "layout": "split-v1",
            "stage_host": "",
            "stage_root": str(stage),
            "workspace_root": str(workspace),
        },
    )
    with pytest.raises(ValueError, match="multiple layout markers"):
        store.chat_stage_layout(
            project_id=project_id,
            kind="project_chat",
            chat_id=chat_id,
            stage_host="",
            stage_root=str(stage),
        )

    store.record_chat_stage_layout(
        task.operation_id,
        stage_root=str(stage),
        workspace_root=str(workspace),
    )
    assert (
        store.chat_stage_layout(
            project_id=project_id,
            kind="project_chat",
            chat_id=chat_id,
            stage_host="",
            stage_root=str(stage),
        )
        == "split-v1"
    )


def test_second_fresh_sessionless_chat_reuses_one_split_layout_and_artifact_root(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    project_id = "legacy-project"
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / "chat-second-fresh"
    stage.mkdir(parents=True)
    _register_legacy_project(store, project_id)
    first = store.create_agent_task(_task(store, "first", project_id, chat_id))
    first_execution = AgentTaskExecution(
        operation_id=first.operation_id,
        store=store,
        control=AgentProcessControl(),
    )
    workspace = _prepare_local_chat_workspace(
        stage,
        execution=first_execution,
        saved_stage=False,
    )
    first_execution.checkpoint_stage("", str(stage))
    artifact = workspace / "turns" / "first" / "artifacts" / "result.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html>retained</html>", encoding="utf-8")

    second = store.create_agent_task(
        _task(
            store,
            "second",
            project_id,
            chat_id,
            native_session_id="second-session",
        )
    )
    second_execution = AgentTaskExecution(
        operation_id=second.operation_id,
        store=store,
        control=AgentProcessControl(),
    )
    assert (
        _prepare_local_chat_workspace(
            stage,
            execution=second_execution,
            saved_stage=False,
        )
        == workspace
    )
    second_execution.checkpoint_stage("", str(stage))
    resumed = store.create_agent_task(
        _task(
            store,
            "resumed",
            project_id,
            chat_id,
            parent_operation_id=second.operation_id,
            request_session_id="second-session",
        )
    )

    stored_first = store.agent_task(first.operation_id)
    stored_second = store.agent_task(second.operation_id)
    assert stored_first is not None and stored_second is not None
    assert stored_second.stage_root == resumed.stage_root == str(stage)
    assert _local_chat_artifact_directory(store, stored_first, "first") / "result.html" == artifact
    layout_receipts = [
        receipt
        for operation_id in (first.operation_id, second.operation_id, resumed.operation_id)
        for receipt in store.agent_task_receipts(operation_id)
        if receipt.category == "chat_stage_layout"
    ]
    assert len(layout_receipts) == 1


def test_fresh_sessionless_task_preserves_an_exact_legacy_stage_layout(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    project_id = "legacy-project"
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / "legacy-chat"
    stage.mkdir(parents=True)
    _register_legacy_project(store, project_id)
    store.create_agent_task(_task(store, "legacy", project_id, chat_id, stage_root=str(stage)))
    fresh = store.create_agent_task(_task(store, "fresh", project_id, chat_id))
    execution = AgentTaskExecution(
        operation_id=fresh.operation_id,
        store=store,
        control=AgentProcessControl(),
    )

    assert (
        _prepare_local_chat_workspace(
            stage,
            execution=execution,
            saved_stage=False,
        )
        == stage
    )
    execution.checkpoint_stage("", str(stage))
    assert not (stage / "workspace").exists()
    assert all(
        receipt.category != "chat_stage_layout"
        for operation_id in ("legacy", fresh.operation_id)
        for receipt in store.agent_task_receipts(operation_id)
    )


def test_split_layout_marker_survives_crash_before_stage_checkpoint(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    project_id = "legacy-project"
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / "chat-crash-before-checkpoint"
    stage.mkdir(parents=True)
    _register_legacy_project(store, project_id)
    crashed = store.create_agent_task(_task(store, "crashed", project_id, chat_id))
    crashed_execution = AgentTaskExecution(
        operation_id=crashed.operation_id,
        store=store,
        control=AgentProcessControl(),
    )
    workspace = _prepare_local_chat_workspace(
        stage,
        execution=crashed_execution,
        saved_stage=False,
    )

    retry = store.create_agent_task(
        _task(
            store,
            "retry",
            project_id,
            chat_id,
            native_session_id="retry-session",
        )
    )
    retry_execution = AgentTaskExecution(
        operation_id=retry.operation_id,
        store=store,
        control=AgentProcessControl(),
        continuation="retry",
    )
    assert (
        _prepare_local_chat_workspace(
            stage,
            execution=retry_execution,
            saved_stage=False,
        )
        == workspace
    )
    retry_execution.checkpoint_stage("", str(stage))
    artifact = workspace / "turns" / "retry" / "artifacts" / "result.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html>recovered</html>", encoding="utf-8")
    resumed = store.create_agent_task(
        _task(
            store,
            "resume",
            project_id,
            chat_id,
            parent_operation_id=retry.operation_id,
            request_session_id="retry-session",
        )
    )

    stored_retry = store.agent_task(retry.operation_id)
    assert stored_retry is not None
    assert resumed.stage_root == str(stage)
    assert _local_chat_artifact_directory(store, stored_retry, "retry") / "result.html" == artifact
    assert (
        sum(
            receipt.category == "chat_stage_layout"
            for operation_id in (crashed.operation_id, retry.operation_id, resumed.operation_id)
            for receipt in store.agent_task_receipts(operation_id)
        )
        == 1
    )


def test_clean_turn_recreates_swept_split_workspace_but_saved_turn_fails_closed(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    project_id = "legacy-project"
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / "chat-swept-split"
    stage.mkdir(parents=True)
    _register_legacy_project(store, project_id)
    first = store.create_agent_task(_task(store, "first", project_id, chat_id))
    first_execution = AgentTaskExecution(
        operation_id=first.operation_id,
        store=store,
        control=AgentProcessControl(),
    )
    workspace = _prepare_local_chat_workspace(
        stage,
        execution=first_execution,
        saved_stage=False,
    )
    first_execution.checkpoint_stage("", str(stage))

    shutil.rmtree(stage)
    stage.mkdir()
    fresh = store.create_agent_task(_task(store, "fresh", project_id, chat_id))
    fresh_execution = AgentTaskExecution(
        operation_id=fresh.operation_id,
        store=store,
        control=AgentProcessControl(),
    )

    assert (
        _prepare_local_chat_workspace(
            stage,
            execution=fresh_execution,
            saved_stage=False,
        )
        == workspace
    )
    assert workspace.is_dir()
    assert (
        sum(
            receipt.category == "chat_stage_layout"
            for operation_id in (first.operation_id, fresh.operation_id)
            for receipt in store.agent_task_receipts(operation_id)
        )
        == 1
    )

    fresh_execution.checkpoint_stage("", str(stage))
    shutil.rmtree(workspace)
    saved = store.create_agent_task(
        _task(
            store,
            "saved",
            project_id,
            chat_id,
            stage_root=str(stage),
            parent_operation_id=fresh.operation_id,
        )
    )
    saved_execution = AgentTaskExecution(
        operation_id=saved.operation_id,
        store=store,
        control=AgentProcessControl(),
        stage_root=str(stage),
        continuation="resume",
    )
    with pytest.raises(ValueError, match="saved provider workspace is unavailable"):
        _prepare_local_chat_workspace(
            stage,
            execution=saved_execution,
            saved_stage=True,
        )


@pytest.mark.parametrize("layout", ["split", "legacy"])
def test_saved_chat_turn_refreshes_outer_stage_before_retention_sweep(
    tmp_path: Path,
    layout: str,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    project_id = "legacy-project"
    chat_id = str(uuid.uuid4())
    stage_root = tmp_path / "data" / "run-stage"
    stage = stage_root / f"chat-active-{layout}"
    stage.mkdir(parents=True)
    _register_legacy_project(store, project_id)
    task = store.create_agent_task(
        _task(store, f"active-{layout}", project_id, chat_id, stage_root=str(stage))
    )
    execution = AgentTaskExecution(
        operation_id=task.operation_id,
        store=store,
        control=AgentProcessControl(),
        stage_root=str(stage),
        continuation="resume",
    )
    expected_workspace = stage
    if layout == "split":
        expected_workspace = stage / "workspace"
        expected_workspace.mkdir()
        store.record_chat_stage_layout(
            task.operation_id,
            stage_root=str(stage),
            workspace_root=str(expected_workspace),
        )
    stale_mtime = time.time() - 8 * 86400
    os.utime(stage, (stale_mtime, stale_mtime))

    assert (
        _prepare_local_chat_workspace(
            stage,
            execution=execution,
            saved_stage=True,
        )
        == expected_workspace
    )
    refreshed_mtime = stage.stat().st_mtime
    assert refreshed_mtime > stale_mtime

    _sweep_stale_stages(stage_root, now=refreshed_mtime)
    assert stage.is_dir()


def test_split_layout_marker_follows_project_identity_adoption(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    legacy_id = "legacy-project"
    canonical_id = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / "chat-before-adoption"
    stage.mkdir(parents=True)
    _register_legacy_project(store, legacy_id)
    task = store.create_agent_task(_task(store, "before-adoption", legacy_id, chat_id))
    execution = AgentTaskExecution(
        operation_id=task.operation_id,
        store=store,
        control=AgentProcessControl(),
    )
    workspace = _prepare_local_chat_workspace(
        stage,
        execution=execution,
        saved_stage=False,
    )
    execution.checkpoint_stage("", str(stage))

    store.migrate_project_identity(legacy_id, canonical_id, store.space_id)

    adopted = store.agent_task(task.operation_id)
    assert adopted is not None
    assert adopted.project_id == canonical_id
    assert _local_chat_artifact_directory(store, adopted, "turn") == (
        workspace / "turns" / "turn" / "artifacts"
    )


def test_adopted_generic_watcher_wake_inherits_conversation_stage(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    legacy_id = "legacy-project"
    canonical_id = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    stage = tmp_path / "data" / "run-stage" / f"chat-pre-adoption-{chat_id}"
    stage.mkdir(parents=True)
    _register_legacy_project(store, legacy_id)
    store.create_agent_task(
        _task(
            store,
            "work-before-adoption",
            legacy_id,
            chat_id,
            stage_root=str(stage),
        )
    )
    continuation = WatcherContinuation(
        provider="codex",
        model="gpt-5",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["state"],
    )
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id="finished-work",
                project_id=legacy_id,
                origin_operation_id="work-before-adoption",
                origin_task_kind="project_chat",
                chat_id=chat_id,
                check_command="true",
                log_path="/tmp/finished-work.log",
                cwd="/tmp",
                continuation=continuation,
                status="completed",
                created_at="2026-08-01T00:00:00+00:00",
                completed_at="2026-08-01T00:01:00+00:00",
            )
        ]
    )
    store.migrate_project_identity(legacy_id, canonical_id, store.space_id)
    now = store.now()
    wake = AgentTaskRecord(
        operation_id="watcher-wake-after-adoption",
        project_id=canonical_id,
        kind="project_chat",
        status="queued",
        request=_request(chat_id, trigger="watcher", watcher_ids=["finished-work"]),
        created_at=now,
        updated_at=now,
        status_message="Queued watcher wake.",
    )

    queued = store.create_watcher_notification_task(wake, ["finished-work"])

    assert queued is not None
    assert wake.stage_root is None
    assert queued.stage_host is None
    assert queued.stage_root == str(stage)


def test_chat_stage_binding_rejects_a_conflicting_explicit_stage(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    legacy_id = "legacy-project"
    canonical_id = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    first = tmp_path / "data" / "run-stage" / "chat-first"
    second = tmp_path / "data" / "run-stage" / "chat-second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    native_session_id = str(uuid.uuid4())
    _register_legacy_project(store, legacy_id)
    store.create_agent_task(
        _task(
            store,
            "first",
            legacy_id,
            chat_id,
            stage_root=str(first),
            native_session_id=native_session_id,
        )
    )
    store.create_agent_task(
        _task(
            store,
            "conflicting-history",
            legacy_id,
            chat_id,
            stage_root=str(second),
            native_session_id=native_session_id,
        )
    )
    store.migrate_project_identity(legacy_id, canonical_id, store.space_id)

    with pytest.raises(ValueError, match="conflicting saved workspace bindings"):
        store.create_agent_task(
            _task(
                store,
                "second",
                canonical_id,
                chat_id,
                request_session_id=native_session_id,
            )
        )

    assert store.agent_task("second") is None
