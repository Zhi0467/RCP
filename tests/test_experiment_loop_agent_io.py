from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.experiment_loop_prompt import experiment_loop_wake_message
from rcp.api import create_app
from rcp.background import AgentTaskExecution
from rcp.core.models import Patch
from rcp.runs.experiment_loop import (
    _watcher_state,
    experiment_episode_context_values,
    experiment_watcher_delivery_request,
    experiment_watcher_output_name,
    preflight_episode_wake,
    stage_chat_experiment_watcher_resources,
)
from rcp.runs.shared import _parent_task_contract_path
from rcp.runs.work import _process_experiment_watcher_maintenance, stream_work_run
from rcp.service import RunRequest
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    WatcherContinuation,
    WatcherRecord,
)

from .helpers import seed_patch

_EXPERIMENT_ID = "exp/native-wake"


def test_experiment_watcher_wake_keeps_packages_available_without_reinvoking_them(
    tmp_path: Path,
) -> None:
    episode_id = "00000000-0000-4000-8000-000000000087"
    continuation = WatcherContinuation(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        patch_kind="experiment_loop",
        control_node_id=_EXPERIMENT_ID,
        control_revision=2,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=3,
        skill_ids=["experiment-causality"],
        invoked_skill_ids=["experiment-causality"],
    )
    watcher = WatcherRecord(
        watcher_id="watcher-turn-local-invocation",
        project_id="project-turn-local-invocation",
        origin_operation_id="origin-turn-local-invocation",
        origin_task_kind="node_chat",
        chat_id="chat-turn-local-invocation",
        node_id=_EXPERIMENT_ID,
        check_command="false",
        log_path=str(tmp_path / "detached.log"),
        cwd=str(tmp_path),
        continuation=continuation,
        status="completed",
        created_at="2026-08-08T00:00:00Z",
    )

    request = experiment_watcher_delivery_request(
        [watcher],
        trigger="watcher",
        episode_id=episode_id,
        invocation=2,
        invocation_ceiling=3,
        control_revision=2,
        decision_bundle=[],
        completion_criteria=[],
        session_id="native-session",
    )

    assert request.skill_ids == ["experiment-causality"]
    assert request.invoked_skill_ids == []
    assert request.invoked_workflow_ids == []


def _experiment_patch(*, invocation_ceiling: int = 3) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Added an Experiment for native wake tests.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": _EXPERIMENT_ID,
                        "type": "experiment",
                        "title": "Native wake continuity",
                        "objective": "Keep one bounded provider session across watcher wakes.",
                        "completion_criteria": ["The detached check is inspected."],
                        "invocation_ceiling": invocation_ceiling,
                    }
                ],
            }
        ],
    )


def _loop_request(
    episode_id: str,
    chat_id: str,
    *,
    invocation: int,
    trigger: str = "experiment_run",
    session_id: str | None = None,
    watcher_ids: list[str] | None = None,
    control_revision: int = 2,
) -> RunRequest:
    return RunRequest(
        provider="codex",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_scope="node",
        node_id=_EXPERIMENT_ID,
        message="Continue the bounded Experiment loop.",
        chat_id=chat_id,
        session_id=session_id,
        mode="work",
        trigger=trigger,
        patch_kind="experiment_loop",
        control_node_id=_EXPERIMENT_ID,
        control_revision=control_revision,
        control_episode_id=episode_id,
        control_invocation=invocation,
        control_invocation_ceiling=3,
        control_decision_bundle=[],
        control_completion_criteria=["The detached check is inspected."],
        watcher_ids=watcher_ids or [],
    )


def _execution(
    store: AppStore,
    project_id: str,
    operation_id: str,
    request: RunRequest,
    *,
    continuation: str = "fresh",
    stage_root: str | None = None,
    parent_operation_id: str | None = None,
    retry_feedback: tuple[str, ...] = (),
) -> AgentTaskExecution:
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="node_chat",
            status="running",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="running",
            attempt=2 if parent_operation_id else 1,
            parent_operation_id=parent_operation_id,
            native_session_id=request.session_id,
            stage_root=stage_root,
        )
    )
    store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {
            "kind": "node_chat",
            "attempt": 1,
            "has_parent": False,
            "resumed": continuation == "resume",
            "continuation_cause": continuation,
        },
    )
    return AgentTaskExecution(
        operation_id=operation_id,
        store=store,
        control=AgentProcessControl(),
        stage_root=stage_root,
        continuation=continuation,
        retry_feedback=retry_feedback,
    )


def _graph_update_from_events(events: list[AgentEvent]) -> dict[str, object]:
    for event in events:
        if event.event != "message" or not event.text:
            continue
        payload = json.loads(event.text)
        graph_update = payload.get("graph_update")
        if isinstance(graph_update, dict):
            return graph_update
    raise AssertionError("The Experiment-loop stream emitted no graph update.")


