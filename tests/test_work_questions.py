from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from rcp.agents import AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.background import AgentTaskExecution
from rcp.core.models import AuthorizedHuman
from rcp.limits import AGENT_TASK_RECEIPT_LIST_LIMIT
from rcp.runs.questions import record_work_question_receipts, work_command_handler
from rcp.runs.tasks.work import _work_execution_instructions
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord, AppStore

from .test_ask_protocol import ask_request


def work_execution(tmp_path, *, mode="work"):
    store = AppStore(tmp_path / "app.sqlite3")
    request = RunRequest(
        chat_scope="project", chat_id="chat", message="Choose", mode=mode, provider="codex"
    )
    human = AuthorizedHuman(space_id=str(uuid4()), user_id=str(uuid4()), display_name="Human")
    record = AgentTaskRecord(
        operation_id="turn",
        project_id="project",
        kind="project_chat",
        status="running",
        request=request.model_dump(mode="json"),
        created_at=store.now(),
        updated_at=store.now(),
        status_message="Running",
        authorized_by=human,
        dispatch_authority=resolve_dispatch_authority("project_chat", request),
        native_session_id="native",
        stage_root="/stage",
        write_scope_fingerprint="a" * 64,
    )
    store.create_agent_task(record)
    return AgentTaskExecution(
        operation_id="turn", store=store, control=AgentProcessControl()
    ), human


def identity(authority="broker", task_id="turn"):
    return CommandTurnIdentity(
        episode_id=None, task_id=task_id, turn_id="turn", authority=authority
    )


def test_work_live_answer_receipt_requires_successful_settlement(tmp_path):
    execution, human = work_execution(tmp_path)
    handler = work_command_handler(execution, None)
    assert handler.allowed_verbs == {"validate", "ask"}
    request = ask_request()
    pending = handler(request, identity())
    assert pending.result["state"] == "pending"
    question_id = pending.result["question_id"]
    record = execution.store.get_question(question_id)
    assert record.origin.operation_id == execution.operation_id
    assert record.origin.native_session_id == "native"
    assert record.origin.capability == "work_auto"
    execution.store.answer_question(question_id, answer="Use A", resolved_by=human)
    answered = handler(request.model_copy(update={"request_id": "d" * 32}), identity())
    assert answered.result["answer"] == "Use A"
    wrong_receipt = handler(ask_request(request_id="e" * 32, receipt_token="0" * 64), identity())
    assert wrong_receipt.status == "invalid"
    assert not any(
        receipt.category == "question_answer_acknowledged"
        for receipt in execution.store.agent_task_receipts("turn")
    )
    acknowledged = handler(
        ask_request(request_id="f" * 32, receipt_token=answered.result["receipt_token"]),
        identity(),
    )
    assert acknowledged.status == "ok"
    record_work_question_receipts(execution)
    assert execution.store.get_question(question_id).client_receipt_revision is None
    execution.store.complete_agent_task("turn", applied_revision=None, result={})
    record_work_question_receipts(execution)
    record = execution.store.get_question(question_id)
    assert record.client_receipt_revision == 1
    assert record.client_receipt_request_id == "d" * 32
    assert record.followup_operation_id is None


def test_question_receipts_beyond_display_cap_are_replayed(tmp_path):
    execution, human = work_execution(tmp_path)
    store = execution.store
    handler = work_command_handler(execution, None)
    request = ask_request()
    question_id = handler(request, identity()).result["question_id"]
    store.answer_question(question_id, answer="Use A", resolved_by=human)
    for index in range(AGENT_TASK_RECEIPT_LIST_LIMIT + 1):
        store.record_agent_task_receipt(
            "turn", "compute_command_result", {"command_id": str(index)}, tier="diagnostic"
        )
    offered = handler(request, identity())
    assert offered.status == "ok"
    token = offered.result["receipt_token"]
    assert handler(request, identity()).result["receipt_token"] == token
    acknowledged = ask_request(request_id="f" * 32, receipt_token=token)
    assert handler(acknowledged, identity()).status == "ok"
    assert handler(acknowledged, identity()).status == "ok"
    assert len(store.agent_task_receipts("turn")) == AGENT_TASK_RECEIPT_LIST_LIMIT
    assert not any(
        receipt.category.startswith("question_answer_")
        for receipt in store.agent_task_receipts("turn")
    )
    assert len(store.agent_task_receipts_by_category("turn", "question_answer_offered")) == 1
    assert len(store.agent_task_receipts_by_category("turn", "question_answer_acknowledged")) == 1
    store.complete_agent_task("turn", applied_revision=None, result={})
    record_work_question_receipts(execution)
    assert store.get_question(question_id).client_receipt_revision == 1


def test_answer_not_returned_before_turn_end_remains_eligible(tmp_path):
    execution, human = work_execution(tmp_path)
    handler = work_command_handler(execution, None)
    pending = handler(ask_request(), identity())
    question_id = pending.result["question_id"]
    execution.store.answer_question(question_id, answer="Use A", resolved_by=human)
    execution.store.complete_agent_task("turn", applied_revision=None, result={})
    record_work_question_receipts(execution)
    assert execution.store.get_question(question_id).client_receipt_revision is None


