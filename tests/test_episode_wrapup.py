from __future__ import annotations

import json
from pathlib import Path

from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.runs.episode_wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup
from rcp.storage import AgentTaskRecord, AppStore, EpisodeRecord, ProjectRecord

from .helpers import fabricated_authorizer


def _store_with_episode(tmp_path: Path) -> tuple[AppStore, Path]:
    store = AppStore(tmp_path / "app.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(tmp_path),
            name="Project",
            state_location=str(tmp_path),
            state_remote=False,
            added_at=store.now(),
        )
    )
    now = store.now()
    store.create_episode(
        EpisodeRecord(
            episode_id="episode",
            project_id="project",
            mode="experiment_loop",
            control_node_id="experiment-node",
            status="queued",
            invocation_ceiling=2,
            authorized_by=fabricated_authorizer("Episode owner"),
            created_at=now,
            updated_at=now,
        )
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    operational = AgentTaskRecord(
        operation_id="operation",
        project_id="project",
        episode_id="episode",
        kind="node_chat",
        status="succeeded",
        request={
            "provider": "codex",
            "model": "",
            "reasoning": "medium",
            "run_on": "laptop",
        },
        created_at=now,
        updated_at=now,
        status_message="Complete",
        native_session_id="native-session",
        stage_root=str(stage),
        dispatch_authority=AgentDispatchAuthority(
            profile="ordinary",
            task_contract="scratch_patch",
            scope=AgentDispatchScope(run_truth_scope=[], patch_kind="refresh"),
        ),
    )
    store.allocate_episode_invocation("episode", operational)
    return store, stage


def test_shared_wrapup_admits_one_deterministic_hidden_report(tmp_path: Path) -> None:
    store, stage = _store_with_episode(tmp_path)
    spec = EpisodeWrapupSpec(
        episode_id="episode",
        ending="exhausted",
        partial=True,
        continuation_operation_id="operation",
        receipt={"observations": ["bounded evidence"]},
        diagnostic="The operational invocation ceiling was reached.",
    )

    first = begin_episode_report_wrapup(store, spec)
    second = begin_episode_report_wrapup(store, spec)

    assert first.launchable is True
    assert second.task == first.task
    assert second.request == first.request
    assert first.task is not None and first.request is not None
    assert first.task.visible is False
    assert first.task.kind == "episode_report"
    assert first.task.parent_operation_id == "operation"
    assert first.task.native_session_id == "native-session"
    assert first.task.stage_root == str(stage)
    assert first.task.status_message == "Wrapping up visualization and report"
    assert first.request.session_id == "native-session"
    assert first.wrapup.output_path == str(stage / "episode-report.html")
    receipt = json.loads(first.wrapup.receipt_json)
    assert receipt == {
        "ending": "exhausted",
        "episode_id": "episode",
        "mode": "experiment_loop",
        "observations": ["bounded evidence"],
        "partial": True,
        "diagnostic": "The operational invocation ceiling was reached.",
    }
    assert first.episode.invocations_used == 1
    assert store.agent_tasks("project") == [store.agent_task("operation")]


def test_missing_exact_binding_ends_with_nonblocking_report_error(tmp_path: Path) -> None:
    store, _stage = _store_with_episode(tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET native_session_id = NULL, stage_root = NULL "
            "WHERE operation_id = 'operation'"
        )

    admission = begin_episode_report_wrapup(
        store,
        EpisodeWrapupSpec(
            episode_id="episode",
            ending="failed",
            partial=True,
            continuation_operation_id="operation",
            receipt={"failure": "provider session unavailable"},
            diagnostic="The operational turn failed.",
        ),
    )

    assert admission.launchable is False
    assert admission.task is None
    assert admission.episode.status == "failed"
    assert admission.episode.wrapup_state == "failed"
    assert admission.episode.report_attempts_used == 0
    assert admission.episode.ending_diagnostic == "The operational turn failed."
    assert admission.episode.wrapup_error is not None
    assert "no exact saved native session and stage" in admission.episode.wrapup_error
    assert store.episode_report("episode") is None


def test_stop_never_enters_report_wrapup(tmp_path: Path) -> None:
    store, _stage = _store_with_episode(tmp_path)

    try:
        begin_episode_report_wrapup(
            store,
            EpisodeWrapupSpec(
                episode_id="episode",
                ending="stopped",
                partial=True,
                continuation_operation_id="operation",
                receipt={},
            ),
        )
    except ValueError as exc:
        assert "Stop skips report generation" in str(exc)
    else:
        raise AssertionError("Stop must not allocate report work")
