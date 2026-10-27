from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rcp.providers import ProviderTurnRequest, profile_for, remote_bundle
from rcp.providers.claude.remote import ClaudeSessionFormat, claude_project_directory
from rcp.providers.codex.app_server import CodexAppServerRuntime


def _request(tmp_path: Path, provider: str) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        prompt="Continue on the new graph target.",
        binary=provider,
        cwd=tmp_path / "new-stage",
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[],
        write_dirs=[tmp_path / "changes"],
        write_scope=None,
        capability="paper_suggest",
        provider_version="2.200.0",
        fork_session_id="source-session",
    )


@pytest.mark.parametrize("provider", ["codex", "claude", "opencode"])
def test_native_fork_commands_and_source_identity_refusal(tmp_path: Path, provider: str) -> None:
    profile = profile_for(provider)
    request = _request(tmp_path, provider)
    turn = profile.runtime(profile.legacy_runtime_id).turn(request)
    if provider == "codex":
        assert turn.command[:3] == ["codex", "exec", "fork"]
        assert turn.command[-2:] == ["source-session", "-"]
        assert "--cd" not in turn.command and "--sandbox" not in turn.command
        session = {"type": "thread.started", "thread_id": "source-session"}
    elif provider == "claude":
        assert turn.command[turn.command.index("--resume") + 1] == "source-session"
        assert "--fork-session" in turn.command
        session = {"type": "system", "subtype": "init", "session_id": "source-session"}
    else:
        assert turn.command[turn.command.index("--session") + 1] == "source-session"
        assert "--fork" in turn.command
        session = {"type": "step_start", "sessionID": "source-session"}
    step = turn.receive_line(json.dumps(session))
    assert step.complete and step.stop_process
    assert any(event.event == "error" for event in step.events)
    assert not any(event.event == "session" for event in step.events)


@pytest.mark.parametrize("returned_session", ["forked-session", "source-session"])
def test_app_server_forks_without_history_payload_and_requires_new_identity(
    tmp_path: Path, returned_session: str
) -> None:
    turn = CodexAppServerRuntime().turn(_request(tmp_path, "codex"))
    turn.receive_line(json.dumps({"id": 1, "result": {}}))
    step = turn.receive_line(json.dumps({"id": 2, "result": {"config": {}}}))
    (request,) = [json.loads(item) for item in step.outgoing]
    assert request["method"] == "thread/fork"
    assert request["params"]["threadId"] == "source-session"
    assert request["params"]["excludeTurns"] is True
    assert request["params"]["cwd"] == str(tmp_path / "new-stage")
    assert "serviceName" not in request["params"]
    step = turn.receive_line(
        json.dumps(
            {
                "id": 3,
                "result": {
                    "thread": {"id": returned_session},
                    "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly"},
                    "activePermissionProfile": {"id": "rcp_paper"},
                },
            }
        )
    )
    if returned_session == "source-session":
        assert step.complete
        assert any(event.event == "error" for event in step.events)
        assert not step.outgoing
    else:
        assert step.delivers_prompt
        assert step.events[0].session_id == returned_session
        assert json.loads(step.outgoing[0])["params"]["threadId"] == returned_session


