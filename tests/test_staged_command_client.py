from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from rcp.agents import command_mailbox as command_mailbox_module
from rcp.agents.command_mailbox import (
    COMMAND_MAILBOX_MAX_REQUEST_BYTES,
    serve_command_mailbox,
    stage_command_mailbox,
)
from rcp.agents.command_protocol import CommandResponse, staged_command_client_source
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.workspace_mailbox import RunStageMailbox, clear_turn_handoff_files


async def _run_client(staged, *arguments: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *staged.client_argv(*arguments),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode("utf-8")


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_mailbox_setup_failure_expires_credential_and_preserves_original_error(
    tmp_path, monkeypatch, cleanup_fails
) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    for name in ("patch.json", "watch.json", "messages.json"):
        (workspace / name).write_text(f"retained {name}", encoding="utf-8")
    issued = []
    original_issue = command_mailbox_module.CommandTurnCredential.issue

    def capture_issue(cls, identity):
        del cls
        credential = original_issue(identity)
        issued.append(credential)
        return credential

    monkeypatch.setattr(
        command_mailbox_module.CommandTurnCredential,
        "issue",
        classmethod(capture_issue),
    )
    original_write = RunStageMailbox.write_text

    def fail_after_write(self, name, content):
        original_write(self, name, content)
        if name.startswith("rcp-command-"):
            raise RuntimeError("credential staging failed")

    monkeypatch.setattr(RunStageMailbox, "write_text", fail_after_write)
    if cleanup_fails:
        original_clear = command_mailbox_module._clear_command_state
        clear_calls = 0

        def fail_cleanup(mailbox):
            nonlocal clear_calls
            clear_calls += 1
            if clear_calls == 2:
                raise OSError("cleanup failed")
            return original_clear(mailbox)

        monkeypatch.setattr(command_mailbox_module, "_clear_command_state", fail_cleanup)

    with pytest.raises(RuntimeError, match="credential staging failed"):
        stage_command_mailbox(
            local_stage=workspace,
            remote_stage=None,
            campaign_id="campaign",
            task_id="task",
            turn_id="turn",
        )

    assert len(issued) == 1
    assert issued[0].expired
    if not cleanup_fails:
        assert not any(
            path.name.startswith(("rcp-command-", ".rcp-command-", ".rcp-mailbox-"))
            for path in workspace.iterdir()
        )
    assert {
        name: (workspace / name).read_text(encoding="utf-8")
        for name in ("patch.json", "watch.json", "messages.json")
    } == {name: f"retained {name}" for name in ("patch.json", "watch.json", "messages.json")}


@pytest.mark.asyncio
async def test_staged_client_and_local_mailbox_preserve_protocol_shapes_and_exit_values(
    tmp_path,
) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    (workspace / "patch.json").write_text('{"ops":[]}\n', encoding="utf-8")
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id="campaign",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )
    assert Path(staged.client_path).read_text(encoding="utf-8") == staged_command_client_source()
    imports: set[str] = set()
    for node in ast.walk(ast.parse(staged_command_client_source())):
        if isinstance(node, ast.Import):
            imports.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module.partition(".")[0])
    assert imports <= sys.stdlib_module_names
    assert "from rcp" not in staged_command_client_source()
    assert "http" not in staged_command_client_source().casefold()
    handled: list[str] = []

    def handler(request, identity):
        assert identity.campaign_id == "campaign"
        assert identity.task_id == "task"
        assert identity.turn_id == "turn"
        handled.append(request.verb)
        if request.verb == "validate":
            assert request.arguments.patch == '{"ops":[]}\n'
            status = "ok"
            message = None
        elif request.verb == "status":
            worker_id = request.arguments.worker_id
            status = worker_id if worker_id in {"invalid", "unavailable"} else "ok"
            message = None if status == "ok" else f"The requested result is {status}."
        else:
            assert request.verb == "finish"
            assert request.arguments.model_dump() == {}
            assert request.idempotency_key == "conclude-once"
            status = "ok"
            message = None
        return CommandResponse(
            request_id=request.request_id,
            status=status,
            message=message,
            result={"observed": request.verb},
        )

    stop = asyncio.Event()
    server = asyncio.create_task(
        serve_command_mailbox(staged=staged, handler=handler, stop=stop, poll_seconds=0.01)
    )
    await asyncio.sleep(0)
    validate_code, validate_output = await _run_client(
        staged, "validate", str(workspace / "patch.json")
    )
    ok_code, ok_output = await _run_client(staged, "status")
    invalid_code, invalid_output = await _run_client(staged, "status", "--worker-id", "invalid")
    unavailable_code, unavailable_output = await _run_client(
        staged, "status", "--worker-id", "unavailable"
    )
    finish_code, finish_output = await _run_client(
        staged,
        "finish",
        "--key",
        "conclude-once",
    )
    stop.set()
    await server

    assert validate_code == 0
    assert json.loads(validate_output)["status"] == "valid"
    assert (ok_code, invalid_code, unavailable_code, finish_code) == (0, 1, 2, 0)
    assert json.loads(ok_output)["status"] == "ok"
    assert json.loads(invalid_output)["status"] == "invalid"
    assert json.loads(unavailable_output)["status"] == "unavailable"
    assert json.loads(finish_output)["status"] == "ok"
    assert handled == ["validate", "status", "status", "status", "finish"]
    request_files = sorted(workspace.glob("*.request.json"))
    response_files = sorted(workspace.glob("*.response.json"))
    assert len(request_files) == len(response_files) == 5
    for request_path in request_files:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert request == {
            "version": 1,
            "mailbox_id": staged.credential.mailbox_id,
            "request_id": request["request_id"],
            "credential": request["credential"],
            "verb": request["verb"],
            "idempotency_key": request["idempotency_key"],
            "arguments": request["arguments"],
        }
        assert len(request["request_id"]) == 32
        assert len(request["credential"]) == 64

    assert staged.credential.expired
    with pytest.raises(RuntimeError, match="expired"):
        staged.credential.document()
    with pytest.raises(RuntimeError, match="exactly one turn"):
        staged.credential.activate()

    staged.cleanup()
    expired_code, expired_output = await _run_client(staged, "status")
    assert expired_code == 1
    assert "credential" in expired_output
    assert "unavailable or not a regular file" in expired_output


