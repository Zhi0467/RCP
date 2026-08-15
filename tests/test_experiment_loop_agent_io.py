from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.experiment_loop_prompt import experiment_loop_wake_message
from rcp.background import AgentTaskExecution
from rcp.core.models import AuthorizedHuman, Patch
from rcp.runs.experiment_loop import (
    _watcher_state,
    experiment_episode_context_values,
    experiment_watcher_delivery_request,
    experiment_watcher_output_name,
    preflight_episode_wake,
    stage_chat_experiment_watcher_resources,
)
from rcp.runs.shared import _parent_task_contract_path
from rcp.runs.work import (
    _apply_work_patch,
    _process_experiment_watcher_maintenance,
    stream_work_run,
)
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    WatcherContinuation,
    WatcherRecord,
)

from .helpers import append_fixture_patch, seed_patch
from .helpers import create_named_app as create_app

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
    dispatch_authority = resolve_dispatch_authority("node_chat", request)
    assert dispatch_authority is not None
    owner = store.local_owner
    assert owner is not None
    if owner.display_name is None:
        owner = store.rename_space_user(owner.user_id, "Test researcher")
    episode_id = request.control_episode_id if request.patch_kind == "experiment_loop" else None
    record = AgentTaskRecord(
        operation_id=operation_id,
        project_id=project_id,
        episode_id=episode_id,
        kind="node_chat",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="queued",
        attempt=2 if parent_operation_id else 1,
        parent_operation_id=parent_operation_id,
        native_session_id=request.session_id,
        stage_root=stage_root,
        authorized_by=AuthorizedHuman(
            space_id=store.space_id,
            user_id=owner.user_id,
            display_name=owner.display_name,
        ),
        dispatch_authority=dispatch_authority,
    )
    if request.patch_kind != "experiment_loop":
        store.create_agent_task(record)
    elif parent_operation_id is not None:
        store.create_experiment_recovery_task(record)
    elif request.trigger == "watcher":
        stored = store.create_experiment_watcher_invocation(record, request.watcher_ids)
        assert stored is not None
    else:
        store.create_experiment_episode_with_invocation(record, request.watcher_ids)
    store.mark_agent_task_running(operation_id)
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
                    {
                        "external": [
                            {
                                "check_command": "false",
                                "log_path": str(self.watcher_cwd / "detached.log"),
                                "cwd": str(self.watcher_cwd),
                            }
                        ],
                        "graph": [],
                    }
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