@pytest.mark.parametrize("shipped", [False, True])
def test_claude_fork_prepares_new_cwd_without_changing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shipped: bool
) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    source_cwd = (tmp_path / "source.stage").resolve()
    target_cwd = (tmp_path / "fork.stage").resolve()
    session_id = str(uuid.uuid4())
    source = config / "projects" / claude_project_directory(source_cwd) / f"{session_id}.jsonl"
    target = config / "projects" / claude_project_directory(target_cwd) / source.name
    transcript = "\n".join(
        json.dumps({"sessionId": session_id, "cwd": str(source_cwd), "type": role})
        for role in ("user", "assistant")
    )
    source.parent.mkdir(parents=True)
    source.write_text(transcript)

    def interrupt_copy(reader, writer):
        writer.write(reader.readline())  # A valid prefix must never become the snapshot.
        raise OSError("copy interrupted")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(shutil, "copyfileobj", interrupt_copy)
        with pytest.raises(OSError):
            ClaudeSessionFormat().prepare_session_fork(
                source_session_id=session_id, source_cwd=source_cwd, cwd=target_cwd
            )
    assert not target.exists()
    assert list(target.parent.iterdir()) == []
    assert source.read_text() == transcript
    if shipped:
        driver = (
            "SESSION_FORMATS['claude'].prepare_session_fork("
            f"source_session_id={session_id!r}, source_cwd=Path({str(source_cwd)!r}), "
            f"cwd=Path({str(target_cwd)!r}))"
        )
        subprocess.run([sys.executable, "-I", "-S", "-c", remote_bundle(driver)], check=True)
    else:
        ClaudeSessionFormat().prepare_session_fork(
            source_session_id=session_id, source_cwd=source_cwd, cwd=target_cwd
        )
    assert target.read_text() == transcript
    assert source.read_text() == transcript
    assert target.stat().st_ino != source.stat().st_ino
    source.write_text(transcript + "\n" + transcript)
    ClaudeSessionFormat().prepare_session_fork(
        source_session_id=session_id, source_cwd=source_cwd, cwd=target_cwd
    )
    assert target.read_text() == transcript
    target.write_text("a conflicting session")
    with pytest.raises(ValueError):
        ClaudeSessionFormat().prepare_session_fork(
            source_session_id=session_id, source_cwd=source_cwd, cwd=target_cwd
        )
    assert source.read_text() == transcript + "\n" + transcript


@pytest.mark.asyncio
async def test_launcher_prepares_claude_fork_before_sending_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from rcp.agents import AgentLauncher

    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    source_cwd = (tmp_path / "source").resolve()
    target_cwd = (tmp_path / "target").resolve()
    target_cwd.mkdir()
    source_id = str(uuid.uuid4())
    fork_id = str(uuid.uuid4())
    source = config / "projects" / claude_project_directory(source_cwd) / f"{source_id}.jsonl"
    target = config / "projects" / claude_project_directory(target_cwd) / source.name
    transcript = json.dumps({"sessionId": source_id, "cwd": str(source_cwd), "type": "user"})
    source.parent.mkdir(parents=True)
    source.write_text(transcript)
    executable = tmp_path / "fake-claude"
    capture = tmp_path / "argv.json"
    executable.write_text(
        f"""#!{sys.executable}
import json, sys
from pathlib import Path
assert Path({str(target)!r}).read_text() == {transcript!r}
assert Path.cwd() == Path({str(target_cwd)!r})
Path({str(capture)!r}).write_text(json.dumps(sys.argv))
initial = json.loads(sys.stdin.readline())
print(json.dumps({{"type":"system", "subtype":"init", "session_id":{fork_id!r}}}), flush=True)
print(json.dumps({{"type":"result", "subtype":"success", "result":"Fork ready", "user_message_uuids":[initial["uuid"]]}}), flush=True)
"""
    )
    executable.chmod(0o755)
    launcher = AgentLauncher()
    launcher.readiness = lambda *_args, **_kwargs: SimpleNamespace(
        installed=True, authenticated=True, binary_path=str(executable), version="2.1.307"
    )
    with pytest.raises(ValueError):  # The launch contract: a fork never also resumes.
        await anext(
            launcher.stream(
                "claude",
                "Continue",
                cwd=target_cwd,
                session_id=source_id,
                fork_session_id=source_id,
                fork_source_cwd=str(source_cwd),
                write_dirs=[tmp_path / "changes"],
                capability="paper_suggest",
            )
        )
    events = [
        event
        async for event in launcher.stream(
            "claude",
            "Continue",
            cwd=target_cwd,
            fork_session_id=source_id,
            fork_source_cwd=str(source_cwd),
            write_dirs=[tmp_path / "changes"],
            capability="paper_suggest",
        )
    ]
    assert events[-1].event == "done", [(event.event, event.text) for event in events]
    assert [event.session_id for event in events if event.event == "session"] == [fork_id]
    command = json.loads(capture.read_text())
    assert command[command.index("--resume") + 1] == source_id
    assert "--fork-session" in command
    assert source.read_text() == transcript


@pytest.mark.parametrize("tail,suffix", [("x", "nkcp5b"), ("🔬", "k6r6ve")])
def test_claude_long_project_key_matches_native_utf16_hash(tail: str, suffix: str) -> None:
    from rcp.providers.claude.remote import claude_project_directory

    cwd = Path("/tmp/" + "x" * 200 + tail)
    assert claude_project_directory(cwd) == "-tmp-" + "x" * 195 + "-" + suffix
