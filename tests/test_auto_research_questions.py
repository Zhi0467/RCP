from __future__ import annotations

import json
import uuid

from rcp.agents import AgentEvent
from rcp.agents.command_protocol import AskCommandRequest
from rcp.background import BackgroundAgentTasks
from rcp.runs.auto_research import AutoResearchCommandDispatcher
from rcp.runs.auto_research_delivery import deliver_pending_auto_research_mail
from rcp.runs.auto_research_questions import (
    auto_research_question_snapshot,
    mark_auto_research_question_snapshot_delivered,
    record_auto_research_question_answer,
)
from rcp.storage.question_models import QuestionOrigin

from .helpers import wait_for_task
from .test_auto_research_commands import _Effects, _request, _setup_auto_research
from .test_auto_research_delivery import _sse, _start_auto_research, _store


def _question(store, episode, root, *, key="question"):
    return store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="episode",
            project_id=root.project_id,
            owner_id=episode.episode_id,
            operation_id=root.operation_id,
            provider="codex",
            native_session_id="session",
            stage_root="/tmp/stage",
            capability="orchestrate",
            write_scope_fingerprint="a" * 64,
            graph_target=root.graph_target,
        ),
        key=key,
        question="Which dataset?",
        choices=["A", "B"],
    )


def test_orchestrator_ask_parks_with_bound_origin(tmp_path, monkeypatch):
    store, episode, root = _setup_auto_research(tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.provider','codex'),native_session_id=?,stage_root=?,write_scope_fingerprint=? WHERE operation_id=?",
            ("session", "/tmp/stage", "a" * 64, root.operation_id),
        )
    dispatcher = AutoResearchCommandDispatcher(store, _Effects(store, episode, root).bundle())
    request = _request(
        AskCommandRequest,
        verb="ask",
        request_id=uuid.uuid4().hex,
        idempotency_key="dataset",
        arguments={"question": "Which dataset?"},
    )
    response = dispatcher.dispatch(root.operation_id, request)
    assert response.status == "ok", response
    assert response.result["state"] == "parked"
    question = store.get_question(response.result["question_id"])
    assert question.origin.owner_id == episode.episode_id
    assert question.origin.operation_id == root.operation_id
    assert question.origin.native_session_id == "session"
    assert question.origin.write_scope_fingerprint == "a" * 64
    assert question.origin.graph_target == root.graph_target
    repeated = dispatcher.dispatch(
        root.operation_id, request.model_copy(update={"request_id": uuid.uuid4().hex})
    )
    assert repeated.result == response.result
    store.answer_question(
        question.question_id, answer="问" * 16000, resolved_by=episode.authorized_by
    )
    repeated = dispatcher.dispatch(
        root.operation_id, request.model_copy(update={"request_id": uuid.uuid4().hex})
    )
    assert repeated.result["state"] == "answered"
    assert repeated.result["answer"] == "问" * 16000
    create_question = store.create_or_get_question

    def ending_before_insert(**arguments):
        store.request_episode_stop(episode.episode_id)
        return create_question(**arguments)

    monkeypatch.setattr(store, "create_or_get_question", ending_before_insert)
    raced = request.model_copy(update={"request_id": uuid.uuid4().hex, "idempotency_key": "raced"})
    assert dispatcher.dispatch(root.operation_id, raced).status == "unavailable"
    raced_question = next(
        question
        for question in store.episode_questions("project", episode.episode_id)
        if question.key == "raced"
    )
    assert raced_question.withdrawn_readonly
    assert dispatcher.dispatch(root.operation_id, request).status == "unavailable"


