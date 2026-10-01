from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AppStore
from rcp.storage.question_models import QuestionOrigin
from tests.test_auto_research_children_storage import _identity, _project
from tests.test_watchers import _completed_chat_turn, _task


def _answered(store):
    _project(store)
    record = _task(store, "origin", []).model_copy(
        update={
            "request": {
                **_task(store, "origin", []).request,
                "trigger": "human",
            }
        }
    )
    record = record.model_copy(
        update={
            "authorized_by": _identity(store),
            "dispatch_authority": resolve_dispatch_authority(
                record.kind, RunRequest.model_validate(record.request)
            ),
        }
    )
    store.create_agent_task(record)
    store.mark_agent_task_running("origin")
    store.checkpoint_agent_task(
        "origin",
        native_session_id="origin-session",
        stage_host="",
        stage_root=str(store.path.parent / "chat-stage"),
    )
    store.complete_agent_task("origin", applied_revision=None, result={})
    task = store.agent_task("origin")
    store.bind_agent_task_write_scope(
        task.operation_id,
        project_id=task.project_id,
        stage_host=task.stage_host or "",
        stage_root=task.stage_root,
        fingerprint="a" * 64,
        continuation_binding=False,
    )
    question = store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="chat",
            project_id="project",
            owner_id="chat",
            operation_id="origin",
            provider="codex",
            native_session_id="origin-session",
            stage_root=task.stage_root,
            stage_host=task.stage_host,
            capability=task.dispatch_authority.task_contract,
            write_scope_fingerprint="a" * 64,
            graph_target=task.graph_target,
        ),
        key="choice",
        question="Which route?",
    )
    return store.answer_question(
        question.question_id, answer="Use route A", resolved_by=_identity(store)
    )


def test_answer_uses_origin_binding_despite_new_chat_settings(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    latest = _completed_chat_turn(
        store,
        "latest",
        "different-session",
        request_updates={
            "provider": "claude",
            "model": "different-model",
            "run_truth_scope": [],
        },
    )
    task = store.admit_chat_question_followup(question.question_id)
    assert task.native_session_id == "origin-session" != latest.native_session_id
    assert task.request["provider"] == "codex"
    assert task.request["run_truth_scope"] == ["state"]
    assert task.request["mode"] == "work"
    assert task.request["message"] == "Use route A"
    assert task.graph_target == question.origin.graph_target
    assert task.stage_root == question.origin.stage_root
    assert task.dispatch_authority == store.agent_task("origin").dispatch_authority
    assert store.agent_task_continuation_cause(task.operation_id) == "message_wake"
    assert store.get_question(question.question_id).followup_operation_id == task.operation_id
    from rcp.background import BackgroundAgentTasks

    launches = []
    engine = BackgroundAgentTasks(store, lambda *args: None)
    monkeypatch.setattr(engine, "admit_provider_task", lambda *args, **kwargs: None)

    def capture(record, request, **kwargs):
        launches.append((record, request, kwargs))
        return record

    monkeypatch.setattr(engine, "_spawn_record", capture)
    engine.launch_admitted(task.operation_id)
    launched, request, binding = launches[0]
    assert request.session_id == question.origin.native_session_id
    assert request.provider == question.origin.provider
    assert request.run_truth_scope == ["state"]
    assert launched.graph_target == question.origin.graph_target
    assert launched.dispatch_authority.task_contract == question.origin.capability
    assert binding["continuation"] == "message_wake"


def test_answer_claim_is_atomic_under_duplicate_and_restart_admission(tmp_path):
    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    reopened = AppStore(store.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(
            pool.map(
                lambda _: reopened.admit_chat_question_followup(question.question_id), range(2)
            )
        )
    admitted = [task for task in tasks if task is not None]
    assert len(admitted) == 1
    assert AppStore(store.path).admit_chat_question_followup(question.question_id) is None


def test_answer_waits_for_settlement_and_receipt_prevents_followup(tmp_path):
    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='running' WHERE operation_id='origin'")
    with pytest.raises(ValueError, match="question_origin_unsettled"):
        store.admit_chat_question_followup(question.question_id)
    assert store.get_question(question.question_id).followup_operation_id is None
    store.record_question_receipt(
        question.question_id, answer_revision=1, operation_id="origin", request_id="next"
    )
    assert store.admit_chat_question_followup(question.question_id) is None


def test_failed_insert_rolls_back_answer_claim(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)

    def refuse(*args, **kwargs):
        raise ValueError("admission_refused")

    monkeypatch.setattr(store, "_insert_agent_task", refuse)
    with pytest.raises(ValueError, match="admission_refused"):
        store.admit_chat_question_followup(question.question_id)
    assert store.get_question(question.question_id).followup_operation_id is None


def test_projection_retries_once_and_followup_exchange_does_not_duplicate_answer(
    manifest, tmp_path
):
    import json
    from types import SimpleNamespace
    from uuid import uuid4

    from rcp.runs.chat import _append_chat_exchange, project_chat_question_answer
    from tests.helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "app-data")
    service = app.state.service
    store = AppStore(tmp_path / "question-store.sqlite3")
    question = _answered(store)
    chat_id = str(uuid4())
    task = store.agent_task("origin")
    request = {**task.request, "chat_id": chat_id, "chat_scope": "project", "node_id": None}
    origin = question.origin.model_copy(update={"owner_id": chat_id})
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='project_chat',request_json=? WHERE operation_id='origin'",
            (json.dumps(request),),
        )
        connection.execute(
            "UPDATE questions SET owner_id=?,origin_json=? WHERE question_id=?",
            (chat_id, origin.model_dump_json(), question.question_id),
        )
    question = store.get_question(question.question_id)
    project_chat_question_answer(service, store, question)
    project_chat_question_answer(service, AppStore(store.path), question)
    followup = store.admit_chat_question_followup(question.question_id)
    _append_chat_exchange(
        service,
        RunRequest.model_validate(followup.request),
        "Understood",
        "origin-session",
        None,
        execution=SimpleNamespace(store=store, operation_id=followup.operation_id),
    )
    transcript = service.chat_transcript(chat_id)
    assert [(item.role, item.text) for item in transcript.messages] == [
        ("user", "Use route A"),
        ("assistant", "Understood"),
    ]


