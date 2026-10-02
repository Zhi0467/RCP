from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, call

import pytest
from fastapi.testclient import TestClient

from rcp.storage.question_models import QuestionOrigin

from .helpers import create_named_app
from .test_episode_storage import _episode


@pytest.fixture
def questions_api(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    app.state.reconcile_question_answers = Mock()
    return app, TestClient(app), app.state.background_tasks.store, app.state.default_project_id


def _question(store, project_id, *, owner_kind="chat", owner_id="chat", key="ask"):
    return store.create_or_get_question(
        origin=QuestionOrigin(
            project_id=project_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            operation_id="origin",
            provider="codex",
            native_session_id="session",
            stage_root="/stage",
            capability="work_auto",
            write_scope_fingerprint="scope",
            graph_target={"kind": "main"},
        ),
        key=key,
        question="Choose a dataset",
        choices=["a", "b"],
    )


def test_list_answer_retry_conflict_and_dismiss(questions_api):
    app, client, store, project_id = questions_api
    question = _question(store, project_id)
    base = f"/api/projects/{project_id}"
    listed = client.get(f"{base}/chats/chat/questions")
    assert listed.status_code == 200
    assert [(q["question_id"], q["can_answer"], q["state"]) for q in listed.json()] == [
        (question.question_id, True, "parked")
    ]
    url = f"{base}/questions/{question.question_id}/answer"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json={"choices": ["a"]}), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert app.state.reconcile_question_answers.call_args_list == [call(project_id)] * 2
    answer = responses[0].json()
    assert answer["chosen_choices"] == ["a"]
    assert answer["resolved_by"]["user_id"] == store.local_owner.user_id
    assert answer["resolved_at"] and not answer["can_answer"]
    assert client.post(url, json={"answer": "different"}).status_code == 409
    assert store.get_question(question.question_id).origin == question.origin
    dismissed = _question(store, project_id, key="dismiss")
    dismiss_url = f"{base}/questions/{dismissed.question_id}/dismiss"
    assert client.post(dismiss_url, json={}).json()["state"] == "dismissed"
    assert client.post(dismiss_url, json={}).status_code == 200
    assert app.state.reconcile_question_answers.call_args_list == [call(project_id)] * 2


@pytest.mark.parametrize("field", ["capability", "scope", "target", "mode", "resolved_by"])
def test_answer_cannot_supply_authority(questions_api, field):
    app, client, store, project_id = questions_api
    question = _question(store, project_id)
    response = client.post(
        f"/api/projects/{project_id}/questions/{question.question_id}/answer",
        json={"answer": "ok", field: "changed"},
    )
    assert response.status_code == 422
    assert store.get_question(question.question_id) == question
    app.state.reconcile_question_answers.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"choices": ["other"]}, {"choices": ["a", "b"]}])
def test_store_validates_answer(questions_api, body):
    app, client, store, project_id = questions_api
    question = _question(store, project_id)
    response = client.post(
        f"/api/projects/{project_id}/questions/{question.question_id}/answer", json=body
    )
    assert response.status_code == 422
    app.state.reconcile_question_answers.assert_not_called()


def test_episode_continuation_and_withdrawal(questions_api):
    app, client, store, project_id = questions_api
    store.create_episode(_episode(store, "first", project_id=project_id))
    question = _question(store, project_id, owner_kind="episode", owner_id="first")
    store.end_episode_without_report("first", ending="exhausted", diagnostic=None)
    base = f"/api/projects/{project_id}"
    card = client.get(f"{base}/episodes/first/questions").json()[0]
    assert card["withdrawn_readonly"] and not card["can_answer"]
    for action, body in [("answer", {"answer": "a"}), ("dismiss", {})]:
        assert (
            client.post(f"{base}/questions/{question.question_id}/{action}", json=body).status_code
            == 409
        )
    store.create_episode(
        _episode(store, "next", project_id=project_id).model_copy(
            update={"continues_episode_id": "first"}
        )
    )
    cards = client.get(f"{base}/episodes/next/questions").json()
    assert len(cards) == 1 and cards[0]["question_id"] == question.question_id
    assert cards[0]["owner_id"] == "first" and cards[0]["can_answer"]
    assert (
        client.post(
            f"{base}/questions/{question.question_id}/answer", json={"answer": "a"}
        ).status_code
        == 200
    )
    app.state.reconcile_question_answers.assert_called_once_with(project_id)


def test_nonmember_and_cross_project_refused(questions_api, monkeypatch):
    _, client, store, project_id = questions_api
    question = _question(store, project_id)
    other = _question(store, "other-project")
    base = f"/api/projects/{project_id}"
    assert (
        client.post(
            f"{base}/questions/{other.question_id}/answer", json={"answer": "a"}
        ).status_code
        == 404
    )
    monkeypatch.setattr(store, "is_project_member", lambda *args: False)
    for path in ["chats/chat/questions", "episodes/unknown/questions"]:
        assert client.get(f"{base}/{path}").status_code == 404
    for action in ["answer", "dismiss"]:
        assert (
            client.post(f"{base}/questions/{question.question_id}/{action}", json={}).status_code
            == 404
        )


