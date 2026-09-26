"""The shipped fence must end a turn exactly where the canonical decoder does.

RCP cannot ship its decoder to an execution host, so the rule for "this turn is
over" is written twice and nothing structural holds the two together. What keeps
them together is this: compare them on the traffic itself rather than on the
shapes someone thought to worry about.

Only boundaries are compared, because only boundaries are written twice. The
host publishes no verdict -- whether a finished turn succeeded is read off its
journal by the one decoder that has always decided it.
"""

from __future__ import annotations

import json

import pytest

from rcp.agents.remote_turn_fence import TurnFence


def _canonical_app_server_turn(tmp_path, *, handshake: bool):
    from rcp.agents.codex_app_server import CodexAppServerRuntime
    from rcp.providers import ProviderTurnRequest

    turn = CodexAppServerRuntime().turn(
        ProviderTurnRequest(
            prompt="fence",
            binary="codex",
            cwd=tmp_path,
            model=None,
            reasoning=None,
            session_id="fence-thread",
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            capability="paper_readonly",
            provider_version="0.153.4",
        )
    )
    fence = TurnFence("codex.app-server-stdio.v1")
    if handshake:
        for message in (
            {"id": 1, "result": {}},
            {"id": 2, "result": {"config": {}}},
            {
                "id": 3,
                "result": {
                    "thread": {"id": "fence-thread"},
                    "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly"},
                },
            },
        ):
            turn.receive_line(json.dumps(message))
        fence.input({"id": 3, "method": "thread/resume"})
        fence.output({"id": 3, "result": {"thread": {"id": "fence-thread"}}})
    return turn, fence


def _canonical_turn(tmp_path, runtime_id: str):
    from rcp.providers import ProviderTurnRequest, profile_for

    provider = "claude" if runtime_id.startswith("claude") else "codex"
    return (
        profile_for(provider)
        .runtime(runtime_id)
        .turn(
            ProviderTurnRequest(
                prompt="corpus",
                binary=provider,
                cwd=tmp_path,
                model=None,
                reasoning=None,
                session_id="corpus-thread",
                read_dirs=[],
                write_dirs=[],
                write_scope=None,
                capability="paper_readonly",
                provider_version="0.153.4",
            )
        )
    )


def _started_decoder_pair(tmp_path, runtime_id: str):
    """Both decoders for one runtime, holding the same turn and ready for its traffic."""

    if runtime_id == "codex.app-server-stdio.v1":
        turn, fence = _canonical_app_server_turn(tmp_path, handshake=True)
        started = {"id": 4, "result": {"turn": {"id": "corpus-turn"}}}
        turn.receive_line(json.dumps(started))
        fence.input({"id": 4, "method": "turn/start"})
        fence.output(started)
        return turn, fence
    turn = _canonical_turn(tmp_path, runtime_id)
    fence = TurnFence(runtime_id)
    start = (
        {"type": "system", "subtype": "init", "session_id": "corpus-thread"}
        if runtime_id.startswith("claude")
        else {"type": "thread.started", "thread_id": "corpus-thread"}
    )
    turn.receive_line(json.dumps(start))
    fence.output(start)
    return turn, fence


