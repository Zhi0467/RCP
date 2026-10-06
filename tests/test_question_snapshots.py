from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import QUESTION_SNAPSHOT_MAX_RECORDS
from rcp.runs.question_snapshots import (
    question_snapshot,
    question_snapshot_part,
    record_question_snapshot_sent,
)
from rcp.storage import AppStore, QuestionOrigin


@pytest.fixture
def store(tmp_path: Path) -> AppStore:
    return AppStore(tmp_path / "app.sqlite3")


@pytest.fixture
def origin() -> QuestionOrigin:
    return QuestionOrigin(
        owner_kind="chat",
        project_id="project",
        owner_id="chat",
        operation_id="asking-turn",
        provider="codex",
        native_session_id="session",
        stage_root="/stage",
        capability="work_auto",
        write_scope_fingerprint="scope",
        graph_target=GraphTargetRef(),
    )


@pytest.fixture
def human() -> AuthorizedHuman:
    return AuthorizedHuman(
        space_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), display_name="Human"
    )


def snapshot(store, operation_id="followup", **kwargs):
    return question_snapshot(
        store,
        project_id="project",
        owner_kind="chat",
        owner_ids=["chat"],
        operation_id=operation_id,
        **kwargs,
    )


def test_snapshot_reads_owner_state_and_scopes_claimed_answers(store, origin, human):
    pending = store.create_or_get_question(origin=origin, key="pending", question="Pick?")
    dismissed = store.create_or_get_question(origin=origin, key="dismissed", question="Still?")
    store.dismiss_question(dismissed.question_id, resolved_by=human)
    answered = store.create_or_get_question(origin=origin, key="answer", question="Which?")
    answered = store.answer_question(answered.question_id, answer="A", resolved_by=human)
    assert answered.question_id not in {
        item["question_id"] for item in json.loads(snapshot(store, "unrelated").text)["questions"]
    }
    assert answered.question_id in {
        item["question_id"]
        for item in json.loads(snapshot(store, origin.operation_id).text)["questions"]
    }
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert store.claim_question_followup(
            connection, answered.question_id, answer_revision=1, operation_id="followup"
        )
    store.create_or_get_question(
        origin=origin.model_copy(update={"owner_id": "another-chat"}),
        key="other",
        question="Excluded?",
    )
    result = snapshot(store)
    entries = {item["question_id"]: item for item in json.loads(result.text)["questions"]}
    assert set(entries) == {pending.question_id, dismissed.question_id, answered.question_id}
    assert entries[answered.question_id]["answer"] == "A"
    assert entries[answered.question_id]["answer_revision"] == 1
    assert result.dismissal_ids == (dismissed.question_id,)
    assert answered.question_id not in {
        item["question_id"] for item in json.loads(snapshot(store, "other").text)["questions"]
    }


def test_dismissal_remains_until_recorded_delivery_and_retries_on_receipt_failure(
    store, origin, human, monkeypatch
):
    item = store.create_or_get_question(origin=origin, key="dismissed", question="Still?")
    store.dismiss_question(item.question_id, resolved_by=human)
    result = snapshot(store)
    assert store.get_question(item.question_id).dismissal_delivered_at is None

    def fail(*args, **kwargs):
        raise OSError("receipt unavailable")

    monkeypatch.setattr(store, "record_agent_task_receipt", fail)
    with pytest.raises(OSError):
        record_question_snapshot_sent(store, result, operation_id="followup")
    assert store.get_question(item.question_id).dismissal_delivered_at is None
    receipts = []
    monkeypatch.setattr(store, "record_agent_task_receipt", lambda *args: receipts.append(args))
    record_question_snapshot_sent(store, result, operation_id="followup")
    assert receipts[0][1] == "question_snapshot_sent"
    assert receipts[0][2]["dismissal_ids"] == [item.question_id]
    assert snapshot(store).dismissal_ids == ()
    assert json.loads(snapshot(store).text)["questions"] == []


def test_snapshot_is_bounded_and_large_content_uses_existing_staging(store, origin, tmp_path):
    for index in range(QUESTION_SNAPSHOT_MAX_RECORDS + 1):
        store.create_or_get_question(origin=origin, key=str(index), question="Q" * 400)
    result = snapshot(store)
    data = json.loads(result.text)
    assert len(data["questions"]) == QUESTION_SNAPSHOT_MAX_RECORDS
    assert data["omitted"] == 1
    part = question_snapshot_part(result, local_stage=tmp_path, remote_stage=None)
    staged = list((tmp_path / "inputs").glob("questions-*.json"))
    assert len(staged) == 1
    assert str(staged[0]) in part
    assert staged[0].read_text() == result.text
    assert question_snapshot_part(result, local_stage=tmp_path, remote_stage=None) == part