class _LoopLauncher:
    def __init__(self, native_session_id: str, watcher_cwd: Path, *, write_handoff: bool) -> None:
        self.native_session_id = native_session_id
        self.watcher_cwd = watcher_cwd
        self.write_handoff = write_handoff
        self.patch_payload: dict[str, object] | None = None
        self.contracts: list[str] = []
        self.sessions: list[str | None] = []

    async def stream(self, _provider, prompt, **kwargs):
        contract_path = Path(prompt.splitlines()[1])
        self.contracts.append(contract_path.read_text(encoding="utf-8"))
        self.sessions.append(kwargs.get("session_id"))
        workspace = Path(kwargs["cwd"])
        if self.write_handoff:
            (workspace / "watch.json").write_text(
                json.dumps(
                    [
                        {
                            "check_command": "false",
                            "log_path": str(self.watcher_cwd / "detached.log"),
                            "cwd": str(self.watcher_cwd),
                        }
                    ]
                ),
                encoding="utf-8",
            )
        if self.patch_payload is not None:
            (workspace / "patch.json").write_text(
                json.dumps(self.patch_payload),
                encoding="utf-8",
            )
        yield AgentEvent(event="session", session_id=self.native_session_id)
        yield AgentEvent(event="answer", text="Inspected the bounded work.")
        yield AgentEvent(event="done")


async def _events(stream) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async for frame in stream:
        events.append(AgentEvent.model_validate_json(frame.removeprefix("data: ").strip()))
    return events


def test_retry_recovers_evicted_contract_from_same_stage_lineage(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    stage = tmp_path / "chat-stage"
    (stage / "inputs").mkdir(parents=True)
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        chat_id="chat-contract-recovery",
        node_id=_EXPERIMENT_ID,
        message="Continue the bounded Experiment loop.",
        mode="work",
        session_id="native-contract-recovery",
    )
    original = _execution(
        store,
        "project-contract-recovery",
        "watcher-wake",
        request,
        continuation="watcher_wake",
        stage_root=str(stage),
    )
    contract = "# Exact watcher-wake contract\n\nContinue the same bounded invocation.\n"
    digest = hashlib.sha256(contract.encode("utf-8")).hexdigest()
    original_path = stage / "inputs" / "task-watcher-wake.md"
    original_path.write_text(contract, encoding="utf-8")
    store.record_agent_task_contract("watcher-wake", "experiment_loop_wake", contract, digest)
    store.record_agent_task_receipt(
        "watcher-wake",
        "agent_prompt",
        {"contract_path": str(original_path)},
        tier="diagnostic",
    )
    for index in range(32):
        store.record_agent_task_receipt(
            "watcher-wake",
            "native_agent_checkpoint",
            {"index": index},
            tier="diagnostic",
        )
    assert all(
        receipt.category != "agent_prompt" for receipt in store.agent_task_receipts("watcher-wake")
    )
    store.fail_agent_task(original.operation_id, "Provider session limit reached.")

    failed_retry = _execution(
        store,
        "project-contract-recovery",
        "failed-retry",
        request,
        continuation="retry",
        stage_root=str(stage),
        parent_operation_id=original.operation_id,
    )
    store.fail_agent_task(failed_retry.operation_id, "Original task contract was unavailable.")
    retried = _execution(
        store,
        "project-contract-recovery",
        "retry-after-failed-retry",
        request,
        continuation="retry",
        stage_root=str(stage),
        parent_operation_id="failed-retry",
    )

    recovered_path = _parent_task_contract_path(retried, stage, None)

    assert Path(recovered_path).read_text(encoding="utf-8") == contract
    receipt = next(
        item
        for item in store.agent_task_receipts(retried.operation_id)
        if item.category == "original_contract_recovered"
    )
    assert receipt.payload["ancestor_operation_id"] == "watcher-wake"
    assert receipt.payload["role"] == "experiment_loop_wake"
    assert receipt.payload["sha256"] == digest


def test_retry_contract_recovery_does_not_cross_stage_boundary(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    old_stage = tmp_path / "old-stage"
    new_stage = tmp_path / "new-stage"
    (old_stage / "inputs").mkdir(parents=True)
    (new_stage / "inputs").mkdir(parents=True)
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        chat_id="chat-stage-boundary",
        node_id=_EXPERIMENT_ID,
        message="Continue the bounded Experiment loop.",
        mode="work",
        session_id="native-stage-boundary",
    )
    original = _execution(
        store,
        "project-stage-boundary",
        "old-binding",
        request,
        stage_root=str(old_stage),
    )
    contract = "old provider contract\n"
    store.record_agent_task_contract(
        original.operation_id,
        "experiment_loop_wake",
        contract,
        hashlib.sha256(contract.encode("utf-8")).hexdigest(),
    )
    store.fail_agent_task(original.operation_id, "Provider session limit reached.")
    retried = _execution(
        store,
        "project-stage-boundary",
        "new-binding-retry",
        request,
        continuation="retry",
        stage_root=str(new_stage),
        parent_operation_id=original.operation_id,
    )

    with pytest.raises(ValueError, match="no recorded original task contract"):
        _parent_task_contract_path(retried, new_stage, None)