def test_orchestrator_answer_retry_records_and_delivers_mail(questions_api, monkeypatch):
    from types import SimpleNamespace

    from rcp.api import questions

    app, client, store, project_id = questions_api
    store.create_episode(_episode(store, "auto", project_id=project_id, mode="auto_research"))
    question = _question(store, project_id, owner_kind="episode", owner_id="auto")
    calls = []

    def record(candidate_store, question_id):
        assert candidate_store.get_question(question_id).state == "answered"
        calls.append(("record", question_id))
        return SimpleNamespace(episode_id="auto", recipient_task_id="root")

    monkeypatch.setattr(questions, "record_auto_research_question_answer", record)
    monkeypatch.setattr(
        questions,
        "deliver_pending_auto_research_mail",
        lambda *args, **kwargs: calls.append(("deliver", kwargs)),
    )
    for _ in range(2):
        assert (
            client.post(
                f"/api/projects/{project_id}/questions/{question.question_id}/answer",
                json={"answer": "a"},
            ).status_code
            == 200
        )
    expected_delivery = [
        ("record", question.question_id),
        ("deliver", {"episode_id": "auto", "recipient_task_id": "root"}),
    ]
    assert calls == expected_delivery * 2
    app.state.reconcile_question_answers.assert_not_called()


def test_delivery_failure_retains_answer_and_exact_retry_delivers(questions_api, caplog):
    app, client, store, project_id = questions_api
    question = _question(store, project_id)
    app.state.reconcile_question_answers.side_effect = [OSError("delivery unavailable"), None]
    url = f"/api/projects/{project_id}/questions/{question.question_id}/answer"
    first = client.post(url, json={"answer": "a"})
    assert first.status_code == 200
    assert store.get_question(question.question_id).state == "answered"
    assert any(
        record.name == "rcp.api.questions" and record.levelname == "ERROR" and record.exc_info
        for record in caplog.records
    )
    assert client.post(url, json={"answer": "a"}).json() == first.json()
    assert app.state.reconcile_question_answers.call_args_list == [call(project_id)] * 2
    assert store.get_question(question.question_id).answer_revision == 1


def test_answer_launch_keeps_origin_binding(questions_api, monkeypatch):
    from .test_chat_question_followup import _answered

    app, client, store, project_id = questions_api
    template = _answered(store)
    # The fixture's fully bound Work task belongs to its synthetic project;
    # attach that server record to this API fixture's registered project.
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET project_id=? WHERE operation_id='origin'", (project_id,)
        )
    question = store.create_or_get_question(
        origin=template.origin.model_copy(update={"project_id": project_id}),
        key="api-answer",
        question="Choose a dataset",
    )
    background = app.state.background_tasks
    launches = []
    monkeypatch.setattr(background, "admit_provider_task", lambda *args, **kwargs: None)

    def capture(record, request, **kwargs):
        launches.append((record, request, kwargs))
        return record

    monkeypatch.setattr(background, "_spawn_record", capture)

    def reconcile(_project_id):
        admitted = store.admit_chat_question_followup(question.question_id)
        if admitted is not None:
            background.launch_admitted(admitted.operation_id)

    app.state.reconcile_question_answers = reconcile
    url = f"/api/projects/{project_id}/questions/{question.question_id}/answer"
    assert client.post(url, json={"answer": "a", "mode": "discuss"}).status_code == 422
    assert launches == []
    assert client.post(url, json={"answer": "a"}).status_code == 200
    assert len(launches) == 1
    launched, request, binding = launches[0]
    origin = question.origin
    assert launched.dispatch_authority.task_contract == origin.capability
    assert launched.graph_target == origin.graph_target
    # Scope is bound at provider launch, after admission. The durable stage
    # binding refuses any different scope for this continuation.
    with pytest.raises(ValueError):
        store.bind_agent_task_write_scope(
            launched.operation_id,
            project_id=project_id,
            stage_host=origin.stage_host or "",
            stage_root=origin.stage_root,
            fingerprint="b" * 64,
            continuation_binding=True,
        )
    store.bind_agent_task_write_scope(
        launched.operation_id,
        project_id=project_id,
        stage_host=origin.stage_host or "",
        stage_root=origin.stage_root,
        fingerprint=origin.write_scope_fingerprint,
        continuation_binding=True,
    )
    assert (
        store.agent_task(launched.operation_id).write_scope_fingerprint
        == origin.write_scope_fingerprint
    )
    assert launched.native_session_id == request.session_id == origin.native_session_id
    assert launched.stage_root == origin.stage_root
    assert request.provider == origin.provider
    assert request.mode == "work"
    assert request.run_truth_scope == store.agent_task("origin").request["run_truth_scope"]
    assert binding["continuation"] == "message_wake"
    assert client.post(url, json={"answer": "a"}).status_code == 200
    assert len(launches) == 1
