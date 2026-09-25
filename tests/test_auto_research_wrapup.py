from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest
from pydantic import ValidationError

import rcp.runs.auto_research as auto_research
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import AGENT_TASK_RECEIPT_MAX_BYTES
from rcp.runs.auto_research import (
    AutoResearchRunRequest,
    AutoResearchStartRequest,
    auto_research_completion_signal,
    auto_research_exhaustion_signal,
    auto_research_wrapup_spec,
    request_auto_research_stop,
    settle_auto_research_stop,
)
from rcp.runs.episodes.wrapup import (
    begin_episode_report_wrapup,
    episode_wrapup_receipt,
)
from rcp.storage import (
    AgentTaskEventRecord,
    AgentTaskRecord,
    AppStore,
    AutoResearchLifecycleNoticeRecord,
    AutoResearchStateRecord,
    EpisodeRecord,
    GraphWatcherRecord,
    ProjectRecord,
    WatcherContinuation,
)
from rcp.storage.episodes import compact_episode_receipt


def _authority(episode_id: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="orchestrator",
        task_contract="orchestrate",
        scope=AgentDispatchScope(
            run_truth_scope=["repo"],
            episode_id=episode_id,
            patch_kind="work",
        ),
    )


def _episode(tmp_path) -> tuple[AppStore, EpisodeRecord, AgentTaskRecord]:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="/tmp/project/research.yaml",
            name="project",
            state_location="/tmp/project/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    now = store.now()
    authorized_by = AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name="Researcher",
    )
    graph_target = GraphTargetRef(kind="branch", branch_id="episode")
    episode = EpisodeRecord(
        episode_id="episode",
        project_id="project",
        mode="auto_research",
        graph_target=graph_target,
        graph_base_head=GraphHeadRef(revision=0),
        status="queued",
        invocation_ceiling=3,
        authorized_by=authorized_by,
        created_at=now,
        updated_at=now,
    )
    request = AutoResearchRunRequest(
        episode_id=episode.episode_id,
        role="orchestrator",
        actor_operation_id="root",
        provider="codex",
        model="gpt-5",
        reasoning="medium",
        run_on="local",
        run_truth_scope=["repo"],
    )
    root = AgentTaskRecord(
        operation_id="root",
        project_id=episode.project_id,
        episode_id=episode.episode_id,
        graph_target=graph_target,
        kind="auto_research",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="queued",
        authorized_by=authorized_by,
        dispatch_authority=_authority(episode.episode_id),
    )
    return (
        store,
        *store.create_auto_research_episode_with_root_task(
            episode,
            AutoResearchStateRecord(
                episode_id=episode.episode_id,
                starting_instruction="Decide what the evidence supports.",
                created_at=now,
                updated_at=now,
            ),
            root,
        ),
    )


def test_auto_research_request_contract_has_only_operational_roles_and_budget() -> None:
    assert AutoResearchStartRequest(invocation_ceiling=1).invocation_ceiling == 1
    with pytest.raises(ValidationError):
        AutoResearchStartRequest(invocation_ceiling=0)
    with pytest.raises(ValidationError, match="orchestrator|worker"):
        AutoResearchRunRequest.model_validate({"episode_id": "episode", "role": "report"})
    with pytest.raises(ValidationError):
        AutoResearchRunRequest.model_validate(
            {"episode_id": "episode", "role": "orchestrator", "ending": "completed"}
        )