def test_compact_wake_message_is_human_style_and_authority_truthful() -> None:
    message = experiment_loop_wake_message(
        focused_experiment_id=_EXPERIMENT_ID,
        invocation=2,
        invocation_ceiling=4,
        previous_graph_result="applied as revision 9",
        previous_watcher_ids=["watch/old-a", "watch/old-b"],
        delivered_watcher_ids=["watch/ready"],
        loop_control_path="/stage/control.json",
        watcher_state_path="/stage/watchers.json",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        patch_path="/stage/patch.json",
        watch_path="/stage/watch.json",
        output_schema_path="/stage/schema.json",
        validator_command="python3 /stage/validate.py /stage/patch.json",
    )

    assert message.startswith(
        f"The watched work for Experiment `{_EXPERIMENT_ID}` is ready for another look."
    )
    assert "turn 2 of 4" in message
    assert "invocation" not in message.lower()
    assert "- graph update: applied as revision 9" in message
    assert "- watchers armed: watch/old-a, watch/old-b" in message
    assert "This turn was triggered by: watch/ready" in message
    normalized = " ".join(message.split())
    assert "does not mean the work succeeded" in normalized
    assert "submit a replacement only when the authoritative state shows" in normalized
    assert "do not wait or poll for detached work; finish this" in normalized
    assert "2. You need human input." in message
    assert "3. The Experiment is operationally finished." in message
    assert (
        "the scientific result may be successful, unsuccessful, inconclusive, or invalid"
        in normalized
    )
    assert "`current_summary`, and `next_action`" in normalized
    assert "use `next_action: null` when no further action remains" in normalized
    assert "trying to write `current_summary` or `next_action`" not in normalized
    assert '"check_command"' in message
    assert "These context values replace" not in message
    assert "# RCP Experiment-loop task contract" not in message


def test_episode_context_ontology_identity_changes_with_extension_definitions() -> None:
    base = experiment_episode_context_values(
        ontology_extensions=True,
        ontology={
            "types": [{"name": "training_run", "base_type": "experiment"}],
            "fields": [],
            "relations": [],
        },
        repositories=[],
        skill_pointers=[],
    )
    changed = experiment_episode_context_values(
        ontology_extensions=True,
        ontology={
            "types": [{"name": "evaluation_run", "base_type": "experiment"}],
            "fields": [],
            "relations": [],
        },
        repositories=[],
        skill_pointers=[],
    )

    assert base["ontology"] != changed["ontology"]
    assert base["ontology"]["extensions"] is True
    assert len(base["ontology"]["sha256"]) == 64