@pytest.mark.asyncio
async def test_patch_only_watcher_correction_accepts_unchanged_empty_watch_list(
    manifest,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    episode_id = "00000000-0000-4000-8000-000000000095"
    request = _loop_request(
        episode_id,
        "chat-patch-only-watch-correction",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    execution = _execution(
        app.state.background_tasks.store,
        project_id,
        "loop-patch-only-watch-correction",
        request,
    )

    class PatchOnlyCorrectionLauncher:
        def __init__(self) -> None:
            self.contracts: list[str] = []

        async def stream(self, _provider, prompt, **kwargs):
            contract_path = Path(prompt.splitlines()[1])
            contract = contract_path.read_text(encoding="utf-8")
            self.contracts.append(contract)
            workspace = Path(kwargs["cwd"])
            (workspace / "watch.json").write_text('{"external":[],"graph":[]}\n', encoding="utf-8")
            correcting = "watcher correction" in contract.casefold()
            next_action = None if correcting else "Analyze and document the remaining results."
            (workspace / "patch.json").write_text(
                json.dumps(
                    {
                        "summary": "Finished the Experiment handoff.",
                        "ops": [
                            {
                                "op": "update_nodes",
                                "nodes": [
                                    {
                                        "id": _EXPERIMENT_ID,
                                        "changes": {
                                            "status": "completed",
                                            "next_action": next_action,
                                        },
                                    }
                                ],
                            }
                        ],
                        "repositories_read": [],
                        "change_summary": ["Finished the Experiment handoff."],
                    }
                ),
                encoding="utf-8",
            )
            yield AgentEvent(event="session", session_id="patch-only-correction-session")
            yield AgentEvent(event="answer", text="Repaired the terminal handoff.")
            yield AgentEvent(event="done")

    launcher = PatchOnlyCorrectionLauncher()
    events = await _events(
        stream_work_run(
            service,
            launcher,
            request,
            data_dir,
            execution=execution,
        )
    )

    assert not [event for event in events if event.event == "error"]
    assert len(launcher.contracts) == 2
    assert launcher.contracts[1].startswith("# RCP Experiment-loop watcher correction")
    assert "Judge the terminal Patch/watch pair" in launcher.contracts[1]
    assert "completed` Experiment with a non-empty `next_action`" in launcher.contracts[1]
    graph_update = _graph_update_from_events(events)
    assert graph_update["status"] == "applied"
    assert service.history.state().nodes[_EXPERIMENT_ID].status == "completed"
    assert service.history.state().nodes[_EXPERIMENT_ID].next_action is None
    assert app.state.background_tasks.store.watchers(project_id) == []


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
    assert "unexpected process exit (including SIGTERM)" in normalized
    assert "Two similar failures do not prove an external cause" in normalized
    assert "plausibly transient failure is uncertainty, not a Blocker" in normalized
    assert (
        "failed or repeatedly terminated process without that diagnosis stays on path 1"
        in normalized
    )
    assert "do not wait or poll for detached work; finish this" in normalized
    assert "Merely observing that all jobs ended is not enough" in message
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
        episode_id=episode.episode_id,
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
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
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
    assert "rcp-agent-client-" in wake_contract
    assert " --credential " in wake_contract
    assert " --workspace " in wake_contract
    assert " validate " in wake_contract

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
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
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
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
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
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
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
    root_task = store.agent_task("loop-rejected")
    assert root_task is not None and root_task.authorized_by is not None
    owner = store.local_owner
    assert owner is not None
    store.rename_space_user(owner.user_id, "Repair researcher")

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
    applied_patch = service.history.load_patches()[-1]
    assert applied_patch.source_operation_id == "loop-graph-repair"
    assert applied_patch.task_id == "loop-graph-repair"
    assert applied_patch.authorized_by is not None
    assert applied_patch.authorized_by.display_name == "Repair researcher"
    assert root_task.authorized_by.display_name != applied_patch.authorized_by.display_name

    repaired_episode = store.experiment_episode(episode_id)
    assert repaired_episode is not None
    assert repaired_episode.last_turn_operation_id == "loop-graph-repair"
    assert repaired_episode.last_turn_invocation == 1
    assert repaired_episode.last_graph_result == (
        f"applied as revision {applied_graph['applied_revision']}"
    )
    assert repaired_episode.last_watcher_ids == rejected_episode.last_watcher_ids
    assert repaired_episode.context_baseline == rejected_episode.context_baseline


def test_experiment_apply_resolves_the_direct_child_binding_not_its_root(
    manifest,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    episode_id = "00000000-0000-4000-8000-000000000096"
    request = _loop_request(
        episode_id,
        "chat-direct-child-authority",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    root = _execution(store, project_id, "loop-authority-root", request)
    store.fail_agent_task(root.operation_id, "Interrupted before Apply.")
    child = _execution(
        store,
        project_id,
        "loop-authority-child",
        request,
        continuation="retry",
        parent_operation_id=root.operation_id,
    )
    stored_child = store.agent_task(child.operation_id)
    assert stored_child is not None and stored_child.dispatch_authority is not None
    real_resolver = service.history.agent_authority_resolver
    assert real_resolver is not None
    resolved_operations: list[str] = []

    def mismatched_child_resolver(project: str, operation_id: str):
        resolved_operations.append(operation_id)
        resolved = real_resolver(project, operation_id)
        if operation_id != child.operation_id:
            return resolved
        dispatch = resolved.dispatch_authority
        assert dispatch is not None
        mismatched_scope = dispatch.scope.model_copy(update={"run_truth_scope": ["repo-b"]})
        return resolved.model_copy(
            update={"dispatch_authority": dispatch.model_copy(update={"scope": mismatched_scope})}
        )

    service.history.agent_authority_resolver = mismatched_child_resolver
    revision_before_apply = service.history.state().revision
    result, failure = _apply_work_patch(
        service,
        child,
        json.dumps(
            {
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
        ),
        run_truth_scope=["repo-a"],
        patch_kind="experiment_loop",
        control_node_id=_EXPERIMENT_ID,
        control_decision_bundle=[],
    )

    assert result is None
    assert failure is not None and "run_truth_scope does not match" in failure.message
    assert resolved_operations == [child.operation_id]
    assert service.history.state().revision == revision_before_apply


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
    owner = store.local_owner
    assert owner is not None
    display_name = owner.display_name or "Test researcher"
    record = AgentTaskRecord(
        operation_id=operation_id,
        project_id=project_id,
        episode_id=episode_id,
        kind="node_chat",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="queued",
        authorized_by=AuthorizedHuman(
            space_id=store.space_id,
            user_id=owner.user_id,
            display_name=display_name,
        ),
    )
    store.create_experiment_episode_with_invocation(record)
    if status == "running":
        store.mark_agent_task_running(operation_id)
    elif status == "succeeded":
        store.complete_agent_task(operation_id, applied_revision=None, result={})
    else:
        raise AssertionError(f"Unsupported Experiment task fixture status: {status}")


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
                episode_id=episode_id,
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


def test_staged_graph_watcher_state_uses_condition_fields_without_shell_telemetry(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "graph-watcher-state.sqlite3")
    project_id = "project-graph-watcher-state"
    episode_id = "00000000-0000-4000-8000-000000000099"
    _store_task(
        store,
        operation_id="graph-watcher-root",
        project_id=project_id,
        episode_id=episode_id,
        status="running",
    )
    evaluated_at = "2026-08-12T00:00:00+00:00"
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
        control_completion_criteria=["The canonical blocker is resolved."],
    )
    store.create_watchers(
        [
            GraphWatcherRecord(
                watcher_id="graph-condition",
                project_id=project_id,
                origin_operation_id="graph-watcher-root",
                origin_task_kind="node_chat",
                chat_id="watcher-state-chat",
                node_id=_EXPERIMENT_ID,
                condition=NodeStatusGraphCondition(
                    node_id="blk/result",
                    status_in=["resolved"],
                ),
                armed_revision=2,
                continuation=continuation,
                status="active",
                created_at=evaluated_at,
                last_evaluated_at=evaluated_at,
            )
        ]
    )
    execution = AgentTaskExecution(
        operation_id="graph-watcher-root",
        store=store,
        control=AgentProcessControl(),
    )

    state = _watcher_state(
        execution,
        _EXPERIMENT_ID,
        [],
        episode_id,
        "initial_run",
    )

    assert len(state) == 1
    item = state[0]
    assert item["condition"] == {"node_id": "blk/result", "status_in": ["resolved"]}
    assert item["armed_revision"] == 2
    assert item["last_evaluated_at"] == evaluated_at
    assert not {
        "check_command",
        "log_path",
        "cwd",
        "last_checked_at",
        "last_exit_code",
        "last_error",
        "next_check_at",
        "consecutive_error_count",
        "group_id",
        "group_label",
    }.intersection(item)


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
    guessed.write_text('{"external":[],"graph":[]}', encoding="utf-8")

    frames, session_id, paused = await _process_experiment_watcher_maintenance(
        service=None,  # The unstaged path is rejected before graph-state access.
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
    parent = _execution(
        store,
        project_id,
        "maintenance-work",
        request,
    )
    store.fail_agent_task(parent.operation_id, "Interrupted before watcher maintenance.")
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
    survivor_text = '{"external":[{"stop_watcher_id":"w-1","reason":"Superseded"}],"graph":[]}'
    survivor.write_text(survivor_text, encoding="utf-8")
    predecessor_digest = hashlib.sha256(survivor_text.encode("utf-8")).hexdigest()

    frames, session_id, paused = await _process_experiment_watcher_maintenance(
        service=None,  # An unchanged Retry survivor is skipped before graph-state access.
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
    older_stopped = store.request_experiment_loop_stop(project_id, _EXPERIMENT_ID)
    assert older_stopped is not None and older_stopped.stop_settled_at is not None
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