def test_wrapup_selects_the_root_actors_exact_recovery_child_and_compact_receipt(tmp_path) -> None:
    store, episode, root = _episode(tmp_path)
    stage = str(tmp_path / "orchestrator-stage")
    store.checkpoint_agent_task(
        root.operation_id,
        native_session_id="session-1",
        stage_root=stage,
    )
    store.fail_agent_task(root.operation_id, "temporary provider failure")
    root = store.agent_task(root.operation_id)
    assert root is not None
    request = AutoResearchRunRequest.model_validate(root.request).model_copy(
        update={"session_id": "session-1"}
    )
    now = store.now()
    recovery = store.create_auto_research_recovery_task(
        AgentTaskRecord(
            operation_id="root-retry",
            project_id=episode.project_id,
            episode_id=episode.episode_id,
            graph_target=episode.graph_target,
            kind="auto_research",
            status="succeeded",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="recovered",
            attempt=2,
            parent_operation_id=root.operation_id,
            native_session_id="session-1",
            stage_root=stage,
            authorized_by=episode.authorized_by,
            dispatch_authority=root.dispatch_authority,
        )
    )
    watcher = store.create_watchers(
        [
            GraphWatcherRecord(
                watcher_id="episode-watcher",
                project_id=episode.project_id,
                origin_operation_id=recovery.operation_id,
                origin_task_kind="auto_research",
                graph_target=episode.graph_target,
                chat_id=episode.episode_id,
                episode_id=episode.episode_id,
                continuation=WatcherContinuation(
                    provider="codex",
                    run_on="local",
                    patch_kind="work",
                ),
                condition={"node_id": "claim", "status_in": ["active"]},
                armed_revision=1,
                status="active",
                created_at=store.now(),
            )
        ]
    )[0]

    signal = auto_research_completion_signal(store, episode.episode_id)
    spec = auto_research_wrapup_spec(store, signal)

    assert spec.continuation_operation_id == recovery.operation_id
    assert spec.ending == "completed"
    assert spec.partial is False
    assert spec.receipt["starting_instruction"] == "Decide what the evidence supports."
    assert spec.receipt["operational_meter"] == {
        "ceiling": 3,
        "used": 1,
        "remaining": 2,
        "observed_input_tokens": 0,
        "observed_generated_tokens": 0,
    }
    assert spec.receipt["experiment_allowance"] == {
        "total": 15,
        "used": 0,
        "remaining": 15,
    }
    assert spec.receipt["child_work"] == []
    assert spec.receipt["child_experiments"] == []
    assert spec.receipt["lifecycle"] == {
        "counts": {"pending": 0, "delivered": 0, "acknowledged": 0},
        "facts": [],
        "omitted_fact_count": 0,
    }
    assert (
        not {
            "events",
            "history",
            "messages",
            "research",
            "tasks",
            "transcript",
        }
        & spec.receipt.keys()
    )
    assert len(json.dumps(spec.receipt).encode("utf-8")) < 32_000
    assert store.watcher(watcher.watcher_id).status == "stopped"  # type: ignore[union-attr]


def test_stop_settles_without_a_wrapup_spec(tmp_path) -> None:
    store, episode, root = _episode(tmp_path)
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})

    stopping = request_auto_research_stop(store, episode.episode_id)
    stopped = settle_auto_research_stop(store, episode.episode_id)

    assert stopping.status == "stopping"
    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.ending == "stopped"
    assert stopped.wrapup_state == "skipped"


def _oversized_receipt_rows(store, episode, root, monkeypatch, payload, *, absurd=False):
    projection = auto_research.project_auto_research_episode(store, episode.episode_id)
    work = tuple(
        {
            "worker_id": f"worker-{index}",
            "control_node_id": "x" * 240,
            "current_operation_id": "x" * 160,
            "status": "succeeded",
            "attempt": 1,
            "stop_requested": False,
        }
        for index in range(16)
    )
    experiments = tuple(
        {
            "episode_id": f"child-{index}",
            "control_node_id": "x" * 240,
            "route_state": "terminal",
            "status": "failed",
            "ending": "failed",
            "replaces_episode_id": None,
            "diagnostic": payload,
        }
        for index in range(16)
    )
    projection = replace(
        projection,
        work=work,
        work_count=len(work),
        experiments=experiments,
        experiment_count=len(experiments),
        pending_admission_ids=tuple("界" * 160 + str(index) for index in range(16))
        if absurd
        else (),
        pending_admission_count=16 if absurd else 0,
        lifecycle_counts={"pending": 2, "delivered": 0, "acknowledged": 0},
    )
    monkeypatch.setattr(auto_research, "project_auto_research_episode", lambda *_: projection)
    notices = [
        AutoResearchLifecycleNoticeRecord(
            notice_id=f"notice-{index}",
            episode_id=episode.episode_id,
            source_kind="task",
            source_id=f"source-{index}",
            source_event="failed",
            payload={"error": payload},
            created_at=root.created_at,
        )
        for index in range(2)
    ]
    monkeypatch.setattr(store, "auto_research_lifecycle_notices", lambda *_: notices)
    events = [
        AgentTaskEventRecord(
            event_id=index,
            operation_id=root.operation_id,
            created_at=root.created_at,
            level="info",
            message="command completed",
            event_kind="command",
            command_verb="status",
            command_phase="exit",
        )
        for index in range(16)
    ]
    monkeypatch.setattr(store, "auto_research_event_history", lambda *_, **__: (16, events))
    return projection


def _stored_receipt(spec):
    return compact_episode_receipt(
        episode_wrapup_receipt(
            receipt=spec.receipt,
            episode_id=spec.episode_id,
            mode="auto_research",
            ending=spec.ending,
            partial=spec.partial,
            diagnostic=spec.diagnostic,
        )
    )