def test_watcher_provenance_model_and_reasoning_do_not_select_or_block_session(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "episode-stage"
    stage.mkdir()
    runtime = ExperimentLoopRuntime(
        episode_id="00000000-0000-4000-8000-000000000073",
        provider="codex",
        model="current-episode-model",
        reasoning="high",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_id="chat-native-wake",
    )
    episode = ExperimentEpisodeRecord(
        episode_id=runtime.episode_id or "",
        project_id="project-native-wake",
        control_node_id=_EXPERIMENT_ID,
        provider="codex",
        execution_machine="laptop",
        native_session_id="provider-session-native-wake",
        stage_root=str(stage),
        chat_id="chat-native-wake",
        created_at="2026-08-06T00:00:00Z",
        updated_at="2026-08-06T00:00:00Z",
    )
    continuation = WatcherContinuation(
        provider="codex",
        model="older-watcher-model",
        reasoning="low",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        patch_kind="experiment_loop",
        control_node_id=_EXPERIMENT_ID,
        control_revision=2,
        control_episode_id=episode.episode_id,
        control_invocation=1,
        control_invocation_ceiling=3,
        control_decision_bundle=[],
        control_completion_criteria=["The detached check is inspected."],
    )
    watcher = WatcherRecord(
        watcher_id="watcher-model-provenance",
        project_id=episode.project_id,
        origin_operation_id="loop-initial",
        origin_task_kind="node_chat",
        chat_id="chat-native-wake",
        node_id=_EXPERIMENT_ID,
        experiment_episode_id=episode.episode_id,
        check_command="false",
        log_path=str(tmp_path / "detached.log"),
        cwd=str(tmp_path),
        continuation=continuation,
        status="completed",
        created_at="2026-08-06T00:00:00Z",
    )

    readiness = preflight_episode_wake(runtime, episode, [watcher])

    assert readiness.readiness == "ready"
    assert readiness.session_id == episode.native_session_id


@pytest.mark.asyncio
async def test_wake_uses_compact_contract_and_commits_baseline_only_after_handoff(
    manifest,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    service.history.append(seed_patch())
    service.history.append(_experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    episode_id = "00000000-0000-4000-8000-000000000073"
    chat_id = "chat-native-wake"
    native_session_id = "provider-session-native-wake"
    initial_request = _loop_request(
        episode_id,
        chat_id,
        invocation=1,
        control_revision=service.history.state().revision,
    )
    initial_execution = _execution(
        store,
        project_id,
        "loop-initial",
        initial_request,
    )
    launcher = _LoopLauncher(native_session_id, tmp_path, write_handoff=True)

    initial_events = await _events(
        stream_work_run(
            service,
            launcher,
            initial_request,
            data_dir,
            execution=initial_execution,
        )
    )
    assert not [event for event in initial_events if event.event == "error"]
    assert launcher.contracts[0].startswith("# RCP Experiment-loop task contract")
    episode = store.experiment_episode(episode_id)
    assert episode is not None
    assert episode.last_turn_operation_id == "loop-initial"
    assert episode.last_turn_invocation == 1
    assert episode.native_session_id == native_session_id
    assert episode.stage_root == initial_execution.stage_root
    assert episode.last_graph_result == "no graph change"
    assert len(episode.last_watcher_ids) == 1
    initial_baseline = episode.context_baseline
    assert set(initial_baseline) == {"ontology", "repositories", "skills"}
    store.complete_agent_task("loop-initial", applied_revision=None, result={})

    # Make one prior baseline value stale. The wake must send only that exact
    # replacement and commit the new complete baseline after its handoff succeeds.
    assert episode.provider and episode.execution_machine and episode.chat_id
    store.commit_experiment_episode_turn(
        episode_id=episode.episode_id,
        project_id=episode.project_id,
        control_node_id=episode.control_node_id,
        provider=episode.provider,
        execution_machine=episode.execution_machine,
        execution_host=episode.execution_host,
        native_session_id=native_session_id,
        stage_host=episode.stage_host,
        stage_root=episode.stage_root or "",
        chat_id=episode.chat_id,
        operation_id="loop-initial",
        invocation=1,
        graph_result=episode.last_graph_result or "",
        watcher_ids=episode.last_watcher_ids,
        context_baseline={**initial_baseline, "repositories": []},
    )
    delivered_id = episode.last_watcher_ids[0]
    store.record_watcher_check(delivered_id, status="completed", exit_code=0, error=None)
    wake_request = _loop_request(
        episode_id,
        chat_id,
        invocation=2,
        trigger="watcher",
        session_id=native_session_id,
        watcher_ids=[delivered_id],
        control_revision=initial_request.control_revision or 0,
    )
    wake_execution = _execution(
        store,
        project_id,
        "loop-wake",
        wake_request,
        continuation="watcher_wake",
        stage_root=episode.stage_root,
    )
    assert wake_execution.stage_root is not None
    stale_workspace = Path(wake_execution.stage_root)
    (stale_workspace / "patch.json").write_text("stale Patch", encoding="utf-8")
    (stale_workspace / "watch.json").write_text("stale watcher", encoding="utf-8")
    # This schema-valid deliverable cannot update an unknown graph node. The
    # provider keeps it byte-identical through correction, so RCP truthfully
    # records a rejected graph handoff while retaining the valid watchers.
    launcher.patch_payload = {
        "summary": "Tried to update an unavailable graph node.",
        "ops": [
            {
                "op": "update_nodes",
                "nodes": [{"id": "hyp/missing", "changes": {"status": "supported"}}],
            }
        ],
        "repositories_read": [],
        "change_summary": ["Tried to update an unavailable graph node."],
    }

    wake_events = await _events(
        stream_work_run(
            service,
            launcher,
            wake_request,
            data_dir,
            execution=wake_execution,
        )
    )
    assert not [event for event in wake_events if event.event == "error"]
    assert launcher.sessions[:2] == [None, native_session_id]
    wake_contract = launcher.contracts[1]
    assert wake_contract.startswith(
        f"The watched work for Experiment `{_EXPERIMENT_ID}` is ready for another look."
    )
    assert "# RCP Experiment-loop task contract" not in wake_contract
    assert "turn 2 of 3" in wake_contract
    assert "These context values replace what this session was given:" in wake_contract
    replacement = wake_contract.split(
        "These context values replace what this session was given:\n", 1
    )[1].split("\n\nFor this turn", 1)[0]
    assert json.loads(replacement) == {"repositories": initial_baseline["repositories"]}
    assert "task-loop-wake-experiment-control-watcher_wake.json" in wake_contract
    assert "task-loop-wake-experiment-watchers.json" in wake_contract
    assert str(service.manifest.research_dir / "graph.json") in wake_contract
    assert str(service.manifest.research_dir / "research.md") in wake_contract
    assert "chat-patch-schema-" in wake_contract
    assert "chat-validator-client-" in wake_contract

    committed = store.experiment_episode(episode_id)
    assert committed is not None
    assert committed.last_turn_operation_id == "loop-wake"
    assert committed.last_turn_invocation == 2
    assert committed.native_session_id == native_session_id
    assert committed.last_graph_result is not None
    assert committed.last_graph_result.startswith("rejected:")
    assert committed.context_baseline == initial_baseline
    assert len(committed.last_watcher_ids) == 1
    store.complete_agent_task("loop-wake", applied_revision=None, result={})

    # A later provider answer with no valid joint handoff gets its in-session
    # correction, but it cannot advance the episode binding or context baseline.
    failed_delivered_id = committed.last_watcher_ids[0]
    store.record_watcher_check(
        failed_delivered_id,
        status="completed",
        exit_code=0,
        error=None,
    )
    failed_request = _loop_request(
        episode_id,
        chat_id,
        invocation=3,
        trigger="watcher",
        session_id=native_session_id,
        watcher_ids=[failed_delivered_id],
        control_revision=initial_request.control_revision or 0,
    )
    failed_execution = _execution(
        store,
        project_id,
        "loop-wake-failed",
        failed_request,
        continuation="watcher_wake",
        stage_root=committed.stage_root,
    )
    failing_launcher = _LoopLauncher(native_session_id, tmp_path, write_handoff=False)
    failed_events = await _events(
        stream_work_run(
            service,
            failing_launcher,
            failed_request,
            data_dir,
            execution=failed_execution,
        )
    )
    assert any(event.event == "error" for event in failed_events)
    unchanged = store.experiment_episode(episode_id)
    assert unchanged is not None
    assert unchanged.last_turn_operation_id == "loop-wake"
    assert unchanged.last_turn_invocation == 2
    assert unchanged.context_baseline == initial_baseline


@pytest.mark.asyncio
async def test_provider_switch_stages_full_recovery_contract_with_durable_provenance(
    manifest,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    service.history.append(seed_patch())
    service.history.append(_experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    episode_id = "00000000-0000-4000-8000-000000000075"
    initial_request = _loop_request(
        episode_id,
        "chat-provider-switch",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    initial_execution = _execution(store, project_id, "loop-before-switch", initial_request)
    initial_launcher = _LoopLauncher("codex-session-before-switch", tmp_path, write_handoff=True)
    initial_events = await _events(
        stream_work_run(
            service,
            initial_launcher,
            initial_request,
            data_dir,
            execution=initial_execution,
        )
    )
    assert not [event for event in initial_events if event.event == "error"]
    store.fail_agent_task("loop-before-switch", "Provider session limit reached.")

    diagnostics = ("Attempt 1 (failed) failed with: Provider session limit reached.",)
    # The orchestration layer authorizes and persists the actual provider override. At this seam,
    # `handoff` plus a missing session id is the provider-switch signal that selects the full
    # provisional-session contract rather than the compact same-provider Retry contract.
    switch_request = initial_request.model_copy(update={"session_id": None})
    switch_execution = _execution(
        store,
        project_id,
        "loop-provider-switch",
        switch_request,
        continuation="handoff",
        parent_operation_id="loop-before-switch",
        retry_feedback=diagnostics,
    )
    switch_launcher = _LoopLauncher("claude-session-after-switch", tmp_path, write_handoff=True)
    switch_events = await _events(
        stream_work_run(
            service,
            switch_launcher,
            switch_request,
            data_dir,
            execution=switch_execution,
        )
    )

    assert switch_launcher.sessions == [None]
    contract = switch_launcher.contracts[0]
    compact = " ".join(contract.split())
    assert contract.startswith("# RCP Experiment-loop task contract")
    assert "Explicit same-episode provider-switch recovery" in contract
    assert "task-loop-provider-switch-retry-diagnostics.json" in contract
    assert "same Experiment episode and the same invocation" in compact
    assert "inspect authoritative external state" in compact
    persisted = store.agent_task_contract("loop-provider-switch", "work_retry_base")
    assert persisted == contract
    diagnostics_path = Path(
        next(
            line.rsplit("`", 2)[1]
            for line in contract.splitlines()
            if line.startswith("- Exact prior failure diagnostics:")
        )
    )
    assert json.loads(diagnostics_path.read_text(encoding="utf-8")) == {
        "prior_attempt_diagnostics": list(diagnostics)
    }
    # Binding replacement is owned by the recovery orchestration layer. Prompt staging must remain
    # inspectable even if a later handoff or binding check rejects the provisional session.
    assert switch_events


@pytest.mark.asyncio
async def test_unbound_initial_handoff_does_not_claim_provider_switch_recovery(
    manifest,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    service.history.append(seed_patch())
    service.history.append(_experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    episode_id = "00000000-0000-4000-8000-000000000076"
    request = _loop_request(
        episode_id,
        "chat-unbound-handoff",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    failed_execution = _execution(store, project_id, "loop-unbound-failure", request)
    store.fail_agent_task(failed_execution.operation_id, "Provider unavailable before launch.")
    handoff_execution = _execution(
        store,
        project_id,
        "loop-unbound-handoff",
        request,
        continuation="handoff",
        parent_operation_id=failed_execution.operation_id,
        retry_feedback=("Attempt 1 (failed) failed with: Provider unavailable before launch.",),
    )
    launcher = _LoopLauncher("first-established-session", tmp_path, write_handoff=True)

    events = await _events(
        stream_work_run(
            service,
            launcher,
            request,
            data_dir,
            execution=handoff_execution,
        )
    )

    assert not [event for event in events if event.event == "error"]
    contract = launcher.contracts[0]
    assert contract.startswith("# RCP Experiment-loop task contract")
    assert "Explicit same-episode provider-switch recovery" not in contract
    assert "provisional replacement provider session" not in contract
    assert "Exact prior failure diagnostics" not in contract
    assert store.agent_task_contract("loop-unbound-handoff", "work_retry_base") == contract


@pytest.mark.asyncio
async def test_manual_graph_repair_updates_the_episode_handoff_summary(
    manifest,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    service.history.append(seed_patch())
    service.history.append(_experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    episode_id = "00000000-0000-4000-8000-000000000074"
    native_session_id = "provider-session-graph-repair"
    initial_request = _loop_request(
        episode_id,
        "chat-graph-repair",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    initial_execution = _execution(
        store,
        project_id,
        "loop-rejected",
        initial_request,
    )
    launcher = _LoopLauncher(native_session_id, tmp_path, write_handoff=True)
    launcher.patch_payload = {
        "summary": "Tried to update an unavailable graph node.",
        "ops": [
            {
                "op": "update_nodes",
                "nodes": [{"id": "hyp/missing", "changes": {"status": "supported"}}],
            }
        ],
        "repositories_read": [],
        "change_summary": ["Tried to update an unavailable graph node."],
    }

    initial_events = await _events(
        stream_work_run(
            service,
            launcher,
            initial_request,
            data_dir,
            execution=initial_execution,
        )
    )
    assert not [event for event in initial_events if event.event == "error"]
    rejected_graph = _graph_update_from_events(initial_events)
    assert rejected_graph["status"] == "rejected"
    assert rejected_graph["repairable"] is True
    store.checkpoint_agent_task(
        "loop-rejected",
        native_session_id=native_session_id,
        stage_root=initial_execution.stage_root,
    )
    store.complete_agent_task(
        "loop-rejected",
        applied_revision=None,
        result={"messages": ["Inspected the bounded work."], "graph_update": rejected_graph},
    )
    store.claim_agent_task_graph_repair("loop-rejected")
    rejected_episode = store.experiment_episode(episode_id)
    assert rejected_episode is not None
    assert rejected_episode.last_graph_result is not None
    assert rejected_episode.last_graph_result.startswith("rejected:")

    repair_request = initial_request.model_copy(
        update={"session_id": native_session_id, "message": None}
    )
    repair_execution = _execution(
        store,
        project_id,
        "loop-graph-repair",
        repair_request,
        continuation="graph_repair",
        stage_root=rejected_episode.stage_root,
        parent_operation_id="loop-rejected",
    )
    repair_launcher = _LoopLauncher(native_session_id, tmp_path, write_handoff=False)
    repair_launcher.patch_payload = {
        "summary": "Finished the Experiment's operational work.",
        "ops": [
            {
                "op": "update_nodes",
                "nodes": [{"id": _EXPERIMENT_ID, "changes": {"status": "completed"}}],
            }
        ],
        "repositories_read": [],
        "change_summary": ["Finished the Experiment's operational work."],
    }

    repair_events = await _events(
        stream_work_run(
            service,
            repair_launcher,
            repair_request,
            data_dir,
            execution=repair_execution,
        )
    )
    assert not [event for event in repair_events if event.event == "error"]
    applied_graph = _graph_update_from_events(repair_events)
    assert applied_graph["status"] == "applied"

    repaired_episode = store.experiment_episode(episode_id)
    assert repaired_episode is not None
    assert repaired_episode.last_turn_operation_id == "loop-graph-repair"
    assert repaired_episode.last_turn_invocation == 1
    assert repaired_episode.last_graph_result == (
        f"applied as revision {applied_graph['applied_revision']}"
    )
    assert repaired_episode.last_watcher_ids == rejected_episode.last_watcher_ids
    assert repaired_episode.context_baseline == rejected_episode.context_baseline


def _store_task(
    store: AppStore,
    *,
    operation_id: str,
    project_id: str,
    episode_id: str,
    status: str = "succeeded",
) -> None:
    request = _loop_request(episode_id, "watcher-state-chat", invocation=1)
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="node_chat",
            status=status,
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message=status,
        )
    )


def _store_watcher(
    store: AppStore,
    *,
    watcher_id: str,
    project_id: str,
    episode_id: str,
    status: str,
    notified: bool = False,
) -> None:
    now = store.now()
    continuation = WatcherContinuation(
        provider="codex",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        patch_kind="experiment_loop",
        control_node_id=_EXPERIMENT_ID,
        control_revision=2,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=3,
        control_decision_bundle=[],
        control_completion_criteria=["The detached check is inspected."],
    )
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id=watcher_id,
                project_id=project_id,
                origin_operation_id=f"origin-{episode_id}",
                origin_task_kind="node_chat",
                chat_id="watcher-state-chat",
                node_id=_EXPERIMENT_ID,
                execution_host="",
                check_command="false",
                log_path=f"/tmp/{watcher_id}.log",
                cwd="/tmp",
                continuation=continuation,
                status=status,
                created_at=now,
                completed_at=now if status == "completed" else None,
                notified=notified,
            )
        ]
    )


@pytest.mark.asyncio
async def test_node_chat_stages_current_experiment_watcher_state_and_clears_stale_output(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "chat-resource.sqlite3")
    project_id = "project-chat-resource"
    episode_id = "00000000-0000-4000-8000-000000000098"
    _store_task(
        store,
        operation_id="loop-resource-root",
        project_id=project_id,
        episode_id=episode_id,
    )
    store.commit_experiment_episode_turn(
        episode_id=episode_id,
        project_id=project_id,
        control_node_id=_EXPERIMENT_ID,
        provider="codex",
        execution_machine="laptop",
        execution_host="episode.example",
        native_session_id="loop-resource-session",
        stage_host=None,
        stage_root=str(tmp_path / "loop-resource-stage"),
        chat_id="watcher-state-chat",
        operation_id="loop-resource-root",
        invocation=1,
        graph_result="no graph change",
        watcher_ids=[],
        context_baseline={},
    )
    _store_watcher(
        store,
        watcher_id="resource-active",
        project_id=project_id,
        episode_id=episode_id,
        status="active",
    )
    request = RunRequest(
        chat_scope="node",
        chat_id="maintenance-chat",
        node_id=_EXPERIMENT_ID,
        message="Inspect the observer.",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )
    execution = _execution(
        store,
        project_id,
        "maintenance-discuss",
        request,
    )
    stage = tmp_path / "maintenance-stage"
    workspace = stage / "workspace"
    workspace.mkdir(parents=True)
    stale = workspace / experiment_watcher_output_name(_EXPERIMENT_ID)
    stale.write_text("stale", encoding="utf-8")

    resources = await stage_chat_experiment_watcher_resources(
        request,
        execution,
        stage,
        None,
        workspace=workspace,
        token="maintenance-discuss",
        clear_stale=True,
    )

    assert len(resources) == 1
    resource = resources[0]
    assert resource.resource.control_node_id == _EXPERIMENT_ID
    assert resource.resource.episode_id == episode_id
    assert resource.resource.execution_host == "episode.example"
    assert resource.watch_path == str(workspace / experiment_watcher_output_name(_EXPERIMENT_ID))
    assert not stale.exists()
    state = json.loads(Path(resource.watcher_state_path).read_text(encoding="utf-8"))
    assert [item["watcher_id"] for item in state] == ["resource-active"]


@pytest.mark.asyncio
async def test_unstaged_experiment_watcher_output_is_permission_rejected(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "unstaged-resource.sqlite3")
    project_id = "project-unstaged-resource"
    request = RunRequest(
        chat_scope="node",
        chat_id="maintenance-chat",
        node_id=_EXPERIMENT_ID,
        message="Try an unstaged resource.",
        run_truth_scope=["repo-a"],
        mode="work",
    )
    execution = _execution(store, project_id, "maintenance-work", request)
    workspace = tmp_path / "unstaged-workspace"
    workspace.mkdir()
    guessed = workspace / experiment_watcher_output_name("exp/outside-scope")
    guessed.write_text("[]", encoding="utf-8")

    frames, session_id, paused = await _process_experiment_watcher_maintenance(
        launcher=_LoopLauncher("maintenance-session", tmp_path, write_handoff=False),
        request=request,
        execution=execution,
        staged_resources=[],
        workspace=workspace,
        remote_stage=None,
        local_stage=tmp_path / "unstaged-stage",
        base_contract_path="/stage/inputs/chat-master.md",
        token="maintenance-work",
        native_session_id="maintenance-session",
        read_dirs=[],
        write_dirs=[],
        execution_host="",
        provider_binary=None,
        retry_output_digests={},
    )

    assert frames == []
    assert session_id == "maintenance-session"
    assert paused is False
    rejection = next(
        item
        for item in store.agent_task_receipts(execution.operation_id)
        if item.category == "experiment_watcher_maintenance_rejected"
    )
    assert "permission denied" in str(rejection.payload["problem"]).casefold()
    assert "not staged" in str(rejection.payload["problem"])
    assert "missing" not in str(rejection.payload["problem"]).casefold()


@pytest.mark.asyncio
async def test_retry_does_not_reapply_a_previous_attempts_watcher_file(tmp_path: Path) -> None:
    """A Retry reuses the chat folder, so a survivor is not this attempt's handoff.

    Applying it would commit the previous attempt's maintenance under this
    attempt's authorization, which invariant 10c forbids.
    """

    store = AppStore(tmp_path / "retry-survivor.sqlite3")
    project_id = "project-retry-survivor"
    request = RunRequest(
        chat_scope="node",
        chat_id="maintenance-chat",
        node_id=_EXPERIMENT_ID,
        message="Repair the observers.",
        run_truth_scope=["repo-a"],
        mode="work",
    )
    execution = _execution(
        store,
        project_id,
        "maintenance-retry",
        request,
        continuation="retry",
        parent_operation_id="maintenance-work",
    )
    workspace = tmp_path / "retry-workspace"
    workspace.mkdir()
    survivor = workspace / experiment_watcher_output_name(_EXPERIMENT_ID)
    survivor_text = '[{"stop_watcher_id": "w-1", "reason": "Superseded"}]'
    survivor.write_text(survivor_text, encoding="utf-8")
    predecessor_digest = hashlib.sha256(survivor_text.encode("utf-8")).hexdigest()

    frames, session_id, paused = await _process_experiment_watcher_maintenance(
        launcher=_LoopLauncher("maintenance-session", tmp_path, write_handoff=False),
        request=request,
        execution=execution,
        staged_resources=[],
        workspace=workspace,
        remote_stage=None,
        local_stage=tmp_path / "retry-stage",
        base_contract_path="/stage/inputs/chat-master.md",
        token="maintenance-retry",
        native_session_id="maintenance-session",
        read_dirs=[],
        write_dirs=[],
        execution_host="",
        provider_binary=None,
        retry_output_digests={survivor.name: predecessor_digest},
    )

    assert frames == []
    assert session_id == "maintenance-session"
    assert paused is False
    receipts = store.agent_task_receipts("maintenance-retry")
    comparisons = [item for item in receipts if item.category == "retry_deliverable_comparison"]
    assert [item.payload["unchanged"] for item in comparisons] == [True]
    assert not [
        item for item in receipts if item.category == "experiment_watcher_maintenance_rejected"
    ]


def test_watcher_state_includes_current_and_compatible_stopped_history(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "watcher-state.sqlite3")
    project_id = "project-watcher-state"
    older_episode = "00000000-0000-4000-8000-000000000070"
    stopped_episode = "00000000-0000-4000-8000-000000000071"
    current_episode = "00000000-0000-4000-8000-000000000072"
    _store_task(
        store,
        operation_id="older-root",
        project_id=project_id,
        episode_id=older_episode,
    )
    _store_task(
        store,
        operation_id="stopped-root",
        project_id=project_id,
        episode_id=stopped_episode,
    )
    store.commit_experiment_episode_turn(
        episode_id=stopped_episode,
        project_id=project_id,
        control_node_id=_EXPERIMENT_ID,
        provider="codex",
        execution_machine="laptop",
        execution_host="",
        native_session_id="stopped-session",
        stage_host=None,
        stage_root="/tmp/stopped-stage",
        chat_id="watcher-state-chat",
        operation_id="stopped-root",
        invocation=1,
        graph_result="no graph change",
        watcher_ids=[],
        context_baseline={},
    )
    stopped = store.request_experiment_loop_stop(project_id, _EXPERIMENT_ID)
    assert stopped is not None and stopped.stop_requested_at is not None
    _store_task(
        store,
        operation_id="current-root",
        project_id=project_id,
        episode_id=current_episode,
        status="running",
    )

    _store_watcher(
        store,
        watcher_id="older-stopped",
        project_id=project_id,
        episode_id=older_episode,
        status="stopped",
        notified=True,
    )
    _store_watcher(
        store,
        watcher_id="previous-stopped",
        project_id=project_id,
        episode_id=stopped_episode,
        status="stopped",
        notified=True,
    )
    _store_watcher(
        store,
        watcher_id="previous-completed-notified",
        project_id=project_id,
        episode_id=stopped_episode,
        status="completed",
        notified=True,
    )
    _store_watcher(
        store,
        watcher_id="current-active",
        project_id=project_id,
        episode_id=current_episode,
        status="active",
    )
    _store_watcher(
        store,
        watcher_id="current-completed",
        project_id=project_id,
        episode_id=current_episode,
        status="completed",
    )
    _store_watcher(
        store,
        watcher_id="current-stopped",
        project_id=project_id,
        episode_id=current_episode,
        status="stopped",
        notified=True,
    )
    _store_watcher(
        store,
        watcher_id="current-completed-notified",
        project_id=project_id,
        episode_id=current_episode,
        status="completed",
        notified=True,
    )
    _store_watcher(
        store,
        watcher_id="delivered-notified",
        project_id=project_id,
        episode_id=stopped_episode,
        status="completed",
        notified=True,
    )
    execution = AgentTaskExecution(
        operation_id="current-root",
        store=store,
        control=AgentProcessControl(),
    )

    initial_ids = {
        item["watcher_id"]
        for item in _watcher_state(
            execution,
            _EXPERIMENT_ID,
            [],
            current_episode,
            "initial_run",
        )
    }
    assert initial_ids == {
        "older-stopped",
        "previous-stopped",
        "current-active",
        "current-completed",
        "current-stopped",
    }

    wake_ids = {
        item["watcher_id"]
        for item in _watcher_state(
            execution,
            _EXPERIMENT_ID,
            ["delivered-notified"],
            current_episode,
            "watcher_wake",
        )
    }
    assert wake_ids == {
        "delivered-notified",
        "current-active",
        "current-completed",
        "current-stopped",
    }
