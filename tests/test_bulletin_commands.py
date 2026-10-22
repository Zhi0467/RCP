from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from rcp.agents import AgentProcessControl, staged_command_client
from rcp.agents.command_protocol import BulletinArguments, BulletinCommandRequest
from rcp.background import AgentTaskExecution
from rcp.limits import BULLETIN_RENDER_MAX_BYTES
from rcp.runs.bulletin import render_bulletin, stage_bulletin_pointer
from rcp.runs.questions import discuss_command_handler, work_command_handler
from rcp.runs.tasks.work import _work_execution_instructions

from .test_lessons_store import store  # noqa: F401
from .test_work_questions import identity


def request(action, *, key=None, **arguments):
    return BulletinCommandRequest(
        mailbox_id="a" * 32,
        request_id="b" * 32,
        credential="c" * 64,
        idempotency_key=key,
        verb="bulletin",
        arguments=BulletinArguments(action=action, **arguments),
    )


def test_bulletin_owner_authority(store, tmp_path):  # noqa: F811
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    handler = work_command_handler(execution, None)
    post = request("post", key="once", text="note")
    assert handler(post, identity("validate_only")).status == "invalid"
    assert handler(post, identity(task_id="other")).status == "invalid"
    assert handler(post, identity()).status == "ok"
    assert handler(request("read"), identity("validate_only")).status == "invalid"
    result = handler(request("read"), identity()).result
    assert isinstance(result["posts"], list)
    assert len(result["posts"]) == 1
    assert "rendered" not in result
    handler = discuss_command_handler(execution)
    assert handler(post, identity()).status == "invalid"
    assert handler(request("read"), identity()).status == "invalid"


def test_contract_usage_and_enforcement_share_the_bulletin_verb(store, tmp_path, monkeypatch):  # noqa: F811
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    turn = SimpleNamespace(
        execution=execution,
        compute_commands=None,
        service=None,
        graph_read_scope=None,
        patch_inputs=SimpleNamespace(validator_staged=SimpleNamespace(ask_wait_seconds=1.0)),
    )
    post = request("post", key="once", text="note")
    assert "bulletin" in work_command_handler(execution, None).allowed_verbs
    assert "rcp-client bulletin --post" in _work_execution_instructions(turn, client="rcp-client")
    monkeypatch.setattr(store, "bulletin_enabled_for_task", lambda _operation_id: False)
    handler = work_command_handler(execution, None)
    assert "bulletin" not in handler.allowed_verbs
    assert "bulletin" not in _work_execution_instructions(turn, client="rcp-client")
    assert handler(post, identity()).status == "invalid"


def test_bulletin_client_file_backed_keyed_post(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "body.txt").write_text("note\n```\n", encoding="utf-8")
    prefix = ["--broker", "broker", "--timeout", "1", "--workspace", str(tmp_path)]
    parser = staged_command_client._parser()
    parsed = parser.parse_args(
        [*prefix, "bulletin", "--post", "--key", "k", "--reply-to", "p", "body.txt"]
    )
    verb, key, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
    assert (verb, key, arguments) == (
        "bulletin",
        "k",
        {"action": "post", "text": "note\n```", "reply_to": "p"},
    )
    parsed = parser.parse_args([*prefix, "bulletin", "--read", "--cursor", "10"])
    assert staged_command_client._request_arguments(parsed, str(tmp_path)) == (
        "bulletin",
        None,
        {"action": "read", "cursor": "10"},
    )
    for action in ("edit", "delete"):
        command = [*prefix, "bulletin", f"--{action}", "p", "--key", "k"]
        if action == "edit":
            command.append("body.txt")
        parsed = parser.parse_args(command)
        verb, key, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
        assert arguments["action"] == action and arguments["post_id"] == "p"
        assert request(action, key=key, **{k: v for k, v in arguments.items() if k != "action"})
        with pytest.raises(ValueError):
            request(action, post_id="p", **({"text": "edit"} if action == "edit" else {}))
    with pytest.raises(staged_command_client.ClientInputError):
        parser.parse_args([*prefix, "bulletin", "--post", "--key", "k", "--ref", "n", "body.txt"])
    with pytest.raises(ValueError):
        request("post", text="unkeyed")


