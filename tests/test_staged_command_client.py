from __future__ import annotations

import ast
import asyncio
import json
import os
import shlex
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
from rcp.agents.command_protocol import (
    CommandResponse,
    staged_command_broker_source,
    staged_command_client_source,
)
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
    original_stage_input = RunStageMailbox.stage_text_input

    def fail_broker_stage(self, name, content):
        if name.startswith("rcp-command-broker-"):
            raise RuntimeError("broker staging failed")
        return original_stage_input(self, name, content)

    monkeypatch.setattr(RunStageMailbox, "stage_text_input", fail_broker_stage)
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

    with pytest.raises(RuntimeError, match="broker staging failed"):
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


def test_staged_broker_is_stdlib_only_and_packaged_for_the_desktop() -> None:
    source = staged_command_broker_source()
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module.partition(".")[0])
    assert imports <= sys.stdlib_module_names
    assert "from rcp" not in source
    root = Path(__file__).resolve().parents[1]
    sidecar = (root / "packaging" / "rcp_backend.spec").read_text(encoding="utf-8")
    hook = (root / "packaging" / "hooks" / "validate_frozen_resources.py").read_text(
        encoding="utf-8"
    )
    assert "STAGED_COMMAND_BROKER" in sidecar
    assert "staged_command_broker_source" in hook


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
    broker_token = staged.credential.token
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
    assert staged.invocation_gate is not None
    async with staged.invocation_gate.serve_current_session():
        server = asyncio.create_task(
            serve_command_mailbox(
                staged=staged,
                handler=handler,
                stop=stop,
                poll_seconds=0.01,
                invocation_gate=staged.invocation_gate,
            )
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
        assert request["credential"] != broker_token

    assert staged.credential.expired
    with pytest.raises(RuntimeError, match="broker-only"):
        staged.credential.document()
    with pytest.raises(RuntimeError, match="exactly one turn"):
        staged.credential.activate()

    staged.cleanup()
    expired_code, expired_output = await _run_client(staged, "status")
    assert expired_code == 2
    assert "broker is unavailable" in expired_output


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
async def test_campaign_broker_signature_cannot_authorize_a_modified_request(tmp_path) -> None:
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
    assert staged.invocation_gate is not None
    assert staged.credential_path is None
    assert "--credential" not in staged.client_argv()
    assert not list(workspace.glob("*.credential.json"))
    token = staged.credential.token
    assert token not in staged.client_command()
    assert all(token not in path.read_text(encoding="utf-8") for path in workspace.rglob("*.py"))
    handled = 0

    def handler(request, _identity):
        nonlocal handled
        handled += 1
        return CommandResponse(request_id=request.request_id, status="ok")

    stop = asyncio.Event()
    async with staged.invocation_gate.serve_current_session():
        server = asyncio.create_task(
            serve_command_mailbox(
                staged=staged,
                handler=handler,
                stop=stop,
                poll_seconds=0.01,
                invocation_gate=staged.invocation_gate,
            )
        )
        try:
            code, _output = await _run_client(staged, "status")
            assert code == 0
            original_path = next(workspace.glob("*.request.json"))
            modified = json.loads(original_path.read_text(encoding="utf-8"))
            modified_id = "f" * 32
            modified["request_id"] = modified_id
            modified["arguments"] = {"worker_id": "different-worker"}
            modified_path = workspace / (
                f"rcp-command-{staged.credential.mailbox_id}-{modified_id}.request.json"
            )
            modified_path.write_text(json.dumps(modified), encoding="utf-8")
            response_path = modified_path.with_name(
                modified_path.name.removesuffix(".request.json") + ".response.json"
            )
            for _ in range(200):
                if response_path.is_file():
                    break
                await asyncio.sleep(0.01)
            assert response_path.is_file()
            response = json.loads(response_path.read_text(encoding="utf-8"))
            assert response["status"] == "invalid"
            assert "credential is invalid" in response["message"]
            assert handled == 1
        finally:
            stop.set()
            await server
    staged.cleanup()


@pytest.mark.asyncio
async def test_detached_prior_turn_process_cannot_command_reused_stage(tmp_path) -> None:
    workspace = tmp_path / "reused-stage"
    workspace.mkdir()
    first = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id="campaign",
        task_id="first-task",
        turn_id="first-turn",
        timeout_seconds=2,
    )
    stale_instruction = workspace / "stale-instruction.json"
    stale_result_path = workspace / "stale-result.json"
    stale_script = (
        "import json,os,subprocess,sys,time\n"
        "instruction,result_path=sys.argv[1:3]\n"
        "deadline=time.monotonic()+10\n"
        "while not os.path.isfile(instruction):\n"
        "  if time.monotonic()>=deadline: raise SystemExit(70)\n"
        "  time.sleep(0.01)\n"
        "with open(instruction,encoding='utf-8') as stream: argv=json.load(stream)\n"
        "result=subprocess.run(argv,capture_output=True,text=True,check=False)\n"
        "with open(result_path,'w',encoding='utf-8') as stream:\n"
        "  json.dump({'code':result.returncode,'stdout':result.stdout},stream)\n"
    )
    stale = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        stale_script,
        str(stale_instruction),
        str(stale_result_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert stale.pid is not None
    assert os.getsid(stale.pid) != os.getsid(0)
    first.cleanup()

    second = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id="campaign",
        task_id="second-task",
        turn_id="second-turn",
        timeout_seconds=2,
    )
    assert second.invocation_gate is not None
    handled: list[str] = []

    def handler(request, _identity):
        handled.append(request.verb)
        return CommandResponse(request_id=request.request_id, status="ok")

    provider_script = (
        "import json,os,subprocess,sys,time\n"
        "instruction,result_path,client_json=sys.argv[1:4]\n"
        "temporary=instruction+'.tmp'\n"
        "with open(temporary,'w',encoding='utf-8') as stream: stream.write(client_json)\n"
        "os.replace(temporary,instruction)\n"
        "deadline=time.monotonic()+10\n"
        "while not os.path.isfile(result_path):\n"
        "  if time.monotonic()>=deadline: raise SystemExit(71)\n"
        "  time.sleep(0.01)\n"
        "with open(result_path,encoding='utf-8') as stream: stale=json.load(stream)\n"
        "current=subprocess.run(json.loads(client_json),capture_output=True,text=True,"
        "check=False,start_new_session=True)\n"
        "print(json.dumps({'stale':stale,'current':{'code':current.returncode,"
        "'stdout':current.stdout}}),flush=True)\n"
    )
    provider_command = [
        sys.executable,
        "-c",
        provider_script,
        str(stale_instruction),
        str(stale_result_path),
        json.dumps(list(second.client_argv("status"))),
    ]
    stop = asyncio.Event()
    server = asyncio.create_task(
        serve_command_mailbox(
            staged=second,
            handler=handler,
            stop=stop,
            poll_seconds=0.01,
            invocation_gate=second.invocation_gate,
        )
    )
    process = await asyncio.create_subprocess_exec(
        *second.invocation_gate.wrap_command(provider_command),
        cwd=workspace,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(second.invocation_gate.bootstrap(b"")),
                timeout=15,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        await stale.wait()
    finally:
        if stale.returncode is None:
            stale.terminate()
            await stale.wait()
        stop.set()
        await server

    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    lines = stdout.decode("utf-8").splitlines()
    assert lines[0] == second.invocation_gate.ready_line
    result = json.loads(lines[-1])
    assert result["stale"]["code"] == 1, result
    assert "outside the current provider invocation" in result["stale"]["stdout"]
    assert result["current"]["code"] == 0, result
    assert json.loads(result["current"]["stdout"])["status"] == "ok"
    assert handled == ["status"]
    assert len(list(workspace.glob("*.request.json"))) == 1
    assert "--credential" not in shlex.split(second.client_command())
    second.cleanup()


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


@pytest.mark.asyncio
async def test_every_mutating_verb_survives_the_real_broker_round_trip(tmp_path) -> None:
    """Sign the bytes the client wrote, not the model they validate into.

    ``watch-graph`` sorts ``status_in`` during validation, so a signature taken
    over the validated model stopped matching whenever the agent wrote its
    statuses in any order but alphabetical. Every other mutating verb shares the
    signing path, so they are exercised here together rather than one at a time.
    """

    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id="campaign",
        task_id="task",
        turn_id="turn",
        timeout_seconds=10,
    )
    assert staged.invocation_gate is not None
    seen: list[tuple[str, dict]] = []

    def handler(request, _identity):
        seen.append((request.verb, request.arguments.model_dump(mode="json")))
        return CommandResponse(request_id=request.request_id, status="ok")

    # Deliberately not alphabetical: this is the ordering that used to be refused.
    condition = json.dumps({"node_id": "hyp-3", "status_in": ["supported", "refuted"]})
    calls = [
        ("spawn", "--seat-node", "exp/run", "--instruction", "measure it", "--key", "k-spawn"),
        ("pause", "worker-1", "--key", "k-pause"),
        ("resume", "worker-1", "--key", "k-resume"),
        ("stop", "worker-1", "--key", "k-stop"),
        ("message", "keep going", "--recipient", "worker-1", "--key", "k-message"),
        ("watch-graph", "--condition-json", condition, "--reason", "settle it", "--key", "k-watch"),
        ("finish", "--key", "k-finish"),
    ]

    stop = asyncio.Event()
    async with staged.invocation_gate.serve_current_session():
        server = asyncio.create_task(
            serve_command_mailbox(
                staged=staged,
                handler=handler,
                stop=stop,
                poll_seconds=0.01,
                invocation_gate=staged.invocation_gate,
            )
        )
        try:
            for arguments in calls:
                code, output = await _run_client(staged, *arguments)
                assert code == 0, f"{arguments[0]} was refused: {output}"
        finally:
            stop.set()
            await server

    assert [verb for verb, _ in seen] == [
        "spawn",
        "pause",
        "resume",
        "stop",
        "message",
        "watch_graph",
        "finish",
    ]
    watched = next(arguments for verb, arguments in seen if verb == "watch_graph")
    # RCP still normalizes for its own use; only the signature stops depending on it.
    assert watched["condition"]["status_in"] == ["refuted", "supported"]
    staged.cleanup()


@pytest.mark.asyncio
async def test_broker_reports_an_undelivered_command_as_unavailable(tmp_path) -> None:
    """A command RCP never answers is not a command the agent got wrong.

    The client distinguishes the two by exit code, and an agent told ``invalid``
    rewrites a request that was already correct.
    """

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
    assert staged.invocation_gate is not None

    # No serve loop at all, so nothing ever writes the response file.
    async with staged.invocation_gate.serve_current_session():
        code, output = await _run_client(staged, "status")

    assert code == 2, output
    assert "invalid" not in output.lower()
    staged.cleanup()