@pytest.mark.parametrize("payload", ["x" * 800, "错误🔬" * 400], ids=["incident", "unicode"])
def test_oversized_receipt_compacts_to_the_actual_stored_envelope(tmp_path, monkeypatch, payload):
    store, episode, root = _episode(tmp_path)
    store.checkpoint_agent_task(
        root.operation_id, native_session_id="session", stage_root=str(tmp_path / "stage")
    )
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    signal = auto_research_exhaustion_signal(store, episode.episode_id, diagnostic="Turns spent.")
    projection = _oversized_receipt_rows(store, episode, root, monkeypatch, payload)

    spec = auto_research_wrapup_spec(store, signal)
    stored_json, stored_digest = _stored_receipt(spec)
    admission = begin_episode_report_wrapup(store, spec)

    assert len(stored_json.encode("utf-8")) <= AGENT_TASK_RECEIPT_MAX_BYTES
    assert admission.wrapup is not None
    assert admission.wrapup.receipt_json == stored_json
    assert admission.wrapup.receipt_sha256 == stored_digest
    assert spec.receipt["omitted_child_work_count"] + len(spec.receipt["child_work"]) == 16
    assert (
        spec.receipt["omitted_child_experiment_count"] + len(spec.receipt["child_experiments"])
        == projection.experiment_count
    )
    lifecycle = spec.receipt["lifecycle"]
    assert lifecycle["omitted_fact_count"] + len(lifecycle["facts"]) == 2


def test_receipt_orders_equal_timestamp_storage_rows_before_compacting(tmp_path, monkeypatch):
    store, episode, root = _episode(tmp_path)
    signal = auto_research_exhaustion_signal(store, episode.episode_id)
    notices = [
        AutoResearchLifecycleNoticeRecord(
            notice_id=notice_id,
            episode_id=episode.episode_id,
            source_kind="task",
            source_id=notice_id,
            source_event="failed",
            payload={"error": notice_id},
            created_at=root.created_at,
        )
        for notice_id in ("notice-b", "notice-a")
    ]
    events = [
        AgentTaskEventRecord(
            event_id=index,
            operation_id=f"command-{index}",
            created_at=root.created_at,
            level="info",
            message="command",
            event_kind="command",
            command_verb="status",
        )
        for index in (2, 1)
    ]
    monkeypatch.setattr(store, "auto_research_lifecycle_notices", lambda *_: notices)
    monkeypatch.setattr(store, "auto_research_event_history", lambda *_, **__: (2, events))
    first = auto_research_wrapup_spec(store, signal)
    notices.reverse()
    events.reverse()
    second = auto_research_wrapup_spec(store, signal)

    assert _stored_receipt(first) == _stored_receipt(second)
    assert [fact["notice_id"] for fact in first.receipt["lifecycle"]["facts"]] == [
        "notice-a",
        "notice-b",
    ]
    assert [fact["operation_id"] for fact in first.receipt["command_facts"]] == [
        "command-1",
        "command-2",
    ]


def test_last_resort_receipt_preserves_shape_and_exact_counts(tmp_path, monkeypatch):
    store, episode, root = _episode(tmp_path)
    signal = auto_research_exhaustion_signal(store, episode.episode_id)
    ordinary = auto_research_wrapup_spec(store, signal)
    _oversized_receipt_rows(store, episode, root, monkeypatch, "界" * 100_000, absurd=True)

    spec = auto_research_wrapup_spec(store, signal)
    receipt = spec.receipt

    assert len(_stored_receipt(spec)[0].encode("utf-8")) <= AGENT_TASK_RECEIPT_MAX_BYTES
    assert receipt.keys() == ordinary.receipt.keys()
    assert receipt["starting_instruction"] is None
    for key in ("actors", "command_facts", "graph_results", "child_work", "child_experiments"):
        assert receipt[key] == []
    assert receipt["omitted_actor_count"] == 1
    assert receipt["omitted_child_work_count"] == 16
    assert receipt["omitted_child_experiment_count"] == 16
    assert receipt["pending_child_admission_ids"] == []
    assert receipt["omitted_pending_child_admission_count"] == 16
    assert receipt["lifecycle"] == {
        "counts": {"pending": 2, "delivered": 0, "acknowledged": 0},
        "facts": [],
        "omitted_fact_count": 2,
    }
    for key in ("operational_meter", "task_status_counts", "experiment_allowance"):
        assert receipt[key] == ordinary.receipt[key]


def test_last_resort_compacts_unicode_envelope_diagnostic_without_changing_fence(tmp_path):
    store, episode, root = _episode(tmp_path)
    store.checkpoint_agent_task(
        root.operation_id, native_session_id="session", stage_root=str(tmp_path / "stage")
    )
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    diagnostic = "🔬" * 2_000
    signal = auto_research_exhaustion_signal(store, episode.episode_id, diagnostic=diagnostic)

    spec = auto_research_wrapup_spec(store, signal)
    admission = begin_episode_report_wrapup(store, spec)

    assert spec.diagnostic == diagnostic
    assert admission.episode.ending_diagnostic == diagnostic
    assert admission.wrapup is not None
    assert len(admission.wrapup.receipt_json.encode("utf-8")) <= AGENT_TASK_RECEIPT_MAX_BYTES
    assert json.loads(admission.wrapup.receipt_json)["diagnostic"].endswith("…")
    assert (admission.wrapup.receipt_json, admission.wrapup.receipt_sha256) == _stored_receipt(spec)
