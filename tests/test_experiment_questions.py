from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.core.transition_models import GraphTargetRef
from rcp.runs.experiment_questions import reconcile_experiment_question_answers
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AppStore, EpisodeInvocationCeilingReached
from rcp.storage.question_models import QuestionOrigin

from .test_experiment_episode_storage import _admit_root, _bind, _identity, _task
from .test_experiment_loop_recorded_finalization import (
    _ANSWER,
    _EPISODE_ID,
    _finalize,
    _recorded_pass,
    _retained_loop_turn,
)


def _question(store, episode_id, operation_id="loop-root"):
    task = store.agent_task(operation_id)
    assert task is not None
    return store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="episode",
            project_id=task.project_id,
            owner_id=episode_id,
            operation_id=operation_id,
            provider="codex",
            native_session_id="native-session",
            stage_root="/tmp/exact-experiment-stage",
            capability="work_auto",
            write_scope_fingerprint="a" * 64,
            graph_target=GraphTargetRef(),
        ),
        key="metric",
        question="Which metric?",
    )


@pytest.mark.asyncio
async def test_question_parks_empty_experiment_handoff_without_graph_pause(manifest, tmp_path):
    service, request, execution, _ = await _retained_loop_turn(manifest, tmp_path)
    _question(execution.store, _EPISODE_ID, execution.operation_id)
    events = await _finalize(
        service,
        request,
        execution,
        _recorded_pass(
            json.dumps({"summary": "Waiting for human input", "ops": []}),
            _ANSWER,
            json.dumps({"external": [], "graph": []}),
        ),
    )
    assert not [item for item in events if item["event"] == "error"]
    assert execution.store.episode(_EPISODE_ID).ending is None
    assert execution.store.experiment_has_question_continuation(
        execution.store.agent_task(execution.operation_id).project_id, _EPISODE_ID
    )


def _ready_answer(tmp_path: Path, *, ceiling=2):
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id, root = _admit_root(store, ceiling=ceiling)
    _bind(store, episode_id, root.operation_id, invocation=1)
    authority = resolve_dispatch_authority("node_chat", RunRequest.model_validate(root.request))
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET write_scope_fingerprint=?, dispatch_authority_json=? "
            "WHERE operation_id=?",
            ("a" * 64, authority.model_dump_json(), root.operation_id),
        )
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    question = _question(store, episode_id)
    store.answer_question(question.question_id, answer="Accuracy", resolved_by=_identity(store))
    record = _task(
        store,
        "answer-wake",
        episode_id,
        invocation=2,
        ceiling=ceiling,
        trigger="watcher",
        session_id="native-session",
        stage_root="/tmp/exact-experiment-stage",
    ).model_copy(update={"dispatch_authority": authority})
    return store, episode_id, question, record


def test_answer_wake_claims_once_and_spends_experiment_budget(tmp_path):
    store, episode_id, question, record = _ready_answer(tmp_path)
    admitted = store.create_experiment_question_invocation(
        record, question_id=question.question_id, answer_revision=1
    )
    assert admitted is not None
    assert admitted.native_session_id == question.origin.native_session_id
    assert admitted.episode_id == episode_id
    assert store.episode(episode_id).invocations_used == 2
    assert store.get_question(question.question_id).followup_operation_id == record.operation_id
    assert (
        store.create_experiment_question_invocation(
            record.model_copy(update={"operation_id": "duplicate"}),
            question_id=question.question_id,
            answer_revision=1,
        )
        is None
    )
    assert store.episode(episode_id).invocations_used == 2


def test_answer_wake_requires_reauthorization_at_ceiling(tmp_path):
    store, episode_id, question, record = _ready_answer(tmp_path, ceiling=1)
    with pytest.raises(EpisodeInvocationCeilingReached):
        store.create_experiment_question_invocation(
            record, question_id=question.question_id, answer_revision=1
        )
    assert store.episode(episode_id).invocations_used == 1
    assert store.get_question(question.question_id).followup_operation_id is None