# One protocol message per case: every shape each runtime can end a turn with,
# and the ordinary traffic it must not end one with.
_COMPLETION_CORPUS = tuple(
    (runtime_id, message)
    for runtime_id, messages in (
        (
            "codex.exec-json.v1",
            (
                {"type": "thread.started", "thread_id": "corpus-thread"},
                {"type": "turn.started"},
                {"type": "item.started", "item": {"type": "agent_message"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
                {"type": "item.completed", "item": {"type": "command_execution", "text": "ls"}},
                {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
                {"type": "turn.failed", "error": {"message": "the model refused"}},
                {"type": "error", "message": "codex crashed"},
                {"type": "unrecognized.event"},
                {"item": {"type": "agent_message", "text": "no type at all"}},
            ),
        ),
        (
            "claude.stream-json.v1",
            (
                {"type": "system", "subtype": "init", "session_id": "corpus-thread"},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
                {"type": "result", "subtype": "success", "result": "answer"},
                {"type": "result", "subtype": "error_during_execution", "is_error": True},
                {"type": "result", "subtype": ""},
                {"type": "error", "message": "claude crashed"},
                {"type": "unrecognized.event"},
                {"message": {"content": []}},
            ),
        ),
        (
            "codex.app-server-stdio.v1",
            (
                {"id": 7, "method": "applyPatchApproval"},
                {"id": 7, "method": "applyPatchApproval", "params": {}},
                {"id": 7, "method": "execCommandApproval", "params": {"threadId": "corpus-thread"}},
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "corpus-turn", "status": "completed"}},
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "corpus-turn", "status": "failed"}},
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "child",
                        "turn": {"id": "child-turn", "status": "completed"},
                    },
                },
                {"method": "error", "params": {"willRetry": False, "message": "boom"}},
                {
                    "method": "error",
                    "params": {"willRetry": False, "turnId": "other-turn", "message": "boom"},
                },
                {"method": "error", "params": {"willRetry": True, "message": "retrying"}},
                {"method": "item/completed", "params": {"threadId": "corpus-thread", "item": {}}},
                {"method": "item/completed", "params": {"threadId": "child", "item": {}}},
                {"method": "turn/started", "params": {"threadId": "corpus-thread"}},
            ),
        ),
    )
    for message in messages
)


@pytest.mark.parametrize(
    "runtime_id,message", _COMPLETION_CORPUS, ids=lambda value: str(value)[:44]
)
def test_the_fence_ends_a_turn_where_the_canonical_decoder_ends_it(tmp_path, runtime_id, message):
    turn, fence = _started_decoder_pair(tmp_path, runtime_id)
    step = turn.receive_line(json.dumps(message))
    fence.output(message)

    assert step.explicit_terminal == fence.terminal


@pytest.mark.parametrize("method", ["initialize", "config/read", "thread/start"])
def test_a_rejected_handshake_ends_the_turn_in_both_decoders(tmp_path, method) -> None:
    """A server that refuses to start is still a server left running.

    Its error reply is the only end this turn will ever have, and the fence is
    what stops a persistent one sitting there with nothing to close it.
    """

    turn, fence = _canonical_app_server_turn(tmp_path, handshake=False)
    rejection = {"id": 1, "error": {"code": -32603, "message": "refused"}}
    fence.input({"id": 1, "method": method})
    step = turn.receive_line(json.dumps(rejection))
    fence.output(rejection)

    assert step.explicit_terminal == fence.terminal is True


def test_the_protocol_corpus_covers_both_verdicts(tmp_path):
    """A corpus both decoders agreed to ignore entirely would prove nothing."""

    seen: dict[str, set[bool]] = {runtime: set() for runtime, _ in _COMPLETION_CORPUS}
    for runtime_id, message in _COMPLETION_CORPUS:
        _turn, fence = _started_decoder_pair(tmp_path, runtime_id)
        fence.output(message)
        seen[runtime_id].add(fence.terminal)
    assert all(verdicts == {False, True} for verdicts in seen.values()), seen


@pytest.mark.parametrize("handshake", [False, True])
@pytest.mark.parametrize(
    "request_message",
    [
        {"id": 7, "method": "applyPatchApproval"},
        {"id": 7, "method": "applyPatchApproval", "params": {}},
        {"id": 7, "method": "execCommandApproval", "params": {"threadId": "fence-thread"}},
    ],
)
def test_an_unattended_turn_cannot_answer_a_request_so_both_decoders_stop_it(
    tmp_path, handshake, request_message
):
    turn, fence = _canonical_app_server_turn(tmp_path, handshake=handshake)
    step = turn.receive_line(json.dumps(request_message))
    fence.output(request_message)

    assert step.explicit_terminal == fence.terminal is True


def test_the_fence_publishes_no_verdict(tmp_path):
    """Success and failure end a turn identically here; only RCP separates them.

    This is the whole reason the fence is smaller than what it replaced. A host
    that names a winner is a second opinion on a protocol RCP already reads, and
    the two drift on exactly the shapes nobody compared.
    """

    _turn, completed = _started_decoder_pair(tmp_path, "codex.exec-json.v1")
    _turn, failed = _started_decoder_pair(tmp_path, "codex.exec-json.v1")
    completed.output({"type": "turn.completed", "usage": {}})
    failed.output({"type": "turn.failed", "error": {"message": "refused"}})

    assert completed.terminal is failed.terminal is True
    assert not hasattr(completed, "complete")


