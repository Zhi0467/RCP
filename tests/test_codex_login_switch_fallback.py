from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent, AgentLauncher, AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.runs.codex_resume import (
    REFUSAL,
    RETRY,
    record_refusal,
    retry_refused_resume,
    start_context,
)
from rcp.runs.shared import _ProviderOutcome, _sse, _stream_agent_events
from rcp.service import RunRequest
from rcp.storage.models import LoginKey

from .helpers import wait_for_task, wait_until
from .test_background import _store
from .test_background_policy_matrix import _chat_request, _record
from .test_codex_app_server import _accounting_turn
from .test_provider_login_resolver import _task
from .test_provider_login_resolver import accounts as accounts


def _rollout(home: Path, session: str) -> Path:
    path = home / "sessions" / "2026" / "10" / "30" / f"rollout-example-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["codex.exec-json.v1", "codex.app-server-stdio.v1"])
@pytest.mark.parametrize("host", ["", "worker.invalid"])
@pytest.mark.parametrize("fork", [False, True])
async def test_refused_native_turn_records_host_transcript(
    accounts, tmp_path, monkeypatch, runtime, host, fork
):
    from rcp.providers.codex.profile import CodexProfile

    home = tmp_path / "machine-codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    session = str(uuid4())
    transcript = _rollout(home, session)
    key = LoginKey("codex", host, "")
    accounts.store.mark_provider_login_verified(key, member_id="member", detail="ok")
    # Pre-existing writer-shaped rows have no fallback metadata.
    _task(accounts.store, "refused", key, member=None, session=session)
    execution = AgentTaskExecution("refused", accounts.store, AgentProcessControl())
    execution.runtime_id = runtime
    ssh_calls = []
    real_run = subprocess.run

    def remote_run(command, **kwargs):
        assert command[0] == "ssh" and command[-2] == host
        ssh_calls.append(command)
        # Execute the actual shipped discovery script against a disposable host
        # store. No network or live remote placement is involved.
        return real_run(shlex.split(command[-1]), **kwargs)

    if host:
        monkeypatch.setattr("rcp.runs.codex_resume.subprocess.run", remote_run)
    error = {"message": "Rejected transcript", "code": "invalid_encrypted_content"}
    if runtime == "codex.exec-json.v1":
        value = {"type": "turn.failed", "error": error}
        event = CodexProfile().decode_event(value, json.dumps(value))
    else:
        turn = _accounting_turn(tmp_path, resumed=True)
        step = turn.receive_line(json.dumps({"method": "error", "params": {"error": error}}))
        event = step.events[0]
    launched = []

    async def stream(_provider, prompt, **kwargs):
        launched.append((prompt, kwargs))
        yield AgentEvent(event=event.event, text=event.text)

    request = RunRequest(provider="codex", message="Continue", mode="discuss")
    if fork:
        request = request.model_copy(
            update={
                "fork_session_id": session,
                "fork_source_host": host,
                "fork_source_cwd": str(tmp_path),
            }
        )
    outcome = _ProviderOutcome()
    frames = [
        frame
        async for frame in _stream_agent_events(
            cast(AgentLauncher, SimpleNamespace(stream=stream)),
            request,
            "full task contract",
            workspace=tmp_path,
            session_id=None if fork else session,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host=host,
            execution=execution,
            remote_stage=None,
            capability="discuss",
            outcome=outcome,
            binary=None,
        )
    ]
    assert outcome.failed and frames
    assert len(launched) == 1
    assert launched[0][1]["runtime_id"] == runtime
    assert launched[0][1]["host"] == host
    assert launched[0][1]["fork_session_id" if fork else "session_id"] == session
    receipts = [r for r in accounts.store.agent_task_receipts("refused") if r.category == REFUSAL]
    assert len(receipts) == 1
    assert receipts[0].payload == {
        "reason": "login_switch",
        "native_session_id": session,
        "transcript_path": str(transcript),
        "host": host,
    }
    assert bool(ssh_calls) == bool(host)


