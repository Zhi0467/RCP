from __future__ import annotations

import json
import re

import pytest

from rcp.agents import AgentProcessControl, staged_command_client
from rcp.agents.command_protocol import BulletinArguments, BulletinCommandRequest
from rcp.background import AgentTaskExecution
from rcp.limits import BULLETIN_RENDER_MAX_BYTES
from rcp.runs.bulletin import render_bulletin, stage_bulletin_pointer
from rcp.runs.questions import discuss_command_handler, work_command_handler

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


def test_bulletin_client_file_backed_keyed_post(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "body.txt").write_text("note\n```\n", encoding="utf-8")
    prefix = ["--broker", "broker", "--timeout", "1", "--workspace", str(tmp_path)]
    parser = staged_command_client._parser()
    parsed = parser.parse_args(
        [*prefix, "bulletin", "--post", "--key", "k", "--ref", "n", "--reply-to", "p", "body.txt"]
    )
    verb, key, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
    assert (verb, key, arguments) == (
        "bulletin",
        "k",
        {"action": "post", "text": "note\n```", "refs": ["n"], "reply_to": "p"},
    )
    parsed = parser.parse_args([*prefix, "bulletin", "--read", "--cursor", "10"])
    assert staged_command_client._request_arguments(parsed, str(tmp_path)) == (
        "bulletin",
        None,
        {"action": "read", "cursor": "10"},
    )
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
    assert list(map(int, re.findall(r"\d+", rendered.splitlines()[-1]))) == [100 - len(agents), 1]
    assert all(row["state"] == "waiting" for row in agents)
    assert len(rendered.encode()) <= BULLETIN_RENDER_MAX_BYTES
