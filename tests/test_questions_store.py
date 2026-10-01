from __future__ import annotations

import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import ASK_ANSWER_MAX_LENGTH
from rcp.storage import (
    AppStore,
    QuestionArgumentConflict,
    QuestionOrigin,
    QuestionStateConflict,
)


@pytest.fixture
def store(tmp_path: Path) -> AppStore:
    return AppStore(tmp_path / "app.sqlite3")


@pytest.fixture
def origin() -> QuestionOrigin:
    return QuestionOrigin(
        owner_kind="chat",
        project_id="project",
        owner_id="chat",
        operation_id="turn",
        provider="codex",
        native_session_id="session",
        stage_root="/stage",
        capability="work",
        write_scope_fingerprint="scope-hash",
        graph_target=GraphTargetRef(kind="branch", branch_id="branch"),
    )


@pytest.fixture
def human() -> AuthorizedHuman:
    return AuthorizedHuman(
        space_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), display_name="Human"
    )


def test_create_or_get_survives_restart_and_keeps_first_binding(store, origin) -> None:
    first = store.create_or_get_question(
        origin=origin, key="pick", question="Which?", choices=["A"]
    )
    later = origin.model_copy(update={"operation_id": "later-turn", "capability": "discuss"})
    reopened = AppStore(store.path)
    assert (
        reopened.create_or_get_question(origin=later, key="pick", question="Which?", choices=["A"])
        == first
    )
    assert reopened.get_question(first.question_id) == first
    with pytest.raises(ValidationError):
        first.origin.capability = "discuss"


def test_create_or_get_rejects_changed_arguments_and_scopes_keys_by_owner(store, origin) -> None:
    first = store.create_or_get_question(origin=origin, key="pick", question="Which?")
    with pytest.raises(QuestionArgumentConflict):
        store.create_or_get_question(origin=origin, key="pick", question="Changed?")
    other = store.create_or_get_question(
        origin=origin.model_copy(update={"owner_id": "other"}), key="pick", question="Changed?"
    )
    assert other.question_id != first.question_id
    assert store.list_questions(owner_kind="chat", owner_id="chat", open_only=True) == [first]


def test_answer_is_terminal_and_preserves_origin(store, origin, human) -> None:
    first = store.create_or_get_question(
        origin=origin, key="pick", question="Which?", choices=["A"]
    )
    answered = store.answer_question(
        first.question_id, answer="Use this", choices=["A"], resolved_by=human
    )
    assert answered.state == "answered"
    assert answered.answer_revision == 1
    assert answered.answer == "Use this"
    assert answered.chosen_choices == ["A"]
    assert answered.resolved_by == human
    assert answered.resolved_at is not None
    assert answered.origin == origin
    assert (
        store.answer_question(
            first.question_id, answer="Use this", choices=["A"], resolved_by=human
        )
        == answered
    )
    with pytest.raises(QuestionStateConflict):
        store.dismiss_question(first.question_id, resolved_by=human)
    with pytest.raises(QuestionStateConflict):
        store.answer_question(first.question_id, answer="replace", resolved_by=human)
    assert store.list_questions(open_only=True) == []


@pytest.mark.parametrize(
    "answer,choices",
    [
        ("x" * (ASK_ANSWER_MAX_LENGTH + 1), []),
        ("", []),
        ("ok", ["unknown"]),
        ("ok", ["A", "B"]),
        ("ok", ["A", "A"]),
    ],
)
def test_answer_validation(store, origin, human, answer, choices) -> None:
    question = store.create_or_get_question(
        origin=origin, key="pick", question="Which?", choices=["A", "B"]
    )
    with pytest.raises(ValueError):
        store.answer_question(
            question.question_id, answer=answer, choices=choices, resolved_by=human
        )
    assert store.get_question(question.question_id).state == "pending"


def test_multiple_accepts_choices_and_free_text(store, origin, human) -> None:
    question = store.create_or_get_question(
        origin=origin, key="pick", question="Which?", choices=["A", "B"], multiple=True
    )
    assert store.answer_question(
        question.question_id, answer="", choices=["A", "B"], resolved_by=human
    ).chosen_choices == ["A", "B"]
    text = store.create_or_get_question(origin=origin, key="text", question="Which?", choices=["A"])
    assert (
        store.answer_question(text.question_id, answer="another option", resolved_by=human).answer
        == "another option"
    )