def test_bulletin_rendering_is_bounded_and_cannot_escape_fences(store, tmp_path, monkeypatch):  # noqa: F811
    text = "```\n# Forged\n~~~\n"
    snapshot = {"roster": [{"id": "agent"}], "posts": [{"text": text}] * 1000, "total": 1000}
    rendered = render_bulletin(snapshot)
    assert len(rendered.encode()) <= BULLETIN_RENDER_MAX_BYTES
    assert "\n# Forged\n" not in rendered
    blocks = rendered.split("```json\n")[1:]
    assert all(json.loads(block.split("\n```", 1)[0]) for block in blocks)
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    assert stage_bulletin_pointer(execution, tmp_path, None)
    assert (tmp_path / "workspace" / "bulletin.md").is_file()
    monkeypatch.setattr(store, "bulletin_enabled_for_task", lambda _operation_id: False)
    assert stage_bulletin_pointer(execution, tmp_path, None) == ""
    assert not (tmp_path / "workspace" / "bulletin.md").exists()


@pytest.mark.asyncio
async def test_validation_mailbox_refuses_bulletin(tmp_path):
    from rcp.agents.command_mailbox import _read_request, stage_command_mailbox

    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=None,
        task_id="turn",
        turn_id="turn",
        authority="validate_only",
    )
    staged.credential.activate()
    try:
        for action in ("read", "post"):
            call = request(
                action, **({"key": "k", "text": "note"} if action == "post" else {})
            ).model_copy(
                update={
                    "mailbox_id": staged.credential.mailbox_id,
                    "credential": staged.credential.token,
                }
            )
            (tmp_path / "request.json").write_text(call.model_dump_json(), encoding="utf-8")
            with pytest.raises(ValueError):
                await _read_request("request.json", call.request_id, staged)
    finally:
        staged.cleanup()


def test_auto_research_stop_fences_posts_but_keeps_bulletin_readable(tmp_path, monkeypatch):
    from rcp.runs.auto_research import request_auto_research_stop

    from .test_auto_research_commands import _dispatcher, _Effects, _setup_auto_research

    app_store, episode, root = _setup_auto_research(tmp_path)
    dispatcher = _dispatcher(app_store, _Effects(app_store, episode, root).bundle())
    monkeypatch.setattr("rcp.storage.bulletin.live_project_member", lambda *_: True)
    post = request("post", key="once", text="note")
    receipt = dispatcher.dispatch(root.operation_id, post)
    assert receipt.status == "ok"
    execute = app_store.execute_bulletin_command

    def stopped_post(*args, **kwargs):
        request_auto_research_stop(app_store, episode.episode_id)
        return execute(*args, **kwargs)

    monkeypatch.setattr(app_store, "execute_bulletin_command", stopped_post)
    assert (
        dispatcher.dispatch(root.operation_id, request("post", key="late", text="note")).status
        == "invalid"
    )
    for action in ("edit", "delete"):
        late = request(
            action,
            key=action,
            post_id=receipt.result["post"]["post_id"],
            **({"text": "late edit"} if action == "edit" else {}),
        )
        assert (
            dispatcher.dispatch(root.operation_id, late).result["code"] == "bulletin_post_forbidden"
        )
    assert dispatcher.dispatch(root.operation_id, post).result == receipt.result
    assert dispatcher.dispatch(root.operation_id, request("read")).result["posts"] == [
        receipt.result["post"]
    ]


def test_staged_roster_cannot_starve_posts():
    from rcp.limits import BULLETIN_ROSTER_MAX_COUNT

    snapshot = {
        "roster": [{"id": str(i), "state": "done", "text": "x" * 2000} for i in range(100)]
        + [{"id": str(i), "state": "waiting"} for i in range(100)],
        "posts": [
            {"post_id": "oversized", "refs": ["x" * 2000] * 20},
            {"post_id": "post", "text": "x" * 2000},
        ],
    }
    rendered = render_bulletin(snapshot)
    records = [json.loads(block.split("\n```", 1)[0]) for block in rendered.split("```json\n")[1:]]
    assert {row["post_id"] for row in records if "post_id" in row} == {"post"}
    agents = [row for row in records if "state" in row]
    assert len(agents) == BULLETIN_ROSTER_MAX_COUNT
    assert list(map(int, re.findall(r"\d+", rendered.splitlines()[-1]))) == [
        0,
        0,
        100 - len(agents),
        1,
    ]
    assert all(row["state"] == "waiting" for row in agents)
    assert len(rendered.encode()) <= BULLETIN_RENDER_MAX_BYTES