@pytest.mark.parametrize("state", ["occupied", "paused", "remote_unresolved"])
def test_followup_defers_without_claim_while_original_binding_is_held(tmp_path, state):
    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    if state == "remote_unresolved":
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET stage_host='worker' WHERE operation_id='origin'"
            )
            origin = question.origin.model_copy(update={"stage_host": "worker"})
            connection.execute(
                "UPDATE questions SET origin_json=? WHERE question_id=?",
                (origin.model_dump_json(), question.question_id),
            )
        store.begin_remote_provider_pass(
            "origin",
            "worker",
            question.origin.stage_root,
            question.origin.stage_root + "/provider.pid",
        )
    else:
        task = _task(store, "held", []).model_copy(
            update={"request": {**_task(store, "held", []).request, "trigger": "human"}}
        )
        store.create_agent_task(task)
        if state == "paused":
            with store.connection() as connection:
                connection.execute(
                    "UPDATE graph_runs SET status='paused',native_session_id='origin-session' WHERE operation_id='held'"
                )
    with pytest.raises(ValueError):
        store.admit_chat_question_followup(question.question_id)
    assert store.get_question(question.question_id).followup_operation_id is None


def test_restart_reconciliation_recovers_successful_receipt_before_admitting(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from rcp.runs.chat import reconcile_chat_question_answers

    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    store.record_agent_task_receipt(
        "origin",
        "question_answer_offered",
        {
            "question_id": question.question_id,
            "answer_revision": question.answer_revision,
            "request_id": "answered-response",
        },
    )
    projections = []
    monkeypatch.setattr(
        "rcp.runs.chat.project_chat_question_answer",
        lambda service, store, question: projections.append(question.question_id),
    )

    def unexpected_launch(operation_id):
        pytest.fail(f"Receipt recovery should not launch {operation_id}")

    statuses = reconcile_chat_question_answers(
        AppStore(store.path),
        SimpleNamespace(launch_admitted=unexpected_launch),
        lambda _: SimpleNamespace(for_graph_target=lambda target: None),
    )
    assert statuses == {question.question_id: "received"}
    assert projections == [question.question_id]
    assert store.get_question(question.question_id).client_receipt_revision == 1
    assert store.get_question(question.question_id).followup_operation_id is None


def test_restart_preserves_claimed_question_task_before_first_dispatch(tmp_path, monkeypatch):
    from rcp.background import BackgroundAgentTasks

    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    admitted = store.admit_chat_question_followup(question.question_id)
    engine = BackgroundAgentTasks(AppStore(store.path), lambda *args: None)
    monkeypatch.setattr(engine, "_schedule_remote_reconciliation", lambda **kwargs: None)
    engine.recover_at_startup()
    assert engine.store.agent_task(admitted.operation_id).status == "queued"
    assert (
        engine.store.get_question(question.question_id).followup_operation_id
        == admitted.operation_id
    )


def test_recovery_retains_answer_origin_before_scope_is_bound(tmp_path):
    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    admitted = store.admit_chat_question_followup(question.question_id)
    store.fail_agent_task(admitted.operation_id, "Provider stopped before scope binding.")
    recovery = admitted.model_copy(
        update={
            "operation_id": "answer-recovery",
            "parent_operation_id": admitted.operation_id,
            "attempt": 2,
            "status": "queued",
            "error": None,
            "finished_at": None,
        }
    )
    store.create_agent_task(recovery, continuation_cause="retry")
    assert store.agent_task(recovery.operation_id).write_scope_fingerprint is None
    inherited = store.question_for_followup(recovery.operation_id)
    assert inherited.question_id == question.question_id
    assert inherited.origin == question.origin
