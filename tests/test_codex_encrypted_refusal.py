from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcp.providers.base import ProviderTurnRequest
from rcp.providers.codex.app_server import CodexAppServerRuntime
from rcp.providers.codex.profile import CodexProfile, is_encrypted_content_refusal


@pytest.mark.parametrize(
    ("detail", "refused"),
    [
        ('{"code":"invalid_encrypted_content"}', True),
        ("The encrypted content for item rs_example could not be verified", False),
        ("encrypted content hydration failed", False),
        ("401 Unauthorized", False),
        ("refresh_token_reused", False),
        ("Connection closed", False),
        ("invalid_request_error", False),
    ],
)
def test_encrypted_refusal_classification(detail: str, refused: bool) -> None:
    assert is_encrypted_content_refusal(detail) is refused


@pytest.mark.parametrize("event_type", ["error", "turn.failed"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("string_error", [False, True])
def test_exec_json_preserves_encrypted_refusal(
    event_type: str, nested: bool, string_error: bool
) -> None:
    error = _refusal_error(string_error)
    value = {"type": event_type, **({"error": error} if nested else error)}
    event = CodexProfile().decode_event(value, json.dumps(value))
    assert is_encrypted_content_refusal(event.text)
    assert event.event == ("error" if event_type == "turn.failed" else "message")
    assert not CodexProfile().credential_failure(event.text)


@pytest.mark.parametrize("failure_path", ["response", "notification", "turn"])
@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("string_error", [False, True])
def test_app_server_preserves_encrypted_refusal(
    tmp_path: Path, failure_path: str, fork: bool, string_error: bool
) -> None:
    turn = CodexAppServerRuntime().turn(
        ProviderTurnRequest(
            prompt="Continue",
            binary="codex",
            cwd=tmp_path,
            model=None,
            reasoning=None,
            session_id=None if fork else "source-thread",
            fork_session_id="source-thread" if fork else None,
            read_dirs=[],
            write_dirs=[tmp_path],
            write_scope=None,
            capability="paper_suggest",
            provider_version="0.165.0",
        )
    )
    turn.receive_line(json.dumps({"id": 1, "result": {}}))
    turn.receive_line(json.dumps({"id": 2, "result": {"config": {}}}))
    error = _refusal_error(string_error)
    thread_id = "fork-thread" if fork else "source-thread"
    if failure_path == "response":
        message = {"id": 3, "error": error}
    else:
        turn.receive_line(
            json.dumps(
                {
                    "id": 3,
                    "result": {
                        "thread": {"id": thread_id},
                        "approvalPolicy": "never",
                        "sandbox": {"type": "readOnly"},
                        "activePermissionProfile": {"id": "rcp_paper"},
                    },
                }
            )
        )
        if failure_path == "notification":
            message = {
                "method": "error",
                "params": {"threadId": thread_id, "error": error, "willRetry": False},
            }
        else:
            message = {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": "turn", "status": "failed", "error": error},
                },
            }
    step = turn.receive_line(json.dumps(message))
    assert step.complete and step.explicit_terminal
    assert len(step.events) == 1
    assert step.events[0].event == "error"
    assert is_encrypted_content_refusal(step.events[0].text)
    assert not CodexProfile().credential_failure(step.events[0].text)


def _refusal_error(string_error: bool) -> dict[str, object]:
    if not string_error:
        return {"message": "Transcript rejected", "data": {"code": "invalid_encrypted_content"}}
    # codex 0.165.0 puts the upstream error JSON inside the message string in
    # both exec-json and app-server (whose codexErrorInfo is just "other").
    upstream = {
        "type": "error",
        "error": {
            "message": "The encrypted content for item rs_example could not be verified.",
            "type": "invalid_request_error",
            "param": None,
            "code": "invalid_encrypted_content",
        },
        "status": 400,
    }
    return {"message": json.dumps(upstream), "codexErrorInfo": "other"}