def test_agent_mentions_are_first_in_snapshot_and_read(store, tmp_path):  # noqa: F811
    from rcp.bulletin import bulletin_actor_id

    with store.connection() as connection:
        task = connection.execute("SELECT * FROM graph_runs WHERE operation_id='turn'").fetchone()
        actor_id = bulletin_actor_id(connection, task)
    post = store.mutate_bulletin_post(
        "project",
        "main",
        text=f"For <@agent:{actor_id}>",
        key="mention",
        user_id=store.local_owner.user_id,
    )
    store.mutate_bulletin_post(
        "project",
        "main",
        text="More recent ordinary post",
        key="ordinary",
        user_id=store.local_owner.user_id,
    )
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    stage_bulletin_pointer(execution, tmp_path, None)
    rendered = (tmp_path / "workspace" / "bulletin.md").read_text()
    first_record = json.loads(rendered.split("```json\n")[1].split("\n```", 1)[0])
    assert first_record["post_id"] == post["post_id"]
    handler = work_command_handler(execution, None)
    read = handler(request("read"), identity()).result
    assert next(iter(read)) == "mentions"
    assert read["mentions"] == [{**post, "target_key": "main"}]
    assert read["open_mentions"] == 1
    assert store.local_owner.user_id in {member["user_id"] for member in read["members"]}
    # Its own reply in the mention's thread answers it; nothing else does.
    handler(request("post", key="aside", text="unrelated"), identity())
    assert handler(request("read"), identity()).result["open_mentions"] == 1
    handler(request("post", key="reply", text="on it", reply_to=post["post_id"]), identity())
    assert handler(request("read"), identity()).result["mentions"] == []
    # An edit after that reply asks again.
    store.mutate_bulletin_post(
        "project",
        "main",
        action="edit",
        post_id=post["post_id"],
        text=f"Also <@agent:{actor_id}>",
        key="edit",
        user_id=store.local_owner.user_id,
    )
    assert handler(request("read"), identity()).result["open_mentions"] == 1
    # A mention under a deleted root cannot be answered, so it is not open.
    root = store.mutate_bulletin_post(
        "project", "main", text="root", key="root", user_id=store.local_owner.user_id
    )
    store.mutate_bulletin_post(
        "project",
        "main",
        text=f"<@agent:{actor_id}> in a thread",
        reply_to=root["post_id"],
        key="threaded",
        user_id=store.local_owner.user_id,
    )
    assert handler(request("read"), identity()).result["open_mentions"] == 2
    store.mutate_bulletin_post(
        "project",
        "main",
        action="delete",
        post_id=root["post_id"],
        key="drop-root",
        user_id=store.local_owner.user_id,
    )
    assert handler(request("read"), identity()).result["open_mentions"] == 1


def test_a_mention_from_another_channel_reaches_the_agent_and_takes_its_reply(store):  # noqa: F811
    from rcp.bulletin import bulletin_actor_id

    with store.connection() as connection:
        task = connection.execute("SELECT * FROM graph_runs WHERE operation_id='turn'").fetchone()
        actor_id = bulletin_actor_id(connection, task)
    elsewhere = "branch:other"
    post = store.mutate_bulletin_post(
        "project",
        elsewhere,
        text=f"For <@agent:{actor_id}>",
        key="mention",
        user_id=store.local_owner.user_id,
    )
    handler = work_command_handler(
        AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl()), None
    )
    read = handler(request("read"), identity()).result
    assert [(item["post_id"], item["target_key"]) for item in read["mentions"]] == [
        (post["post_id"], elsewhere)
    ]
    handler(request("post", key="reply", text="on it", reply_to=post["post_id"]), identity())
    assert handler(request("read"), identity()).result["open_mentions"] == 0
    (thread,) = [
        item for item in store.list_bulletin("project", elsewhere)["posts"] if item["reply_to"]
    ]
    assert thread["reply_to"] == post["post_id"]
    assert store.list_bulletin("project", "main")["posts"] == []