def test_answer_is_one_mail_one_paid_wake_and_dismissal_waits_for_next_wake(tmp_path):
    store = _store(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(AgentEvent(event="session", session_id=request.session_id or "session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = _start_auto_research(tasks)
    answered = _question(store, episode, root)
    dismissed = _question(store, episode, root, key="dismissed")
    store.dismiss_question(dismissed.question_id, resolved_by=episode.authorized_by)
    assert record_auto_research_question_answer(store, dismissed.question_id) is None
    assert store.auto_research_messages(episode.episode_id) == []
    assert (
        deliver_pending_auto_research_mail(
            tasks, episode_id=episode.episode_id, recipient_task_id=root.operation_id
        )
        is None
    )
    store.answer_question(
        answered.question_id, answer="A" * 16000, resolved_by=episode.authorized_by
    )
    message = record_auto_research_question_answer(store, answered.question_id)
    assert record_auto_research_question_answer(store, answered.question_id) == message
    assert message.sender_role == "human"
    assert json.loads(message.body)["question_id"] == answered.question_id
    assert json.loads(message.body)["answer"] == "A" * 16000
    before = store.episode_budget_meter(episode.episode_id).invocations_used
    wake_id = deliver_pending_auto_research_mail(
        tasks, episode_id=episode.episode_id, recipient_task_id=root.operation_id
    )
    assert wake_id is not None
    wake = wait_for_task(store, wake_id, expect="succeeded")
    assert wake.request["wake_cause"] == "message"
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before + 1
    text, dismissal_ids = auto_research_question_snapshot(
        store, project_id=root.project_id, episode_id=episode.episode_id, operation_id=wake_id
    )
    records = {record["question_id"]: record for record in json.loads(text)["questions"]}
    assert records[answered.question_id]["answer"] == "A" * 16000
    assert records[dismissed.question_id]["state"] == "dismissed"
    assert dismissal_ids == [dismissed.question_id]
    store.record_agent_task_receipt(wake_id, "question_snapshot", {"dismissal_ids": dismissal_ids})
    # Preparing a launch never acknowledges its dismissal; a failed launch can retry.
    assert store.get_question(dismissed.question_id).dismissal_delivered_at is None
    mark_auto_research_question_snapshot_delivered(store, wake_id)
    assert store.get_question(dismissed.question_id).dismissal_delivered_at is not None
    assert (
        auto_research_question_snapshot(
            store, project_id=root.project_id, episode_id=episode.episode_id, operation_id="later"
        )[1]
        == []
    )
    record_auto_research_question_answer(store, answered.question_id)
    assert len(store.auto_research_messages(episode.episode_id)) == 1
    assert (
        deliver_pending_auto_research_mail(
            tasks, episode_id=episode.episode_id, recipient_task_id=root.operation_id
        )
        is None
    )
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before + 1


def test_orchestrator_launch_stages_fresh_store_snapshot_and_retains_dismissals(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from rcp.runs.tasks.auto_research_stream import _orchestrator_prompt

    from .test_auto_research_stream import _execution
    from .test_prompts import _work_write_scope

    store, episode, root = _setup_auto_research(tmp_path)
    question = _question(store, episode, root)
    stage = tmp_path / "prompt-stage"
    stage.mkdir()
    context = SimpleNamespace(
        project_name="Example",
        repositories=[],
        graph_path="/state/graph.json",
        research_md_path="/state/research.md",
        ontology_extensions=False,
    )
    turn = SimpleNamespace(
        task=root,
        request=SimpleNamespace(instruction=None),
        binding=SimpleNamespace(native_session_id=None, stage_root=None, stage_host=None),
        allocation_operation_id=root.operation_id,
    )
    arguments = dict(
        context=context,
        local_stage=stage,
        remote_stage=None,
        token=root.operation_id,
        patch_path="/stage/patch.json",
        schema_path="/stage/schema.json",
        command_client="rcp-client",
        messages_path=None,
        lifecycle_path=None,
        skill_pointers=[],
        write_scope=_work_write_scope(),
    )
    _, prompt, _, values = _orchestrator_prompt(_execution(store, root), turn, **arguments)
    receipt = [
        r for r in store.agent_task_receipts(root.operation_id) if r.category == "question_snapshot"
    ][-1]
    first_path = receipt.payload["path"]
    assert first_path in prompt
    assert (
        json.loads(Path(first_path).read_text())["questions"][0]["question_id"]
        == question.question_id
    )
    assert "ask" in values["allowed_verbs"]
    store.dismiss_question(question.question_id, resolved_by=episode.authorized_by)
    turn.binding.native_session_id = "session"
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET stage_root=?,native_session_id=? WHERE operation_id=?",
            (str(stage), "session", root.operation_id),
        )
    arguments["token"] = "resumed-turn"
    # A later launch gets fresh data, not the prior turn's snapshot.
    _, prompt, _, _ = _orchestrator_prompt(
        _execution(store, root, continuation="resume"), turn, **arguments
    )
    receipt = [
        r for r in store.agent_task_receipts(root.operation_id) if r.category == "question_snapshot"
    ][-1]
    assert receipt.payload["path"] != first_path
    assert receipt.payload["path"] in prompt
    assert receipt.payload["dismissal_ids"] == [question.question_id]
    assert store.get_question(question.question_id).dismissal_delivered_at is None