@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("stale_queued_snapshot", [False, True])
def test_refusal_retries_once_through_fresh_handoff(tmp_path, fork, stale_queued_snapshot):
    store = _store(tmp_path)
    session = str(uuid4())
    transcript = _rollout(tmp_path / "codex", session)
    stage = tmp_path / "old-stage"
    stage.mkdir()
    request = _chat_request(mode="discuss")
    previous = _record(
        store,
        operation_id="refused",
        kind="project_chat",
        request=request,
        status="failed",
        native_session_id=session,
        stage_root=str(stage),
    )
    if fork:
        # The old writer persisted a fork source in request_json. Retry must drop
        # it even though normal chat admission would carry it until a new session.
        with store.connection() as connection:
            old_request = dict(
                previous.request,
                fork_session_id=session,
                fork_source_host="",
                fork_source_cwd=str(stage),
            )
            connection.execute(
                "UPDATE graph_runs SET request_json=? WHERE operation_id=?",
                (json.dumps(old_request), previous.operation_id),
            )
        previous = store.agent_task(previous.operation_id)
    store.record_agent_task_receipt(
        previous.operation_id,
        REFUSAL,
        {
            "reason": "login_switch",
            "native_session_id": session,
            "transcript_path": str(transcript),
            "host": "",
        },
    )
    calls = []

    async def stream(_project, _kind, fresh, execution):
        calls.append((fresh, execution))
        # A fresh session that fails must not cause another automatic attempt.
        store.record_agent_task_receipt(
            execution.operation_id,
            REFUSAL,
            {
                "reason": "login_switch",
                "native_session_id": session,
                "transcript_path": str(transcript),
                "host": "",
            },
        )
        yield _sse(AgentEvent(event="error", text="invalid_encrypted_content"))

    tasks = BackgroundAgentTasks(store, stream)
    snapshot = (
        previous.model_copy(update={"status": "queued"}) if stale_queued_snapshot else previous
    )
    retry_refused_resume(tasks, snapshot)
    wait_until(lambda: calls, detail="fresh retry was not dispatched")
    fresh_request, execution = calls[0]
    child = wait_for_task(store, execution.operation_id, expect="failed")
    assert execution.continuation == "handoff"
    assert child.parent_operation_id == previous.operation_id
    assert child.native_session_id is None and child.stage_root is None
    assert fresh_request.session_id is None and fresh_request.fork_session_id is None
    assert fresh_request.message == request.message
    assert fresh_request.run_truth_scope == request.run_truth_scope
    assert json.dumps(str(transcript)) in start_context(execution, "")
    with pytest.raises(ValueError):
        start_context(execution, "other.invalid")
    retry_refused_resume(tasks, previous)
    retry_refused_resume(tasks, child)
    assert len(calls) == 1
    assert store.agent_task_has_receipt(previous.operation_id, RETRY)
    assert not store.agent_task_has_receipt(child.operation_id, RETRY)
    assert any(
        r.category == "chat_session_resolution" and r.payload.get("reason_code") == "login_switch"
        for r in store.agent_task_receipts(child.operation_id)
    )


@pytest.mark.parametrize(
    "native_session, error",
    [
        (None, "invalid_encrypted_content"),
        ("source", "network connection lost"),
        ("source", "The encrypted content for item rs_example could not be verified"),
    ],
)
def test_fresh_or_unrelated_failure_never_requests_fallback(accounts, native_session, error):
    key = LoginKey("codex", "", "")
    _task(accounts.store, "ordinary", key, session=native_session)
    execution = AgentTaskExecution("ordinary", accounts.store, AgentProcessControl())
    record_refusal(execution, provider="codex", session_id=native_session, host="", error=error)
    assert not accounts.store.agent_task_has_receipt("ordinary", REFUSAL)