def test_dismissal_is_terminal_and_delivery_is_separate(store, origin, human) -> None:
    question = store.create_or_get_question(origin=origin, key="pick", question="Which?")
    dismissed = store.dismiss_question(question.question_id, resolved_by=human)
    assert dismissed.state == "dismissed"
    assert dismissed.dismissal_delivered_at is None
    assert store.dismiss_question(question.question_id, resolved_by=human) == dismissed
    with pytest.raises(QuestionStateConflict):
        store.answer_question(question.question_id, answer="late", resolved_by=human)
    assert store.mark_question_dismissal_delivered(question.question_id)
    assert not store.mark_question_dismissal_delivered(question.question_id)
    assert store.get_question(question.question_id).dismissal_delivered_at is not None


def test_receipt_matches_original_turn_and_answer_revision(store, origin, human) -> None:
    question = store.create_or_get_question(origin=origin, key="pick", question="Which?")
    answered = store.answer_question(question.question_id, answer="yes", resolved_by=human)
    for revision, operation in [(0, "turn"), (1, "other")]:
        assert not store.record_question_receipt(
            question.question_id,
            answer_revision=revision,
            operation_id=operation,
            request_id="request",
        )
    assert store.record_question_receipt(
        question.question_id,
        answer_revision=answered.answer_revision,
        operation_id="turn",
        request_id="request",
    )
    receipt = store.get_question(question.question_id)
    assert receipt.client_receipt_revision == 1
    assert receipt.client_receipt_operation_id == "turn"
    assert receipt.client_receipt_request_id == "request"
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert not store.claim_question_followup(
            connection, question.question_id, answer_revision=1, operation_id="followup"
        )


def test_followup_claim_is_atomic_with_task_insert_and_has_one_winner(store, origin, human) -> None:
    question = store.create_or_get_question(origin=origin, key="pick", question="Which?")
    store.answer_question(question.question_id, answer="yes", resolved_by=human)
    with pytest.raises(RuntimeError), store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert store.claim_question_followup(
            connection, question.question_id, answer_revision=1, operation_id="rolled-back"
        )
        raise RuntimeError("task insertion failed")
    assert store.get_question(question.question_id).followup_operation_id is None

    def claim(index: int) -> bool:
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return store.claim_question_followup(
                connection,
                question.question_id,
                answer_revision=1,
                operation_id=f"followup-{index}",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(claim, range(2))) == [False, True]
    assert store.get_question(question.question_id).followup_claimed_at is not None
    assert not store.record_question_receipt(
        question.question_id, answer_revision=1, operation_id="turn", request_id="late"
    )
    with store.connection() as connection, pytest.raises(ValueError):
        store.claim_question_followup(
            connection, question.question_id, answer_revision=1, operation_id="outside-transaction"
        )


def test_episode_withdrawal_and_continuation_preserve_provenance(store, origin, human) -> None:
    origin = origin.model_copy(update={"owner_kind": "episode", "owner_id": "episode"})
    question = store.create_or_get_question(origin=origin, key="pick", question="Which?")
    assert store.set_episode_questions_withdrawn("project", "episode", withdrawn=True) == 1
    assert store.list_questions(open_only=True) == []
    with pytest.raises(QuestionStateConflict):
        store.answer_question(question.question_id, answer="yes", resolved_by=human)
    store.set_episode_questions_withdrawn("project", "episode", withdrawn=False)
    assert store.list_questions(owner_id="episode", open_only=True) == [question]
    assert (
        store.answer_question(question.question_id, answer="yes", resolved_by=human).origin
        == origin
    )


def test_migration_upgrades_existing_database_and_preserves_existing_rows(store) -> None:
    with sqlite3.connect(store.path) as connection:
        identity = connection.execute("SELECT * FROM space_identity").fetchall()
        connection.execute("DROP TABLE questions")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=33")
    migrated = AppStore(store.path)
    assert migrated.storage_schema_ledger_head() == 33
    assert migrated.list_questions() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT * FROM space_identity").fetchall() == identity
