from __future__ import annotations

from typing import cast

import pytest

from rcp.background import AgentTaskContinuation
from rcp.runs.chat import _prepare_local_chat_workspace
from rcp.runs.tasks.work_turn_runtime import clears_stale_turn_handoffs


@pytest.mark.parametrize(
    "continuation",
    [
        "fresh",
        "handoff",
        "watcher_wake",
        "graph_condition_wake",
        "message_wake",
        "lifecycle_wake",
    ],
)
def test_new_logical_work_turns_clear_previous_handoffs(
    continuation: AgentTaskContinuation,
) -> None:
    assert clears_stale_turn_handoffs(continuation) is True


@pytest.mark.parametrize("continuation", ["resume", "retry", "graph_repair"])
def test_same_logical_work_turn_continuations_preserve_handoffs(
    continuation: AgentTaskContinuation,
) -> None:
    assert clears_stale_turn_handoffs(continuation) is False


@pytest.mark.parametrize("continuation", ["auto_research_continuation", "episode_report"])
def test_work_rejects_continuations_without_an_explicit_handoff_policy(
    continuation: str,
) -> None:
    with pytest.raises(ValueError, match="Unsupported Work continuation"):
        clears_stale_turn_handoffs(cast(AgentTaskContinuation, continuation))


def test_local_work_workspace_keeps_staged_inputs_outside_the_write_root(tmp_path) -> None:
    stage = tmp_path / "run-stage"
    inputs = stage / "inputs"
    inputs.mkdir(parents=True)
    contract = inputs / "task-contract.md"
    contract.write_text("immutable contract", encoding="utf-8")

    workspace = _prepare_local_chat_workspace(stage, execution=None, saved_stage=False)

    assert workspace == stage / "workspace"
    assert workspace.is_dir()
    assert not (workspace / "inputs").exists()
    assert contract.read_text(encoding="utf-8") == "immutable contract"


@pytest.mark.parametrize("owner", ["work", "experiment_loop"])
def test_resumed_mailbox_preserves_validation_policy_without_opening_graph(
    owner, monkeypatch, tmp_path
) -> None:
    from rcp.runs.patch_validator import PatchValidationResult
    from rcp.runs.tasks import experiment_loop, work

    from .test_work_questions import work_execution

    module = work if owner == "work" else experiment_loop
    saved = {"run_truth_scope": ["rq-launch"]}
    if owner == "experiment_loop":
        saved.update(control_node_id="experiment-launch", control_decision_bundle=[])

    execution, _ = work_execution(tmp_path)
    monkeypatch.setattr(module, "load_work_mailbox_context", lambda _: saved)
    restored = {}
    monkeypatch.setattr(
        module,
        "restore_work_validator_mailbox",
        lambda _execution, **kwargs: restored.update(kwargs),
    )
    monkeypatch.setattr(module, "_work_patch_source_operation_id", lambda _: "accepted-turn")
    service = object()
    opened = []

    def open_service():
        opened.append(True)
        return service

    validated = []

    def validate(actual_service, text, **policy):
        validated.append((actual_service, text, policy))
        return PatchValidationResult(status="valid")

    monkeypatch.setattr(module, "_validate_work_patch_live", validate)
    resume = (
        work.resume_work_command_mailbox
        if owner == "work"
        else experiment_loop.resume_experiment_command_mailbox
    )
    resume(open_service, execution)
    assert opened == []
    assert restored["command_handler"].allowed_verbs == {"validate", "lesson"}
    assert restored["validate"]("candidate").status == "valid"
    assert opened == [True]
    expected = {"run_truth_scope": ["rq-launch"], "source_operation_id": "accepted-turn"}
    if owner == "experiment_loop":
        expected.update(control_node_id="experiment-launch", control_decision_bundle=[])
    assert validated == [(service, "candidate", expected)]


def test_resumed_compute_mailbox_keeps_the_launch_scope(manifest, tmp_path) -> None:
    from rcp.agents.launcher import AgentProcessControl
    from rcp.agents.write_scope import ProjectWriteScope
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.compute_commands import WorkComputeCommands
    from rcp.runs.tasks.work import (
        _resume_work_compute_commands,
        _work_mailbox_context,
        _WorkMailboxContext,
    )
    from rcp.storage import AgentTaskRecord, AppStore
    from rcp.transport import RemoteRunStage

    store = AppStore(tmp_path / "private-data" / "rcp.sqlite3")
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="accepted-turn",
            project_id="project",
            kind="project_chat",
            status="running",
            request={},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Running",
        )
    )
    scope = ProjectWriteScope.create(
        project_id="project",
        execution_machine="laptop",
        execution_host="remote",
        capability="work_auto",
        stage_root="/stage",
        workspace_root="/stage/workspace",
        repositories=[],
        protected_write_paths=[],
    )
    execution = AgentTaskExecution(
        "accepted-turn",
        store,
        AgentProcessControl(),
        stage_host="remote",
        stage_root="/stage",
        write_scope_fingerprint=scope.fingerprint,
    )
    original = WorkComputeCommands(execution, manifest, scope, RemoteRunStage("remote"), "episode")
    saved = _work_mailbox_context(["rq-launch"], original).model_dump(mode="json")
    restored = _resume_work_compute_commands(execution, _WorkMailboxContext.model_validate(saved))
    assert restored is not None
    assert restored.write_scope == scope
    assert restored.episode_id == "episode"
    assert str(restored.remote_stage.workspace) == "/stage/workspace"
    assert restored.manifest.model_dump() == manifest.model_dump()
    execution.write_scope_fingerprint = "changed"
    with pytest.raises(ValueError, match="scope"):
        _resume_work_compute_commands(execution, _WorkMailboxContext.model_validate(saved))


@pytest.mark.parametrize("owner", ["work", "experiment_loop"])
@pytest.mark.parametrize("ask_allowed,mode", [(True, "work"), (True, "discuss"), (False, "work")])
def test_resumed_mailbox_retains_ask_without_compute_and_rechecks_authority(
    tmp_path, monkeypatch, owner, ask_allowed, mode
):
    from rcp.runs.tasks import experiment_loop, work

    from .test_work_questions import work_execution

    execution, _ = work_execution(tmp_path)
    module = work if owner == "work" else experiment_loop
    policy = {} if owner == "work" else {"control_node_id": "exp", "control_decision_bundle": []}
    saved = {}
    monkeypatch.setattr(
        module, "start_work_validator_mailbox", lambda _staged, **kwargs: saved.update(kwargs)
    )
    module._start_work_validator_mailbox(
        None, None, execution=execution, budget=None, run_truth_scope=[], **policy
    )
    assert "ask" in saved["command_handler"].allowed_verbs
    context_model = (
        work._WorkMailboxContext if owner == "work" else experiment_loop._ExperimentMailboxContext
    )
    context = context_model.model_validate(saved["resume_context"])
    assert context.ask_allowed
    context.ask_allowed = ask_allowed
    if mode == "discuss":
        from rcp.service import RunRequest, resolve_dispatch_authority

        authority = resolve_dispatch_authority(
            "project_chat",
            RunRequest(chat_scope="project", chat_id="chat", mode="discuss", message="Discuss"),
        )
        with execution.store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET dispatch_authority_json=? WHERE operation_id='turn'",
                (authority.model_dump_json(),),
            )
    restored = work._resume_work_command_handler(execution, context, lambda: None)
    if ask_allowed and mode == "work":
        assert restored is not None and "ask" in restored.allowed_verbs
    elif mode == "work":
        assert restored is not None and restored.allowed_verbs == {"validate", "lesson"}
    else:
        assert restored is None