def test_captured_claude_notice_does_not_finish_or_answer(tmp_path):
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures" / "claude_turn_completion"
    sent = json.loads((fixtures / "resume-input.json").read_text())
    turn, fence = _started_decoder_pair(tmp_path, "claude.stream-json.v1")
    turn.adopt_recorded_inputs([sent["uuid"]], {})
    fence.input(sent)
    steps = []
    for line in (fixtures / "resume.jsonl").read_text().splitlines():
        value = json.loads(line)
        step = turn.receive_line(line)
        fence.output(value)
        assert step.explicit_terminal == fence.terminal
        steps.append((value, step))
    notice = next(step for value, step in steps if value.get("origin"))
    assert not notice.complete
    assert all(event.event != "answer" for event in notice.events)
    own_result = {"type": "result", "subtype": "success", "user_message_uuid": sent["uuid"]}
    assert turn.receive_line(json.dumps(own_result)).complete
    fence.output(own_result)
    assert fence.terminal


@pytest.mark.parametrize("variant", ["sent", "failed", "unattributed", "foreign"])
def test_claude_notice_origin_and_own_input_control_completion(tmp_path, variant):
    turn, fence = _started_decoder_pair(tmp_path, "claude.stream-json.v1")
    sent = json.loads(turn.initial_input())
    fence.input(sent)
    result = {"type": "result", "subtype": "success", "origin": {"kind": "task-notification"}}
    if variant == "sent":
        result["user_message_uuid"] = sent["uuid"]
    elif variant == "failed":
        result["is_error"] = True
    elif variant == "unattributed":
        result.pop("origin")
    else:
        result["user_message_uuid"] = "foreign"
    step = turn.receive_line(json.dumps(result))
    fence.output(result)
    assert step.complete == fence.terminal == (variant != "foreign")


def test_claude_open_task_blocks_result_and_records_first_wait(tmp_path):
    turn, fence = _started_decoder_pair(tmp_path, "claude.stream-json.v1")
    result = {"type": "result", "subtype": "success"}
    for value in ({"type": "system", "subtype": "task_started", "task_id": "child"}, result):
        assert not turn.receive_line(json.dumps(value)).complete
        fence.output(value)
        assert not fence.terminal
    since = fence.completion.open_work_since
    assert since is not None
    fence.output(result)
    assert fence.completion.open_work_since == since
    stopped = {"type": "system", "subtype": "task_notification", "task_id": "child"}
    assert turn.receive_line(json.dumps(stopped)).complete
    fence.output(stopped)
    assert fence.terminal


def test_codex_exec_retry_errors_remain_traces_until_turn_failed(tmp_path):
    turn, fence = _started_decoder_pair(tmp_path, "codex.exec-json.v1")
    for index in range(2):
        notice = {"type": "item.completed", "item": {"type": "error", "message": str(index)}}
        step = turn.receive_line(json.dumps(notice))
        fence.output(notice)
        assert not step.complete and not fence.terminal
        assert step.events[0].event not in {"answer", "error"}
    for attempt in (2, 3):
        retry = {"type": "error", "message": f"Reconnecting... {attempt}/5 (...)"}
        step = turn.receive_line(json.dumps(retry))
        fence.output(retry)
        assert not step.complete and not fence.terminal
        assert step.events[0].event == "message"
        assert turn.last_error == fence.last_error == retry["message"]
    failure = {"type": "turn.failed", "error": {"message": "retry budget exhausted"}}
    assert turn.receive_line(json.dumps(failure)).complete
    fence.output(failure)
    assert fence.terminal


def test_app_server_child_receipt_settles_pending_root_completion(tmp_path):
    turn, fence = _started_decoder_pair(tmp_path, "codex.app-server-stdio.v1")
    values = [
        {
            "method": "item/started",
            "params": {
                "threadId": "fence-thread",
                "item": {"type": "subAgentActivity", "kind": "started", "agentThreadId": "child"},
            },
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "corpus-turn", "status": "completed"}},
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "child", "turn": {"id": "child-turn", "status": "completed"}},
        },
    ]
    for index, value in enumerate(values):
        step = turn.receive_line(json.dumps(value))
        fence.output(value)
        assert step.complete == fence.terminal == (index == 2)