def test_received_answers_and_withdrawn_questions_are_not_redelivered(store, origin, human):
    item = store.create_or_get_question(origin=origin, key="answered", question="Which?")
    store.answer_question(item.question_id, answer="A", resolved_by=human)
    store.record_question_receipt(
        item.question_id,
        answer_revision=1,
        operation_id=origin.operation_id,
        request_id="request-2",
    )
    assert json.loads(snapshot(store).text)["questions"] == []
    episode_origin = origin.model_copy(update={"owner_kind": "episode", "owner_id": "episode"})
    item = store.create_or_get_question(origin=episode_origin, key="pending", question="Pick?")
    store.set_episode_questions_withdrawn("project", "episode", withdrawn=True)
    result = question_snapshot(
        store, project_id="project", owner_kind="episode", owner_ids=["episode"], operation_id="new"
    )
    assert json.loads(result.text)["questions"] == []
    store.set_episode_questions_withdrawn("project", "episode", withdrawn=False)
    result = question_snapshot(
        store, project_id="project", owner_kind="episode", owner_ids=["episode"], operation_id="new"
    )
    assert json.loads(result.text)["questions"][0]["question_id"] == item.question_id


def test_work_followup_launch_has_fresh_snapshot_and_records_delivery(
    store, origin, human, tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from rcp.runs import questions
    from rcp.runs.tasks import work_turn_runtime as runtime

    item = store.create_or_get_question(origin=origin, key="dismiss", question="Continue?")
    store.dismiss_question(item.question_id, resolved_by=human)
    monkeypatch.setattr(questions, "work_ask_authorized", lambda execution: True)
    monkeypatch.setattr(
        store,
        "agent_task",
        lambda operation: SimpleNamespace(
            project_id="project", episode_id=None, parent_operation_id=None
        ),
    )
    receipts = []
    monkeypatch.setattr(store, "record_agent_task_receipt", lambda *args: receipts.append(args))
    execution = SimpleNamespace(store=store, operation_id="followup")
    turn = SimpleNamespace(
        execution=execution,
        request=SimpleNamespace(chat_id="chat"),
        local_stage=tmp_path,
        remote_stage=None,
        workspace=tmp_path,
        read_dirs=[],
        write_dirs=[],
        write_scope=SimpleNamespace(fingerprint="scope"),
        execution_host="",
        provider_binary=None,
        patch_inputs=SimpleNamespace(validator_staged="mailbox"),
        validator_lifecycle="lifecycle",
        question_snapshot=None,
        hidden_read_scope=None,
        service=None,
        data_dir=tmp_path,
    )
    snapshot_part = runtime.prepare_work_question_snapshot(turn)
    prompts = []
    outcome = SimpleNamespace(completed=False)

    async def stream(launcher, request, prompt, **kwargs):
        prompts.append(prompt)
        assert store.get_question(item.question_id).dismissal_delivered_at is None
        yield "provider frame"
        outcome.completed = True

    monkeypatch.setattr(runtime, "stream_work_agent_events", stream)

    async def run():
        return [
            frame
            async for frame in runtime.stream_turn_agent_events(
                turn, None, "base prompt\n" + snapshot_part, session_id="session", outcome=outcome
            )
        ]

    assert asyncio.run(run()) == ["provider frame"]
    assert item.question_id in prompts[0]
    assert turn.question_snapshot.dismissal_ids == (item.question_id,)
    assert receipts[0][1] == "question_snapshot_sent"
    assert store.get_question(item.question_id).dismissal_delivered_at is not None


@pytest.mark.parametrize("mode", ["work", "discuss"])
@pytest.mark.parametrize("changed", [None, "session", "scope", "provider", "target", "capability"])
def test_recovery_snapshot_carries_answers_only_through_identical_binding(
    store, origin, human, monkeypatch, changed, mode
):
    from types import SimpleNamespace

    if mode == "discuss":
        origin = origin.model_copy(
            update={"capability": "discuss", "write_scope_fingerprint": None}
        )
    item = store.create_or_get_question(origin=origin, key="answer", question="Which?")
    store.answer_question(item.question_id, answer="A", resolved_by=human)
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert store.claim_question_followup(
            connection, item.question_id, answer_revision=1, operation_id="followup"
        )
    binding = dict(
        project_id="project",
        kind="project_chat",
        native_session_id="session",
        request={"provider": "codex", "mode": mode},
        graph_target=GraphTargetRef(),
        write_scope_fingerprint="scope" if mode == "work" else None,
        stage_root="/stage",
        stage_host=None,
    )
    parent = SimpleNamespace(operation_id="followup", parent_operation_id=None, **binding)
    current = SimpleNamespace(operation_id="recovery", parent_operation_id="followup", **binding)
    if changed == "session":
        current.native_session_id = "other"
    elif changed == "scope":
        current.write_scope_fingerprint = "other"
    elif changed == "provider":
        current.request = {"provider": "claude", "mode": mode}
    elif changed == "capability":
        current.request = {"provider": "codex", "mode": "discuss" if mode == "work" else "work"}
    elif changed == "target":
        current.graph_target = GraphTargetRef(kind="branch", branch_id="other")
    records = {"recovery": current, "followup": parent}
    monkeypatch.setattr(store, "agent_task", records.get)
    monkeypatch.setattr(store, "agent_task_continuation_cause", lambda operation: "resume")
    resolved_scope = current.write_scope_fingerprint
    current.write_scope_fingerprint = None  # Launch admission has not bound it yet.
    result = snapshot(store, "recovery", write_scope_fingerprint=resolved_scope)
    ids = {item["question_id"] for item in json.loads(result.text)["questions"]}
    assert (item.question_id in ids) == (changed is None)
