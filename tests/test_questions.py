from __future__ import annotations

from uuid import uuid4

import pytest

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.questions import handle_ask
from rcp.storage import AppStore
from rcp.storage.question_models import QuestionOrigin

from .test_ask_protocol import ask_request


@pytest.mark.parametrize("resolution", ["pending", "parked", "answered", "dismissed"])
def test_ask_helper_returns_durable_state_without_claiming_delivery(tmp_path, resolution):
    store = AppStore(tmp_path / "app.sqlite3")
    origin = QuestionOrigin(
        owner_kind="chat",
        project_id="project",
        owner_id="chat",
        operation_id="turn",
        provider="codex",
        native_session_id="session",
        stage_root="/stage",
        capability="work_auto",
        write_scope_fingerprint="scope",
        graph_target=GraphTargetRef(),
    )
    request = ask_request(arguments={"question": "Which path?", "choices": ["a", "b"]})
    pending = handle_ask(store, request, origin)
    question_id = pending.result["question_id"]
    assert pending.status == "ok"
    assert pending.result["state"] == "pending"
    human = AuthorizedHuman(space_id=str(uuid4()), user_id=str(uuid4()), display_name="Researcher")
    if resolution == "answered":
        store.answer_question(question_id, answer="Use a", choices=["a"], resolved_by=human)
    elif resolution == "dismissed":
        store.dismiss_question(question_id, resolved_by=human)
    response = handle_ask(store, request, origin, parked=resolution != "pending")
    assert response.status == "ok"
    assert response.result["state"] == resolution
    assert response.result["question_id"] == question_id
    if resolution == "answered":
        assert response.result["answer"] == "Use a"
        assert response.result["choices"] == ["a"]
    record = store.get_question(question_id)
    assert record.client_receipt_revision is None
    assert record.followup_operation_id is None
    changed = ask_request(arguments={"question": "Different question"})
    assert handle_ask(store, changed, origin).status == "invalid"
    assert len(store.list_questions()) == 1