def test_fresh_handoff_rebuilds_discuss_master_and_contract(tmp_path, monkeypatch):
    from rcp.runs.tasks.discuss import stream_discuss_run

    from .helpers import create_named_app
    from .test_api import ScriptedLauncher, _chat_task_execution
    from .test_work_agent_io import _isolated_manifest

    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_named_app(str(_isolated_manifest(tmp_path)), data_dir=tmp_path / "data")
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        chat_scope="project",
        chat_id="fallback-chat",
        message="Continue the evidence review",
        mode="discuss",
        run_truth_scope=["repo-a"],
    )
    original = _chat_task_execution(app, request, "refused-native")
    store = original.store
    session = str(uuid4())
    transcript = _rollout(tmp_path / "machine-codex", session)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='failed', phase='failed', "
            "native_session_id=?, error='invalid_encrypted_content' WHERE operation_id=?",
            (session, original.operation_id),
        )
    store.record_agent_task_receipt(
        original.operation_id,
        REFUSAL,
        {
            "reason": "login_switch",
            "native_session_id": session,
            "transcript_path": str(transcript),
            "host": "",
        },
    )
    launcher = ScriptedLauncher([{}], message="Review complete")
    children = []

    async def stream(_project, _kind, fresh, execution):
        children.append(execution.operation_id)
        async for frame in stream_discuss_run(
            app.state.service, launcher, fresh, tmp_path / "data", execution
        ):
            yield frame

    tasks = BackgroundAgentTasks(store, stream)
    retry_refused_resume(tasks, store.agent_task(original.operation_id))
    wait_until(lambda: children, detail="Discuss handoff was not dispatched")
    child = wait_for_task(store, children[0], expect="succeeded")
    assert launcher.resumed_sessions == [None]
    assert child.native_session_id == launcher.native_session_id
    assert json.dumps(str(transcript)) in launcher.prompts[0]
    master = store.agent_task_contract(child.operation_id, "session_master")
    assert master and master in launcher.input_snapshots[0].values()
    assert store.agent_task_contract(original.operation_id, "session_master") is None
    assert child.stage_root and Path(child.stage_root).is_dir()
    assert any(request.message in value for value in launcher.input_snapshots[0].values())
    launches = [
        r for r in store.agent_task_receipts(child.operation_id) if r.category == "agent_launch"
    ]
    assert launches[0].payload["resumed"] is False
    assert launches[0].payload["continuation_cause"] == "handoff"


@pytest.mark.parametrize("guard", ["ancestor_retry", "origin_bound_edit", "legacy_origin_edit"])
def test_refusal_retry_respects_lineage_and_protected_origin(accounts, guard):
    store = accounts.store
    key = LoginKey("codex", "", "")
    for operation_id in ("first-refusal", "transport-retry", "current-refusal"):
        _task(store, operation_id, key, member=None)
    edit = None
    if guard == "origin_bound_edit":
        edit = {"artifact_id": "artifact", "origin_bound": True}
    elif guard == "legacy_origin_edit":
        edit = {"artifact_id": "artifact"}
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='failed', phase='failed'")
        if guard == "ancestor_retry":
            connection.execute(
                "UPDATE graph_runs SET parent_operation_id='first-refusal' "
                "WHERE operation_id='transport-retry'"
            )
            connection.execute(
                "UPDATE graph_runs SET parent_operation_id='transport-retry' "
                "WHERE operation_id='current-refusal'"
            )
        else:
            connection.execute(
                "UPDATE graph_runs SET kind='artifact_edit', request_json=? "
                "WHERE operation_id='current-refusal'",
                (json.dumps({"provider": "codex", "artifact_edit": edit}),),
            )
    if guard == "ancestor_retry":
        store.record_agent_task_receipt("first-refusal", RETRY, {"attempt": 1})
    store.record_agent_task_receipt("current-refusal", REFUSAL, {"reason": "login_switch"})

    def forbidden_retry(*_args, **_kwargs):
        pytest.fail("A guarded transcript refusal dispatched another turn")

    tasks = cast(BackgroundAgentTasks, SimpleNamespace(store=store, retry=forbidden_retry))
    current = store.agent_task("current-refusal")
    assert current is not None and current.can_retry
    retry_refused_resume(tasks, current)
    assert not store.agent_task_has_receipt(current.operation_id, RETRY)
    skipped = [
        r
        for r in store.agent_task_receipts(current.operation_id)
        if r.category == "codex_fresh_session_retry_failed"
    ]
    if edit is not None:
        assert len(skipped) == 1
        assert skipped[0].payload["reason"] == "origin_bound_artifact_edit"
    else:
        assert skipped == []
