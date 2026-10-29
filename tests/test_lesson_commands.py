from __future__ import annotations

from typing import cast

import pytest

from rcp.agents import AgentLauncher, AgentProcessControl, staged_command_client
from rcp.agents.command_protocol import LessonArguments, LessonCommandRequest
from rcp.background import AgentTaskExecution
from rcp.runs.questions import discuss_command_handler, work_command_handler

from .test_lessons_store import consolidate, store  # noqa: F401
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
    assert isinstance(first.result["lesson"], dict)
    lesson_id = first.result["lesson"]["lesson_id"]
    response = handler(request("delete", key="edit", lesson_id=lesson_id), identity())
    assert response.status == "ok"
    assert handler(add, identity()).result == first.result
    assert handler(request("delete", key="edit", lesson_id=lesson_id), identity()).result == {}
    assert store.list_lessons("project") == []


@pytest.mark.parametrize("kind", ["node_chat", "project_chat"])
def test_member_work_chat_manages_every_lesson(store, kind):  # noqa: F811
    consolidate(store)
    agent = store.execute_lesson_command("project", "turn", "add", key="seed", text="agent")[
        "lesson"
    ]
    human = store.add_lesson(
        "project", "human", user_id=store.local_owner.user_id, display_name="Original author"
    )
    with store.connection() as connection:
        connection.execute("DELETE FROM consolidation_runs")
        connection.execute("UPDATE graph_runs SET kind=? WHERE operation_id='turn'", (kind,))
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    handler = work_command_handler(execution, None)
    listed = handler(request("list"), identity())
    assert listed.status == "ok"
    assert isinstance(listed.result["lessons"], list)
    assert {item["lesson_id"] for item in listed.result["lessons"]} == {
        agent["lesson_id"],
        human["lesson_id"],
    }
    task = store.agent_task("turn")
    for lesson in (agent, human):
        lesson_id = lesson["lesson_id"]
        updated = handler(
            request("update", key=lesson_id, lesson_id=lesson_id, text="merged"), identity()
        )
        assert updated.status == "ok"
        assert isinstance(updated.result["lesson"], dict)
        assert updated.result["lesson"]["human_owned"] is True
        assert updated.result["lesson"]["author"] == {
            "kind": "human",
            "user_id": task.authorized_by.user_id,
            "display_name": task.authorized_by.display_name,
        }
        assert (
            handler(request("delete", key=lesson_id, lesson_id=lesson_id), identity()).status
            == "ok"
        )
    assert store.list_lessons("project") == []


@pytest.mark.parametrize("mode", ["work", "discuss"])
def test_chat_lesson_commands_recheck_live_membership(store, mode):  # noqa: F811
    import json

    from rcp.service import RunRequest, resolve_dispatch_authority

    chat = RunRequest(
        chat_scope="project", chat_id="chat", message="Manage lessons", mode=mode, provider="codex"
    )
    authority = resolve_dispatch_authority("project_chat", chat)
    assert authority is not None
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET dispatch_authority_json=?,request_json=? WHERE operation_id='turn'",
            (
                authority.model_dump_json(),
                json.dumps(chat.model_dump(mode="json")),
            ),
        )
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    factory = (
        discuss_command_handler if mode == "discuss" else lambda ex: work_command_handler(ex, None)
    )
    handler = factory(execution)
    added = handler(request("add", key="a", text="tip"), identity())
    assert added.status == "ok"
    assert isinstance(added.result["lesson"], dict)
    lesson_id = added.result["lesson"]["lesson_id"]
    with store.connection() as connection:
        connection.execute("DELETE FROM project_members WHERE project_id='project'")
    for call in (
        request("add", key="a", text="tip"),
        request("list"),
        request("update", key="u", lesson_id=lesson_id, text="changed"),
        request("delete", key="d", lesson_id=lesson_id),
    ):
        response = handler(call, identity())
        assert response.status == "invalid"
        assert response.result["code"] == "lesson_forbidden"
    assert factory(execution).lesson_actions == ()
    assert len(store.list_lessons("project")) == 1


