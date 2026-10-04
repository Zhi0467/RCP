from __future__ import annotations

import pytest

from rcp.agents import AgentProcessControl, staged_command_client
from rcp.agents.command_protocol import LessonArguments, LessonCommandRequest
from rcp.background import AgentTaskExecution
from rcp.runs.questions import work_command_handler

from .test_lessons_store import store  # noqa: F401
from .test_work_questions import identity


def request(action, *, key=None, **arguments):
    return LessonCommandRequest(
        mailbox_id="a" * 32,
        request_id="b" * 32,
        credential="c" * 64,
        idempotency_key=key,
        verb="lesson",
        arguments=LessonArguments(action=action, **arguments),
    )


def test_work_lesson_authority_and_keyed_outcome(store):  # noqa: F811
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    handler = work_command_handler(execution, None)
    add = request("add", key="once", text="Use uv")
    assert handler(add, identity("validate_only")).status == "invalid"
    assert handler(add, identity(task_id="other")).status == "invalid"
    first = handler(add, identity())
    assert first.status == "ok"
    assert handler(request("add", key="once", text="different"), identity()).result == {
        "code": "lesson_key_conflict"
    }
    lesson_id = first.result["lesson"]["lesson_id"]
    response = handler(request("delete", key="edit", lesson_id=lesson_id), identity())
    assert response.status == "invalid"
    assert response.result["code"] == "lesson_edit_not_authorized"
    assert len(store.list_lessons("project")) == 1


def test_lesson_client_parser_preserves_subcommand_arguments(tmp_path):
    prefix = ["--broker", "broker", "--timeout", "1", "--workspace", str(tmp_path)]
    for argv, expected in (
        (["add", "--key", "k", "--text", "Use uv"], ("k", {"action": "add", "text": "Use uv"})),
        (["list", "--cursor", "id"], (None, {"action": "list", "cursor": "id"})),
        (
            ["update", "--key", "k", "--id", "id", "--text", "Use uv"],
            ("k", {"action": "update", "lesson_id": "id", "text": "Use uv"}),
        ),
        (["delete", "--key", "k", "--id", "id"], ("k", {"action": "delete", "lesson_id": "id"})),
    ):
        parsed = staged_command_client._parser().parse_args([*prefix, "lesson", *argv])
        verb, key, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
        assert verb == "lesson"
        assert (key, arguments) == expected
        assert request(arguments.pop("action"), key=key, **arguments).verb == "lesson"


@pytest.mark.asyncio
async def test_validation_only_mailbox_refuses_lesson_before_owner_dispatch(tmp_path):
    from rcp.agents.command_mailbox import _read_request, stage_command_mailbox

    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=None,
        task_id="turn",
        turn_id="turn",
        authority="validate_only",
    )
    call = request("add", key="k", text="Not admitted").model_copy(
        update={"mailbox_id": staged.credential.mailbox_id, "credential": staged.credential.token}
    )
    filename = "request.json"
    (tmp_path / filename).write_text(call.model_dump_json(), encoding="utf-8")
    staged.credential.activate()
    try:
        with pytest.raises(ValueError, match="^lesson requires broker authority$"):
            await _read_request(filename, call.request_id, staged)
    finally:
        staged.cleanup()


@pytest.mark.parametrize("role", ["orchestrator", "worker"])
def test_auto_research_owners_add_lessons_with_their_operation_author(tmp_path, role):
    from rcp.runs.auto_research import AutoResearchCommandDispatcher

    from .test_auto_research_commands import _Effects, _setup_auto_research, _worker

    lesson_store, episode, root = _setup_auto_research(tmp_path)
    task = root if role == "orchestrator" else _worker(lesson_store, episode, root, "worker")
    owner = lesson_store.local_owner
    lesson_store.seat_project_member(task.project_id, owner.user_id)
    with lesson_store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id=? WHERE operation_id=?",
            (owner.user_id, task.operation_id),
        )
    dispatcher = AutoResearchCommandDispatcher(
        lesson_store, _Effects(lesson_store, episode, root).bundle()
    )
    call = request("add", key="owner-tip", text="Use the project environment.")

    response = dispatcher.dispatch(task.operation_id, call)

    assert response.status == "ok", response
    lesson = response.result["lesson"]
    assert lesson["author"] == {"kind": "agent", "operation_id": task.operation_id}
    assert lesson["human_owned"] is False
    assert dispatcher.dispatch(task.operation_id, call).result == response.result
    assert lesson_store.list_lessons(task.project_id) == [lesson]
