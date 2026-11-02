from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution, TaskPaused
from rcp.providers import ProviderUsage
from rcp.service import RunRequest
from tests.helpers import create_named_app, store_test_claude_token
from tests.test_dispatch_authority import _authorizer, _work_authority, _work_request
from tests.test_storage import _task_record


@pytest.mark.parametrize("kind", ["project_chat", "space_chat"])
@pytest.mark.parametrize(
    "result",
    [
        "summary",
        "error",
        "timeout",
        "oversize",
        "same",
        "stop",
        "runtime_fallback",
        "unhidden",
        "missing_session",
        "wrong_session",
        "signed_out",
        "empty",
        "source_missing",
        "bug",
        "os_error",
        "auth_error",
        "stale_error",
        "remote_summary",
        "missing_transcript",
    ],
)
def test_provider_switch_seed(manifest, tmp_path, monkeypatch, kind, result):
    from rcp.core.models import HiddenReadScope, HiddenReadStatus
    from rcp.runs.agent_threads import provider_switch_seed

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    thread = next(
        item
        for item in store.ensure_agent_threads(
            human.user_id, list(store.member_project_ids(human.user_id)), default_names=("Pip",)
        )
        if item.kind == ("space" if kind == "space_chat" else "project")
    )
    request = _work_request().model_copy(update={"chat_id": thread.thread_id, "provider": "claude"})
    stage = tmp_path / "old-stage"
    stage.mkdir()
    native_session = str(uuid.uuid4())
    old = store.create_agent_task(
        _task_record(
            store.now(),
            operation_id=str(uuid.uuid4()),
            kind=kind,
            status="succeeded",
            project_id=thread.project_id or store.space_id,
            request=request.model_dump(mode="json"),
            authorized_by=human,
            dispatch_authority=_work_authority(request),
            native_session_id=native_session,
            stage_root=str(stage),
            runtime_id="claude.stream-json.v1",
            login_id="",
        )
    )
    new_request = request.model_copy(update={"provider": "claude" if result == "same" else "codex"})
    task = store.create_agent_task(
        old.model_copy(
            update={
                "operation_id": str(uuid.uuid4()),
                "status": "queued",
                "native_session_id": None,
                "stage_root": None,
                "runtime_id": "",
                "request": new_request.model_dump(mode="json"),
            }
        )
    )
    new_request = RunRequest.model_validate(task.request)
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
    store_test_claude_token(store, "example-host" if result == "remote_summary" else "")
    if result == "source_missing":
        store.record_agent_task_receipt(
            task.operation_id,
            "chat_session_resolution",
            {"reason_code": "provider_switch_tail", "source_operation_id": "gone"},
        )
    if result == "remote_summary":
        store.checkpoint_agent_task(old.operation_id, stage_host="example-host")
        monkeypatch.setattr(
            "rcp.runs.provider_process.require_remote_provider_quiescence", lambda *a: None
        )
    policy = HiddenReadScope(
        execution_machine="local",
        execution_host="example-host" if result == "remote_summary" else "",
        os_account="test",
        hidden_directories=(str(tmp_path / "other-member"),),
        enforcement=HiddenReadStatus(status="enforced"),
    )
    if result == "signed_out":
        from rcp.storage.models import LoginKey

        store.mark_provider_login_signed_out(
            LoginKey("claude", "", ""), member_id=human.user_id, source="sign_out", detail="expired"
        )
    preparations, launches = [], []

    async def prepare(old_request, source):
        preparations.append((old_request.provider, source.operation_id))
        assert old_request.browser_requested is False
        return (
            policy.model_copy(
                update={
                    "enforcement": HiddenReadStatus(
                        status="unhidden", reasons=("wrapper_unavailable",)
                    )
                }
            )
            if result == "unhidden"
            else policy
        )

    async def provider(provider, prompt, **kwargs):
        launches.append((provider, kwargs))
        assert kwargs["capability"] == "session_summary"
        assert kwargs["write_dirs"] == [] and kwargs["write_scope"] is None
        assert kwargs.get("invocation_gate") is None and kwargs.get("browser_grant") is None
        assert kwargs["hidden_read_scope"] is policy
        assert kwargs["control"] is execution.control
        if result == "remote_summary":
            assert kwargs["session_id"] == native_session and kwargs["fork_session_id"] is None
            summary_session = native_session
        else:
            assert kwargs["session_id"] is None and kwargs["fork_session_id"] == native_session
            assert kwargs["fork_source_cwd"] != str(kwargs["cwd"])
            if result in {"summary", "missing_transcript"}:
                from pathlib import Path

                from rcp.providers.claude.remote import (
                    ClaudeSessionFormat,
                    claude_project_directory,
                )

                config = tmp_path / "claude-config"
                monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
                original = (
                    config
                    / "projects"
                    / claude_project_directory(Path(kwargs["fork_source_cwd"]))
                    / f"{native_session}.jsonl"
                )
                transcript = json.dumps({"sessionId": native_session, "type": "user"})
                if result == "summary":
                    original.parent.mkdir(parents=True)
                    original.write_text(transcript)
                ClaudeSessionFormat().prepare_session_fork(
                    source_session_id=native_session,
                    source_cwd=Path(kwargs["fork_source_cwd"]),
                    cwd=kwargs["cwd"],
                )
                assert original.read_text() == transcript
                copied = (
                    config / "projects" / claude_project_directory(kwargs["cwd"]) / original.name
                )
                assert copied.read_text() == transcript
            summary_session = "summary-fork"
        if result != "missing_session":
            yield AgentEvent(
                event="session",
                session_id=native_session if result == "wrong_session" else summary_session,
            )
        usage_event = AgentEvent(
            event="raw",
            usage=ProviderUsage(
                provider_profile="claude",
                provider_event_type="result",
                dedupe_key="summary",
                processed_input_tokens=12,
                generated_tokens=7,
            ),
        )
        if result != "oversize":
            yield usage_event
        if result == "runtime_fallback":
            yield AgentEvent(event="runtime_fallback", text="legacy unavailable")
            pytest.fail("summary must not try another runtime")
        elif result == "timeout":
            await asyncio.Event().wait()
        elif result == "stop":
            execution.control.request_pause()
            yield AgentEvent(event="paused")
        elif result == "bug":
            raise ValueError("unexpected programming error")
        elif result == "os_error":
            raise OSError("provider pipe failed")
        elif result in {"error", "auth_error", "stale_error"}:
            yield AgentEvent(
                event="error",
                text={
                    "error": "unavailable",
                    "auth_error": "401 OAuth access token is invalid",
                    "stale_error": "no thread with id: old-native",
                }[result],
            )
        else:
            yield AgentEvent(
                event="answer",
                text="é" * 40000
                if result == "oversize"
                else ""
                if result == "empty"
                else "old session summary",
            )
            if result == "oversize":
                yield usage_event
            yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", provider)
    if result == "timeout":
        monkeypatch.setattr("rcp.limits.AGENT_THREAD_COMPACT_TIMEOUT_SECONDS", 0.1)

    async def run():
        return await provider_switch_seed(
            app.state.launcher,
            new_request,
            execution,
            "transcript tail",
            prepare_scope=prepare,
        )

    success = result in {"summary", "remote_summary"}
    if result == "stop":
        with pytest.raises(TaskPaused):
            asyncio.run(run())
    elif result == "bug":
        with pytest.raises(ValueError, match="unexpected programming error"):
            asyncio.run(run())
    else:
        seed = asyncio.run(run())
        assert ("old session summary" in seed) == success
        assert (seed == "transcript tail") == (not success)
        if success:
            assert asyncio.run(run()) == seed
            # Restart after saving the seed but before publishing its reason.
            store.record_agent_task_receipt(
                task.operation_id,
                "chat_session_resolution",
                {
                    "outcome": "fresh",
                    "reason_code": "provider_switch_tail",
                    "source_operation_id": old.operation_id,
                },
            )
            assert asyncio.run(run()) == seed
    receipt = store.agent_task_session_resolutions([task.operation_id])[task.operation_id]
    assert receipt.payload["reason_code"] == (
        "current_session"
        if result == "same"
        else "provider_switch_compacted"
        if success
        else "provider_switch_tail"
    )
    assert len(preparations) == (0 if result in {"same", "signed_out", "source_missing"} else 1)
    assert len(launches) == (
        0 if result in {"same", "signed_out", "source_missing", "unhidden"} else 1
    )
    assert store.agent_task(task.operation_id).native_session_id == task.native_session_id
    usage = store.agent_usage(task.project_id)
    assert len(usage) == (
        0
        if result
        in {
            "same",
            "signed_out",
            "source_missing",
            "unhidden",
            "wrong_session",
            "missing_transcript",
        }
        else 1
    )
    if usage:
        assert usage[0].operation_id == task.operation_id
        assert usage[0].provider == "claude" and usage[0].generated_tokens == 7
    receipts = store.agent_task_receipts(task.operation_id)
    failures = [r for r in receipts if r.category == "provider_switch_summary_failed"]
    cause = {
        "timeout": "timeout",
        "oversize": "oversized",
        "signed_out": "sign_in",
        "auth_error": "sign_in",
        "missing_session": "session_missing",
        "wrong_session": "session_missing",
        "source_missing": "session_missing",
        "stale_error": "session_missing",
        "missing_transcript": "session_missing",
    }.get(result, "provider_error")
    if success or result in {"same", "stop", "bug"}:
        assert failures == []
    else:
        assert len(failures) == 1 and failures[0].payload == {"cause": cause}
        assert failures[0].tier == "diagnostic"
    assert len([r for r in receipts if r.category == "provider_usage"]) == len(usage)
    assert list(stage.iterdir()) == []