@pytest.mark.parametrize("owner", ["experiment", "child_work"])
def test_episode_chat_is_add_only(store, owner):  # noqa: F811
    import json

    with store.connection() as connection:
        task = store.agent_task("turn")
        authority = task.dispatch_authority.model_dump(mode="json")
        if owner == "experiment":
            authority["scope"]["patch_kind"] = "experiment_loop"
            authority["scope"]["control_node_id"] = "exp/test"
            authority["scope"]["control_episode_id"] = "episode"
        connection.execute(
            "UPDATE graph_runs SET episode_id='episode',dispatch_authority_json=? WHERE operation_id='turn'",
            (json.dumps(authority),),
        )
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    handler = work_command_handler(execution, None)
    added = handler(request("add", key="a", text="tip"), identity())
    assert added.status == "ok"
    assert isinstance(added.result["lesson"], dict)
    assert added.result["lesson"]["human_owned"] is False
    for action in ("list", "update", "delete"):
        arguments = {"lesson_id": added.result["lesson"]["lesson_id"]} if action != "list" else {}
        if action == "update":
            arguments["text"] = "changed"
        refused = handler(
            request(action, key=None if action == "list" else action, **arguments), identity()
        )
        assert refused.status == "invalid"
        assert refused.result["code"] == "lesson_edit_not_authorized"


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
    assert owner is not None
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
    assert isinstance(lesson, dict)
    assert lesson["author"] == {"kind": "agent", "operation_id": task.operation_id}
    assert lesson["human_owned"] is False
    assert dispatcher.dispatch(task.operation_id, call).result == response.result
    assert lesson_store.list_lessons(task.project_id) == [lesson]


@pytest.mark.parametrize("mode", ["work", "discuss"])
def test_served_chat_lesson_broker_and_discuss_graph_boundary(manifest, tmp_path, mode):
    import asyncio
    import json
    import shlex
    from pathlib import Path
    from uuid import uuid4

    from rcp.runs.tasks.discuss import stream_discuss_run
    from rcp.runs.tasks.work import stream_work_run

    from .helpers import (
        create_named_app,
        current_command_client,
        signed_in_client,
        wait_for_task_response,
    )
    from .test_api import ScriptedLauncher

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    project_id = app.state.default_project_id
    results = []

    class Launcher(ScriptedLauncher):
        async def stream(self, provider, prompt, **kwargs):
            prefix = shlex.split(current_command_client(prompt))

            async def call(*args):
                async with kwargs["invocation_gate"].serve_current_session():
                    process = await asyncio.create_subprocess_exec(
                        *prefix,
                        *args,
                        cwd=kwargs["cwd"],
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await process.communicate()
                assert not stderr, stderr.decode()
                response = json.loads(stdout)
                results.append((args, process.returncode, response))
                return response

            added = await call("lesson", "add", "--key", "add", "--text", "Use uv")
            assert added["status"] == "ok", added
            lesson = added["result"]["lesson"]
            assert lesson["human_owned"] is True
            listed = await call("lesson", "list")
            assert listed["result"]["lessons"] == [lesson]
            assert (await call("lesson", "delete", "--key", "delete", "--id", lesson["lesson_id"]))[
                "status"
            ] == "ok"
            assert (await call("lesson", "add", "--key", "add", "--text", "Use uv"))[
                "result"
            ] == added["result"]
            if mode == "discuss":
                (Path(kwargs["cwd"]) / "patch.json").write_text('{"operations":[]}')
                assert (await call("apply", "--key", "forbidden", "patch.json"))[
                    "status"
                ] == "invalid"
            async for event in super().stream(provider, prompt, **kwargs):
                yield event

    launcher = Launcher([{}], message="Lessons managed")

    async def stream(_project_id, _kind, chat, execution):
        run = stream_discuss_run if mode == "discuss" else stream_work_run
        async for frame in run(
            app.state.service,
            cast(AgentLauncher, launcher),
            chat,
            tmp_path / "data",
            execution=execution,
        ):
            yield frame

    tasks.stream = stream
    with signed_in_client(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                "chat_id": str(uuid4()),
                "message": "Manage lessons",
                "mode": mode,
                "run_truth_scope": ["repo-a"],
            },
        )
        assert response.status_code == 202, response.text
        operation_id = response.json()["operation_id"]
        result = wait_for_task_response(client, project_id, operation_id)
        assert result["status"] == "succeeded", result.get("error", result)
        assert client.get(f"/api/projects/{project_id}/lessons").json() == {"lessons": []}
    assert launcher.calls == 1
    assert all(code == (1 if args[0] == "apply" else 0) for args, code, _ in results)
    assert tasks.store.agent_task(operation_id).applied_revision is None


def test_lesson_usage_and_handler_share_resolved_actions(store, tmp_path):  # noqa: F811
    import shlex

    from rcp.runs.lesson_commands import lesson_command_usage

    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    handler = work_command_handler(execution, None)
    usage = lesson_command_usage(actions=handler.lesson_actions, client="client")
    actions = []
    for line in usage.splitlines():
        argv = shlex.split(line.removeprefix("- `").removesuffix("`"))[1:]
        argv = [value.strip("[]") for value in argv]
        parsed = staged_command_client._parser().parse_args(
            [
                "--broker",
                "broker",
                "--timeout",
                "1",
                "--workspace",
                str(tmp_path),
                *argv,
            ]
        )
        verb, _, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
        assert verb == "lesson"
        actions.append(arguments["action"])
    assert tuple(actions) == handler.lesson_actions