@pytest.mark.asyncio
async def test_non_campaign_credential_rejects_mutation_before_handler(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id=None,
        task_id="validator-task",
        turn_id="validator-turn",
        timeout_seconds=2,
    )
    handled = False

    def handler(request, identity):
        nonlocal handled
        handled = True
        return CommandResponse(request_id=request.request_id, status="ok")

    stop = asyncio.Event()
    server = asyncio.create_task(
        serve_command_mailbox(staged=staged, handler=handler, stop=stop, poll_seconds=0.01)
    )
    await asyncio.sleep(0)
    code, output = await _run_client(
        staged,
        "message",
        "--key",
        "message-once",
        "This must not be dispatched.",
    )
    stop.set()
    await server

    assert code == 1
    assert json.loads(output)["status"] == "invalid"
    assert "campaign-bound credential" in output
    assert not handled


@pytest.mark.asyncio
async def test_staged_client_rejects_oversized_patch_before_writing_request(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    patch = workspace / "patch.json"
    with patch.open("wb") as stream:
        stream.truncate(COMMAND_MAILBOX_MAX_REQUEST_BYTES + 1)
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id="campaign",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )

    code, output = await _run_client(staged, "validate", str(patch))

    assert code == 1
    assert (
        f"patch.json exceeds the {COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
        in output
    )
    assert not list(workspace.glob("*.request.json"))
    staged.cleanup()


@pytest.mark.asyncio
async def test_staged_client_rejects_oversized_status_id_before_writing_request(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id="campaign",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )

    code, output = await _run_client(staged, "status", "--worker-id", "x" * 201)

    assert code == 1
    assert "worker id must be at most 200 characters" in output
    assert not list(workspace.glob("*.request.json"))
    staged.cleanup()


def test_remote_mailbox_enforces_byte_limit_before_transfer(tmp_path, monkeypatch) -> None:
    root = tmp_path / "rcp-run.test"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    request = workspace / "request.json"
    request.write_text("abcde", encoding="utf-8")
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    completed: list[subprocess.CompletedProcess[bytes]] = []

    def run_remote_script(arguments, *, input_data=None):
        result = subprocess.run(
            arguments,
            capture_output=True,
            input=input_data,
            check=False,
        )
        completed.append(result)
        return result

    monkeypatch.setattr(stage, "_ssh_bytes", run_remote_script)
    mailbox = RunStageMailbox.for_stage(local_stage=None, remote_stage=stage)

    with pytest.raises(ValueError, match=r"mailbox file exceeds 4 bytes: request.json"):
        mailbox.read_text("request.json", max_bytes=4)
    assert completed[-1].stdout == b""

    request.write_text("abcd", encoding="utf-8")
    assert mailbox.read_text("request.json", max_bytes=4) == "abcd"
    assert stage.read_workspace_text("request.json", max_bytes=4) == "abcd"


def test_turn_handoff_cleanup_includes_messages_and_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    mailbox = RunStageMailbox.for_stage(local_stage=workspace, remote_stage=None)
    for name in ("patch.json", "watch.json", "messages.json"):
        (workspace / name).write_text("stale", encoding="utf-8")

    clear_turn_handoff_files(mailbox)
    assert not any(
        (workspace / name).exists() for name in ("patch.json", "watch.json", "messages.json")
    )

    (workspace / "patch.json").write_text("stale", encoding="utf-8")
    (workspace / "watch.json").write_text("stale", encoding="utf-8")
    (workspace / "messages.json").mkdir()
    with pytest.raises(ValueError, match="unsafe directory"):
        clear_turn_handoff_files(mailbox)
    assert (workspace / "messages.json").is_dir()