def test_episode_end_withdraws_questions(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id, _ = _admit_root(store)
    question = _question(store, episode_id)
    store.request_episode_stop(episode_id)
    assert store.get_question(question.question_id).withdrawn_readonly


def test_continuation_reopens_predecessor_question_without_moving_origin(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id, _ = _admit_root(store)
    question = _question(store, episode_id)
    store.complete_agent_task("loop-root", applied_revision=None, result={})
    store.end_episode_without_report(episode_id, ending="exhausted")
    assert store.get_question(question.question_id).withdrawn_readonly
    continued_id = str(uuid.uuid4())
    store.create_experiment_episode_with_invocation(
        _task(store, "continued-root", continued_id),
        continues_episode_id=episode_id,
        continuation_request_id=str(uuid.uuid4()),
    )
    reopened = store.get_question(question.question_id)
    assert not reopened.withdrawn_readonly
    assert reopened.origin == question.origin
    assert store.experiment_question_owner_ids(continued_id) == [continued_id, episode_id]
    store.request_episode_stop(continued_id)
    assert store.get_question(question.question_id).withdrawn_readonly


def test_reconcile_restarts_claimed_queued_answer_without_spending_twice(tmp_path):
    store, episode_id, question, _ = _ready_answer(tmp_path)
    launches = []
    tasks = SimpleNamespace(store=store, launch_admitted=launches.append)
    first = reconcile_experiment_question_answers(tasks)
    claimed = store.get_question(question.question_id).followup_operation_id
    assert claimed is not None and first[question.question_id] == claimed
    restarted = SimpleNamespace(
        store=AppStore(tmp_path / "rcp.sqlite3"), launch_admitted=launches.append
    )
    assert reconcile_experiment_question_answers(restarted)[question.question_id] == claimed
    assert launches == [claimed, claimed]
    assert store.episode(episode_id).invocations_used == 2


def test_experiment_answer_cannot_change_origin_scope(tmp_path):
    store, _, question, record = _ready_answer(tmp_path)
    changed = record.model_copy(
        update={"request": {**record.request, "run_truth_scope": ["repo-b"]}}
    )
    with pytest.raises(ValueError):
        store.create_experiment_question_invocation(
            changed, question_id=question.question_id, answer_revision=1
        )
    assert store.get_question(question.question_id).followup_operation_id is None


def test_human_experiment_serves_ask_and_continuation_reuses_question_key(tmp_path):
    from rcp.agents import AgentProcessControl
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.background import AgentTaskExecution
    from rcp.runs.questions import work_command_handler

    from .test_ask_protocol import ask_request

    store, episode_id, _, _ = _ready_answer(tmp_path)
    store.checkpoint_agent_task(
        "loop-root", native_session_id="native-session", stage_root="/tmp/exact-experiment-stage"
    )
    execution = AgentTaskExecution(
        operation_id="loop-root", store=store, control=AgentProcessControl()
    )
    handler = work_command_handler(execution, None)
    identity = CommandTurnIdentity(
        episode_id=episode_id, task_id="loop-root", turn_id="one", authority="broker"
    )
    assert "ask" in handler.allowed_verbs
    first = handler(ask_request(), identity)
    assert first.status == "ok" and first.result["state"] == "pending"
    store.end_episode_without_report(episode_id, ending="exhausted")
    continued_id = str(uuid.uuid4())
    continued = _task(store, "continued-root", continued_id)
    authority = resolve_dispatch_authority(
        "node_chat", RunRequest.model_validate(continued.request)
    )
    store.create_experiment_episode_with_invocation(
        continued.model_copy(update={"dispatch_authority": authority}),
        continues_episode_id=episode_id,
        continuation_request_id=str(uuid.uuid4()),
    )
    store.checkpoint_agent_task(
        "continued-root",
        native_session_id="native-session",
        stage_root="/tmp/exact-experiment-stage",
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET write_scope_fingerprint=? WHERE operation_id=?",
            ("a" * 64, "continued-root"),
        )
    continuation = AgentTaskExecution(
        operation_id="continued-root", store=store, control=AgentProcessControl()
    )
    repeated = work_command_handler(continuation, None)(
        ask_request(),
        CommandTurnIdentity(
            episode_id=continued_id, task_id="continued-root", turn_id="two", authority="broker"
        ),
    )
    assert repeated.status == "ok"
    assert repeated.result["question_id"] == first.result["question_id"]
    assert store.get_question(repeated.result["question_id"]).origin.owner_id == episode_id
    question_id = first.result["question_id"]
    store.answer_question(question_id, answer="Accuracy", resolved_by=_identity(store))
    answered_request = ask_request(request_id="e" * 32)
    answered = work_command_handler(continuation, None)(
        answered_request,
        CommandTurnIdentity(
            episode_id=continued_id, task_id="continued-root", turn_id="two", authority="broker"
        ),
    )
    assert answered.result["state"] == "answered"
    assert not store.record_question_continuation_receipt(
        question_id,
        answer_revision=1,
        operation_id="continued-root",
        request_id=answered_request.request_id,
    )
    acknowledged = work_command_handler(continuation, None)(
        ask_request(request_id="f" * 32, receipt_token=answered.result["receipt_token"]),
        CommandTurnIdentity(
            episode_id=continued_id, task_id="continued-root", turn_id="two", authority="broker"
        ),
    )
    assert acknowledged.status == "ok"
    store.complete_agent_task("continued-root", applied_revision=None, result={})
    assert not store.record_question_continuation_receipt(
        question_id, answer_revision=1, operation_id="continued-root", request_id="not-offered"
    )
    restarted_store = AppStore(tmp_path / "rcp.sqlite3")
    assert restarted_store.question_receipt_candidate_operations(question_id) == ["continued-root"]
    assert question_id not in reconcile_experiment_question_answers(
        SimpleNamespace(store=restarted_store, launch_admitted=lambda _operation: None)
    )
    received = store.get_question(question_id)
    assert received.client_receipt_operation_id == "continued-root"
    assert received.client_receipt_revision == 1
    assert received.origin.owner_id == episode_id
    assert question_id not in reconcile_experiment_question_answers(
        SimpleNamespace(store=store, launch_admitted=lambda _operation: None)
    )


def test_orchestrator_child_experiment_refuses_ask(tmp_path):
    from rcp.agents import AgentProcessControl
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.background import AgentTaskExecution
    from rcp.runs.questions import work_command_handler

    from .test_ask_protocol import ask_request
    from .test_auto_research_children_storage import (
        _experiment_route,
        _experiment_task,
        _setup_parent,
    )

    store, parent, root = _setup_parent(tmp_path)
    episode_id = str(uuid.uuid4())
    task = _experiment_task(store, episode_id, parent.authorized_by, node_id="exp-child")
    task = task.model_copy(
        update={
            "dispatch_authority": resolve_dispatch_authority(
                "node_chat", RunRequest.model_validate(task.request)
            )
        }
    )
    store.create_experiment_episode_with_invocation(
        task, auto_research_route=_experiment_route(store, parent, root, task)
    )
    execution = AgentTaskExecution(
        operation_id=task.operation_id, store=store, control=AgentProcessControl()
    )
    handler = work_command_handler(execution, None)
    assert "ask" not in handler.allowed_verbs
    assert (
        handler(
            ask_request(),
            CommandTurnIdentity(
                episode_id=episode_id, task_id=task.operation_id, turn_id="one", authority="broker"
            ),
        ).status
        == "invalid"
    )
    assert store.list_questions() == []


@pytest.mark.parametrize("acknowledged", [False, True])
def test_only_acknowledged_answer_removes_experiment_continuation(tmp_path, acknowledged):
    store, episode_id, question, _ = _ready_answer(tmp_path)
    assert store.experiment_has_question_continuation(
        "project", episode_id, operation_id="loop-root"
    )
    store.record_agent_task_receipt(
        "loop-root",
        "question_answer_acknowledged" if acknowledged else "question_answer_offered",
        {
            "question_id": question.question_id,
            "answer_revision": 1,
            "request_id": "answered",
        },
    )
    assert store.experiment_has_question_continuation(
        "project", episode_id, operation_id="loop-root"
    ) is (not acknowledged)
    assert store.get_question(question.question_id).client_receipt_revision is None