def test_agent_edits_and_deletes_across_turns_of_same_actor(store):  # noqa: F811
    first = store.agent_task("turn")
    assert first is not None
    posted = store.execute_bulletin_command("project", "turn", "post", key="post", text="original")
    post_id = posted["post"]["post_id"]
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='turn'")
    for operation_id, chat_id in (("next", "chat"), ("other", "other-chat")):
        store.create_agent_task(
            first.model_copy(
                update={
                    "operation_id": operation_id,
                    "native_session_id": operation_id,
                    "stage_root": f"/stage/{operation_id}",
                    "request": {**first.request, "chat_id": chat_id},
                }
            )
        )
    execution = AgentTaskExecution(operation_id="next", store=store, control=AgentProcessControl())
    handler = work_command_handler(execution, None)
    edit = request("edit", key="edit", post_id=post_id, text="updated #exp/result")
    edited = handler(edit, identity(task_id="next"))
    assert edited.status == "ok"
    assert edited.result["post"]["refs"] == ["exp/result"]
    assert edited.result["post"]["author"]["actor_id"] == "chat:chat"
    assert handler(edit, identity(task_id="next")).result == edited.result
    other = work_command_handler(
        AgentTaskExecution(operation_id="other", store=store, control=AgentProcessControl()),
        None,
    )
    for action in ("edit", "delete"):
        response = other(
            request(
                action,
                key=action,
                post_id=post_id,
                **({"text": "forged"} if action == "edit" else {}),
            ),
            identity(task_id="other"),
        )
        assert response.result["code"] == "bulletin_not_author"
    delete = request("delete", key="delete", post_id=post_id)
    deleted = handler(delete, identity(task_id="next"))
    assert deleted.status == "ok"
    assert handler(delete, identity(task_id="next")).result == deleted.result
    assert store.list_bulletin("project", "main")["posts"] == []


def test_bulletin_channel_and_artifact_options_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "body.txt").write_text("<artifact:" + "a" * 24 + ">")
    prefix = ["--broker", "broker", "--timeout", "1", "--workspace", str(tmp_path)]
    parser = staged_command_client._parser()
    for flags, expected in (
        (
            ["--post", "--key", "k", "--channel", "other branch", "body.txt"],
            {
                "action": "post",
                "channel": "other branch",
                "reply_to": None,
                "text": "<artifact:" + "a" * 24 + ">",
            },
        ),
        (["--artifact", "a" * 24], {"action": "artifact", "artifact_id": "a" * 24}),
    ):
        parsed = parser.parse_args([*prefix, "bulletin", *flags])
        verb, key, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
        assert verb == "bulletin" and arguments == expected
        envelope = request(
            arguments["action"], key=key, **{k: v for k, v in arguments.items() if k != "action"}
        )
        assert envelope.arguments.model_dump(exclude_none=True) == {
            k: v for k, v in expected.items() if v is not None
        }
    for flags in (
        ["--read", "--channel", "main"],
        ["--post", "--reply-to", "p", "--channel", "main", "--key", "k", "body.txt"],
        ["--edit", "p", "--channel", "main", "--key", "k", "body.txt"],
        ["--artifact", "a" * 24, "--cursor", "c"],
    ):
        with pytest.raises(staged_command_client.ClientInputError):
            staged_command_client._request_arguments(
                parser.parse_args([*prefix, "bulletin", *flags]), str(tmp_path)
            )
    for arguments in (
        {"action": "read", "channel": "main"},
        {"action": "post", "text": "reply", "reply_to": "p", "channel": "main"},
        {"action": "artifact"},
        {"action": "artifact", "artifact_id": "a" * 24, "text": "extra"},
        {"action": "read", "artifact_id": "a" * 24},
    ):
        with pytest.raises(ValueError):
            BulletinArguments(**arguments)