@pytest.mark.parametrize("authority,task_id", [("validate_only", "turn"), ("broker", "other")])
def test_work_ask_requires_own_broker(tmp_path, authority, task_id):
    execution, _ = work_execution(tmp_path)
    assert (
        work_command_handler(execution, None)(ask_request(), identity(authority, task_id)).status
        == "invalid"
    )
    assert execution.store.list_questions() == []


def test_discuss_refuses_ask_and_work_master_uses_resolved_verbs(tmp_path):
    execution, _ = work_execution(tmp_path, mode="discuss")
    handler = work_command_handler(execution, None)
    assert handler.allowed_verbs == {"validate"}
    assert handler(ask_request(), identity()).status == "invalid"
    turn = SimpleNamespace(execution=execution, compute_commands=None)
    assert _work_execution_instructions(turn) == ""


def test_human_work_prompt_includes_ask_without_compute(tmp_path):
    execution, _ = work_execution(tmp_path)
    turn = SimpleNamespace(execution=execution, compute_commands=None)
    assert "ask --key" in _work_execution_instructions(turn)


@pytest.mark.parametrize("session_drift", [False, True])
def test_answer_followup_work_stream_preserves_origin_and_refuses_session_drift(
    manifest, tmp_path, session_drift
):
    import json

    from fastapi.testclient import TestClient

    from rcp.runs.tasks.work import stream_work_run
    from rcp.storage.question_models import QuestionOrigin
    from tests.helpers import (
        append_fixture_patch,
        create_named_app,
        seed_patch,
        wait_for_task_response,
    )
    from tests.test_chat_prompt_protocol import _RecordingLauncher

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    tasks = app.state.background_tasks
    store = tasks.store
    project_id = app.state.default_project_id
    launcher = _RecordingLauncher("origin-native-session")

    async def stream(_project_id, kind, request, execution):
        async for frame in stream_work_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    tasks.stream = stream
    client = TestClient(app)
    response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": str(uuid4()),
            "message": "Choose the next route.",
            "run_truth_scope": ["repo-a"],
            "mode": "work",
        },
    )
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    assert wait_for_task_response(client, project_id, operation_id)["status"] == "succeeded"
    origin = store.agent_task(operation_id)
    question = store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="chat",
            project_id=project_id,
            owner_id=origin.request["chat_id"],
            operation_id=origin.operation_id,
            provider=origin.request["provider"],
            native_session_id=origin.native_session_id,
            stage_root=origin.stage_root,
            stage_host=origin.stage_host,
            capability=origin.dispatch_authority.task_contract,
            write_scope_fingerprint=origin.write_scope_fingerprint,
            graph_target=origin.graph_target,
        ),
        key="route",
        question="Which route?",
    )
    store.answer_question(
        question.question_id, answer="Take route A", resolved_by=origin.authorized_by
    )
    if session_drift:
        launcher.native_session_id = "different-native-session"
    statuses = app.state.reconcile_question_answers(project_id)
    followup_id = store.get_question(question.question_id).followup_operation_id
    assert followup_id is not None, statuses
    result = wait_for_task_response(client, project_id, followup_id)
    followup = store.agent_task(followup_id)
    assert result["status"] == ("failed" if session_drift else "succeeded"), result
    assert launcher.sessions == [None, origin.native_session_id]
    assert followup.native_session_id == origin.native_session_id
    assert followup.write_scope_fingerprint == origin.write_scope_fingerprint
    assert followup.graph_target == origin.graph_target
    assert launcher.launch_kwargs[1]["capability"] == question.origin.capability
    snapshot_line = next(
        line for line in launcher.prompts[1].split("\n") if line.startswith('{"questions":')
    )
    snapshot = json.loads(snapshot_line)
    assert [(item["question_id"], item["answer"]) for item in snapshot["questions"]] == [
        (question.question_id, "Take route A")
    ]


def test_repeating_question_cannot_deliver_answer_to_changed_binding(tmp_path):
    execution, human = work_execution(tmp_path)
    handler = work_command_handler(execution, None)
    request = ask_request()
    question_id = handler(request, identity()).result["question_id"]
    execution.store.answer_question(question_id, answer="Use A", resolved_by=human)
    with execution.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET native_session_id='different-session' WHERE operation_id='turn'"
        )
    response = handler(request.model_copy(update={"request_id": "e" * 32}), identity())
    assert response.status == "invalid"
    assert response.result == {}
    assert execution.store.get_question(question_id).client_receipt_revision is None


def test_episode_stop_racing_question_creation_leaves_readonly_card(tmp_path, monkeypatch):
    from tests.test_experiment_questions import _ready_answer

    store, episode_id, _, _ = _ready_answer(tmp_path)
    store.checkpoint_agent_task(
        "loop-root", native_session_id="native-session", stage_root="/tmp/exact-experiment-stage"
    )
    execution = AgentTaskExecution(
        operation_id="loop-root", store=store, control=AgentProcessControl()
    )
    handler = work_command_handler(execution, None)
    create = store.create_or_get_question

    def stop_then_create(**kwargs):
        store.request_episode_stop(episode_id)
        return create(**kwargs)

    monkeypatch.setattr(store, "create_or_get_question", stop_then_create)
    response = handler(
        ask_request(),
        CommandTurnIdentity(
            episode_id=episode_id, task_id="loop-root", turn_id="one", authority="broker"
        ),
    )
    assert response.status == "invalid"
    assert all(q.withdrawn_readonly for q in store.list_questions(owner_id=episode_id))
