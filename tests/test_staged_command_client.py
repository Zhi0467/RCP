from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
from contextlib import suppress
from pathlib import Path, PurePosixPath

import pytest

from rcp.agents import command_mailbox as command_mailbox_module
from rcp.agents.command_mailbox import (
    COMMAND_MAILBOX_MAX_REQUEST_BYTES,
    serve_command_mailbox,
    stage_command_mailbox,
)
from rcp.agents.command_protocol import (
    ApplyCommandRequest,
    CommandResponse,
    EpisodeCommandRequest,
    InboxCommandRequest,
    LaunchCommandRequest,
    SpawnCommandRequest,
    StatusArguments,
    staged_command_broker_source,
    staged_command_client_source,
    validate_command_request,
)
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.workspace_mailbox import RunStageMailbox, clear_turn_handoff_files

from .helpers import assert_frozen_backend_ships, async_wait_until


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
            episode_id="episode",
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
    assert_frozen_backend_ships(
        "agents/staged_command_broker.py", "agents/staged_command_client.py"
    )
    assert "def _atomic_request" in staged_command_client_source()


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
        episode_id="episode",
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
        assert identity.episode_id == "episode"
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
    assert validate_output == '{"status":"valid","messages":[]}\n'
    assert (ok_code, invalid_code, unavailable_code, finish_code) == (0, 1, 2, 0)
    assert ok_output == '{"status":"ok","message":null,"result":{"observed":"status"}}\n'
    assert json.loads(invalid_output) == {
        "status": "invalid",
        "message": "The requested result is invalid.",
        "result": {"observed": "status"},
    }
    assert json.loads(unavailable_output) == {
        "status": "unavailable",
        "message": "The requested result is unavailable.",
        "result": {"observed": "status"},
    }
    assert finish_output == ('{"status":"ok","message":null,"result":{"observed":"finish"}}\n')
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


def _request_document(verb: str, arguments: dict, *, key: str | None = None) -> str:
    return json.dumps(
        {
            "version": 1,
            "mailbox_id": "a" * 32,
            "request_id": "b" * 32,
            "credential": "c" * 64,
            "verb": verb,
            "idempotency_key": key,
            "arguments": arguments,
        }
    )


def test_command_protocol_has_strict_file_target_and_action_request_shapes() -> None:
    status = validate_command_request(
        _request_document(
            "status",
            {"worker_id": None, "episode_id": "episode-1"},
        )
    )
    assert status.arguments == StatusArguments(episode_id="episode-1")
    with pytest.raises(ValueError, match="either a worker id or an episode id"):
        StatusArguments(worker_id="worker-1", episode_id="episode-1")

    applied = validate_command_request(
        _request_document("apply", {"patch_file": "patch.json"}, key="apply-once")
    )
    assert isinstance(applied, ApplyCommandRequest)
    assert applied.arguments.patch_file == "patch.json"
    with pytest.raises(ValueError):
        validate_command_request(
            _request_document("apply", {"patch_file": "other.json"}, key="apply-once")
        )

    spawned = validate_command_request(
        _request_document(
            "spawn",
            {"seat_node_id": "exp-1", "instruction_file": "worker-task.md"},
            key="spawn-once",
        )
    )
    assert isinstance(spawned, SpawnCommandRequest)
    assert spawned.arguments.instruction_file == "worker-task.md"
    with pytest.raises(ValueError):
        validate_command_request(
            _request_document(
                "spawn",
                {
                    "seat_node_id": "exp-1",
                    "instruction_file": "nested/worker-task.md",
                },
                key="spawn-once",
            )
        )

    for arguments in (
        {
            "action": "kick_off_experiment",
            "node_id": "exp-1",
            "goal_file": "goal.md",
            "invocation_limit": 2,
        },
        {"action": "stop", "episode_id": "episode-1"},
        {"action": "resume", "episode_id": "episode-1"},
    ):
        request = validate_command_request(
            _request_document("episode", arguments, key=f"episode-{arguments['action']}")
        )
        assert isinstance(request, EpisodeCommandRequest)
        assert request.arguments.action == arguments["action"]
    with pytest.raises(ValueError):
        validate_command_request(
            _request_document(
                "episode",
                {
                    "action": "kick_off_experiment",
                    "node_id": "exp-1",
                    "goal_file": None,
                    "invocation_limit": 0,
                },
                key="episode-start",
            )
        )

    for action in ("harvest", "clear"):
        request = validate_command_request(
            _request_document("inbox", {"action": action}, key=f"inbox-{action}")
        )
        assert isinstance(request, InboxCommandRequest)
        assert request.arguments.action == action
    with pytest.raises(ValueError):
        validate_command_request(
            _request_document(
                "inbox",
                {"action": "harvest", "unexpected": True},
                key="inbox-harvest",
            )
        )


@pytest.mark.asyncio
async def test_instruction_and_goal_files_fail_closed_before_admission(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "task.md").write_text("Nested task.\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("Outside task.\n", encoding="utf-8")
    # File contents are RCP's to judge on first admission (see the dispatcher
    # tests); the client refuses only paths outside this workspace.
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )

    try:
        for path in (nested / "task.md", outside):
            calls = (
                (
                    "spawn",
                    "--key",
                    f"spawn-{path.name}",
                    "--seat-node",
                    "exp-1",
                    "--instruction-file",
                    str(path),
                ),
                (
                    "episode",
                    "--kick-off-experiment",
                    "--key",
                    f"episode-{path.name}",
                    "--node",
                    "exp-1",
                    "--goal-file",
                    str(path),
                ),
            )
            for call in calls:
                code, output = await _run_client(staged, *call)
                assert code == 1, (call, output)
                response = json.loads(output)
                assert response["status"] == "invalid"
                assert isinstance(response["message"], str)
                assert response["result"] == {}
                assert set(response) == {"status", "message", "result"}
        assert not list(workspace.glob("*.request.json"))
    finally:
        staged.cleanup()


@pytest.mark.asyncio
async def test_apply_accepts_only_this_workspace_patch_json(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    other = workspace / "other.json"
    other.write_text('{"ops":[]}\n', encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    nested_patch = nested / "patch.json"
    nested_patch.write_text('{"ops":[]}\n', encoding="utf-8")
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )

    try:
        for index, path in enumerate((other, nested_patch), start=1):
            code, output = await _run_client(
                staged,
                "apply",
                "--key",
                f"apply-{index}",
                str(path),
            )
            assert code == 1
            assert json.loads(output)["status"] == "invalid"
        assert not list(workspace.glob("*.request.json"))
    finally:
        staged.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        ("retry", "worker-1", "--key", "retry-once"),
        (
            "spawn",
            "--key",
            "spawn-once",
            "--seat-node",
            "exp-1",
            "--instruction-file",
            "task.md",
            "--provider",
            "codex",
        ),
        (
            "episode",
            "--kick-off-experiment",
            "--key",
            "episode-once",
            "--node",
            "exp-1",
            "--model",
            "gpt-5",
        ),
        (
            "episode",
            "--kick-off-experiment",
            "--key",
            "episode-once",
            "--node",
            "exp-1",
            "--effort",
            "high",
        ),
        (
            "episode",
            "--kick-off-experiment",
            "--key",
            "episode-once",
            "--node",
            "exp-1",
            "--host",
            "research.example",
        ),
        ("status", "--worker-id", "worker-1", "--episode-id", "episode-1"),
        ("launch", "--cwd", "/tmp", "--", "true"),
        ("launch", "--key", "k", "--cwd", "relative", "--", "true"),
        ("launch", "--key", "k", "--cwd", "/tmp", "true"),
        ("launch", "--key", "k", "--cwd", "/tmp", "--"),
        ("launch", "--key", "k", "--cwd", "/tmp", "--host", "elsewhere", "--", "true"),
        ("job-status", "--key", "once", "job-1"),
        ("cancel", "--key", "once", "job-1"),
    ],
)
async def test_closed_cli_rejects_retry_launch_profile_and_ambiguous_status(
    tmp_path,
    arguments,
) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    (workspace / "task.md").write_text("Do the task.\n", encoding="utf-8")
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )

    try:
        code, output = await _run_client(staged, *arguments)
        assert code == 1
        response = json.loads(output)
        assert response["status"] == "invalid"
        assert isinstance(response["message"], str)
        assert response["result"] == {}
        assert set(response) == {"status", "message", "result"}
        assert output.count("\n") == 1
        assert not list(workspace.glob("*.request.json"))
    finally:
        staged.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        ("message", "--key", "once", "This must not be dispatched."),
        ("launch", "--key", "once", "--cwd", "/tmp", "--", "true"),
    ],
)
async def test_non_campaign_credential_rejects_mutation_before_handler(tmp_path, arguments) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id=None,
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
    code, output = await _run_client(staged, *arguments)
    stop.set()
    await server

    assert code == 1
    assert json.loads(output)["status"] == "invalid"
    assert "broker authority" in output
    assert not handled


@pytest.mark.asyncio
async def test_campaign_broker_signature_cannot_authorize_a_modified_request(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
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
@pytest.mark.parametrize("episode_id", [None, "episode"])
async def test_detached_prior_turn_process_cannot_command_reused_stage(
    tmp_path, episode_id
) -> None:
    workspace = tmp_path / "reused-stage"
    workspace.mkdir()
    first = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id=episode_id,
        authority="broker",
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
        "temporary=result_path+'.tmp'\n"
        "with open(temporary,'w',encoding='utf-8') as stream:\n"
        "  json.dump({'code':result.returncode,'stdout':result.stdout},stream)\n"
        "os.replace(temporary,result_path)\n"
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
        episode_id=episode_id,
        authority="broker",
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
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )

    code, output = await _run_client(staged, "validate", str(patch))

    assert code == 1
    response = json.loads(output)
    assert set(response) == {"status", "messages"}
    assert response["status"] == "invalid"
    assert len(response["messages"]) == 1
    assert (
        f"patch.json exceeds the {COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
        in output
    )
    assert output.count("\n") == 1
    assert not list(workspace.glob("*.request.json"))
    staged.cleanup()


@pytest.mark.asyncio
async def test_staged_client_enforces_the_serialized_validator_request_limit(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    patch = workspace / "patch.json"
    patch.write_text('"' * (COMMAND_MAILBOX_MAX_REQUEST_BYTES // 2), encoding="utf-8")
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )

    code, output = await _run_client(staged, "validate", str(patch))

    assert code == 1
    assert json.loads(output) == {
        "status": "invalid",
        "messages": [
            "RCP command is invalid: serialized command request exceeds the "
            f"{COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
        ],
    }
    assert not list(workspace.glob("*.request.json"))
    staged.cleanup()


@pytest.mark.asyncio
async def test_staged_client_rejects_oversized_status_id_before_writing_request(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
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


def test_mailbox_consumes_only_the_snapshotted_file(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    mailbox = RunStageMailbox.for_stage(local_stage=workspace, remote_stage=None)
    patch = workspace / "patch.json"
    patch.write_text("first", encoding="utf-8")
    first_digest = hashlib.sha256(b"first").hexdigest()

    patch.write_text("second", encoding="utf-8")
    assert mailbox.remove_if_sha256("patch.json", first_digest) is False
    assert patch.read_text(encoding="utf-8") == "second"

    second_digest = hashlib.sha256(b"second").hexdigest()
    assert mailbox.remove_if_sha256("patch.json", second_digest) is True
    assert not patch.exists()


def test_mailbox_consume_never_unlinks_a_concurrent_replacement(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    mailbox = RunStageMailbox.for_stage(local_stage=workspace, remote_stage=None)
    patch = workspace / "patch.json"
    patch.write_text("snapshotted", encoding="utf-8")
    digest = hashlib.sha256(b"snapshotted").hexdigest()
    real_unlink = os.unlink
    replaced = False

    def replace_before_unlink(path, *args, **kwargs):
        nonlocal replaced
        if not replaced and str(path).startswith(".rcp-consume-"):
            replaced = True
            patch.write_text("newer", encoding="utf-8")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("rcp.transport.workspace_mailbox.os.unlink", replace_before_unlink)

    assert mailbox.remove_if_sha256("patch.json", digest) is True
    assert replaced is True
    assert patch.read_text(encoding="utf-8") == "newer"


def test_remote_mailbox_conditionally_consumes_or_restores_snapshot(tmp_path, monkeypatch) -> None:
    root = tmp_path / "rcp-run.test"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    patch = workspace / "patch.json"
    patch.write_text("snapshotted", encoding="utf-8")
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            check=False,
        ),
    )

    assert (
        stage.remove_workspace_file_if_sha256("patch.json", hashlib.sha256(b"other").hexdigest())
        is False
    )
    assert patch.read_text(encoding="utf-8") == "snapshotted"

    assert (
        stage.remove_workspace_file_if_sha256(
            "patch.json", hashlib.sha256(b"snapshotted").hexdigest()
        )
        is True
    )
    assert not patch.exists()


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
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=10,
    )
    assert staged.invocation_gate is not None
    seen: list[tuple[str, dict]] = []
    patch = workspace / "patch.json"
    patch.write_text('{"ops":[]}\n', encoding="utf-8")
    instruction = workspace / "worker-task.md"
    instruction.write_text("Measure the remaining uncertainty.\n", encoding="utf-8")
    goal = workspace / "experiment-goal.md"
    goal.write_text("Test the bounded hypothesis.\n", encoding="utf-8")

    def handler(request, _identity):
        seen.append((request.verb, request.arguments.model_dump(mode="json")))
        return CommandResponse(request_id=request.request_id, status="ok")

    # Deliberately not alphabetical: this is the ordering that used to be refused.
    condition = json.dumps({"node_id": "hyp-3", "status_in": ["supported", "refuted"]})
    calls = [
        ("apply", "--key", "k-apply", str(patch)),
        (
            "spawn",
            "--seat-node",
            "exp/run",
            "--instruction-file",
            str(instruction),
            "--key",
            "k-spawn",
        ),
        ("pause", "worker-1", "--key", "k-pause"),
        ("resume", "worker-1", "--key", "k-resume"),
        ("stop", "worker-1", "--key", "k-stop"),
        ("message", "keep going", "--recipient", "worker-1", "--key", "k-message"),
        ("watch-graph", "--condition-json", condition, "--reason", "settle it", "--key", "k-watch"),
        (
            "episode",
            "--kick-off-experiment",
            "--node",
            "exp/run",
            "--goal-file",
            str(goal),
            "--invocation-limit",
            "3",
            "--key",
            "k-episode-start",
        ),
        ("episode", "--stop", "episode-1", "--key", "k-episode-stop"),
        ("episode", "--resume", "episode-1", "--key", "k-episode-resume"),
        ("inbox", "--harvest", "--key", "k-inbox-harvest"),
        ("inbox", "--clear", "--key", "k-inbox-clear"),
        ("finish", "--key", "k-finish"),
        ("lesson", "add", "--key", "k-lesson", "--text", "Use the environment"),
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
        "apply",
        "spawn",
        "pause",
        "resume",
        "stop",
        "message",
        "watch_graph",
        "episode",
        "episode",
        "episode",
        "inbox",
        "inbox",
        "finish",
        "lesson",
    ]
    applied = next(arguments for verb, arguments in seen if verb == "apply")
    assert applied == {"patch_file": "patch.json"}
    spawned = next(arguments for verb, arguments in seen if verb == "spawn")
    assert spawned == {
        "seat_node_id": "exp/run",
        "instruction_file": "worker-task.md",
    }
    watched = next(arguments for verb, arguments in seen if verb == "watch_graph")
    # RCP still normalizes for its own use; only the signature stops depending on it.
    assert watched["condition"]["status_in"] == ["refuted", "supported"]
    episode_started = next(
        arguments
        for verb, arguments in seen
        if verb == "episode" and arguments["action"] == "kick_off_experiment"
    )
    assert episode_started == {
        "action": "kick_off_experiment",
        "node_id": "exp/run",
        "goal_file": "experiment-goal.md",
        "invocation_limit": 3,
    }


@pytest.mark.asyncio
async def test_brokered_client_reads_one_complete_response_larger_than_four_mib(tmp_path) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=10,
    )
    assert staged.invocation_gate is not None
    complete_snapshot = "x" * (4 * 1024 * 1024)

    def handler(request, _identity):
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="Every blocker is returned.",
            result={"episode_id": "episode", "complete_snapshot": complete_snapshot},
        )

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
            code, output = await _run_client(staged, "status")
        finally:
            stop.set()
            await server

    response = json.loads(output)
    assert code == 1
    assert len(output.encode("utf-8")) > 4 * 1024 * 1024
    assert response["result"]["complete_snapshot"] == complete_snapshot
    assert output.count("\n") == 1
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
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=2,
    )
    assert staged.invocation_gate is not None

    # No broker yet: the command never left this host.
    code, output = await _run_client(staged, "status")
    assert (code, json.loads(output)["result"]) == (2, {"delivery": "not_sent"})

    # No serve loop at all, so nothing ever writes the response file.
    async with staged.invocation_gate.serve_current_session():
        code, output = await _run_client(staged, "status")

    assert code == 2, output
    assert json.loads(output)["result"] == {"delivery": "unknown"}
    staged.cleanup()


@pytest.mark.asyncio
async def test_keyed_retry_reaches_rcp_after_apply_consumed_its_patch(tmp_path) -> None:
    """The exact retry must get RCP's recorded answer even though patch.json is gone."""

    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=10,
    )
    assert staged.invocation_gate is not None
    seen = []

    def handler(request, _identity):
        seen.append(request.arguments.model_dump(mode="json"))
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
            code, output = await _run_client(
                staged, "apply", "--key", "k-apply", str(workspace / "patch.json")
            )
            assert code == 0, output
            # Replaced by bytes RCP would refuse on a first admission: still RCP's call.
            (workspace / "patch.json").write_bytes(b"\xff")
            code, output = await _run_client(
                staged, "apply", "--key", "k-apply", str(workspace / "patch.json")
            )
            assert code == 0, output
        finally:
            stop.set()
            await server
    assert seen == [{"patch_file": "patch.json"}] * 2
    staged.cleanup()


@pytest.mark.asyncio
async def test_refusals_before_dispatch_are_recorded_and_forged_notices_are_not(
    tmp_path,
) -> None:
    from rcp import limits
    from rcp.agents import staged_command_broker, staged_command_client

    assert (
        staged_command_broker._REJECTION_NOTICE_MAX_COUNT
        == limits.COMMAND_REJECTION_NOTICE_MAX_COUNT
    )
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=10,
    )
    assert staged.invocation_gate is not None
    mailbox_id = staged.credential.mailbox_id
    recorded: list[tuple[str, str]] = []
    handled = []
    # A notice the agent could write itself: well formed, but not broker-signed.
    (workspace / f"rcp-command-{mailbox_id}-{'f' * 32}.rejected.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mailbox_id": mailbox_id,
                "notice_id": "f" * 32,
                "status": "unavailable",
                "message": "forged",
                "credential": "0" * 64,
            }
        )
    )
    # A request that bypassed the broker, so it carries no valid signature.
    request_id = "e" * 32
    (workspace / f"rcp-command-{mailbox_id}-{request_id}.request.json").write_text(
        json.dumps(
            {
                **json.loads(_request_document("status", {"worker_id": None, "episode_id": None})),
                "mailbox_id": mailbox_id,
                "request_id": request_id,
            }
        )
    )
    stop = asyncio.Event()
    async with staged.invocation_gate.serve_current_session():
        try:
            # A malformed message straight to the broker never becomes a request.
            path = staged_command_client._broker_socket_path(staged.invocation_gate.socket_path)
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b"not json\n")
            await writer.drain()
            answer = json.loads(await reader.readline())
            writer.close()
            server = asyncio.create_task(
                serve_command_mailbox(
                    staged=staged,
                    handler=lambda request, _identity: handled.append(request),
                    stop=stop,
                    poll_seconds=0.01,
                    invocation_gate=staged.invocation_gate,
                    record_rejection=lambda status, message: recorded.append((status, message)),
                )
            )
            await async_wait_until(lambda: len(recorded) == 2)
            await asyncio.sleep(0.1)  # a few more polls, so a forged notice would show
        finally:
            stop.set()
            await server
    assert answer["result"] == {"delivery": "not_sent"}
    # Only the broker's refusal and the mailbox's refusal; never the forged notice.
    assert [status for status, _message in recorded] == ["invalid", "invalid"]
    assert not handled
    staged.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("episode_id", [None, "episode"])
async def test_compute_launch_uses_turn_bound_broker_without_bearer_credential(
    tmp_path, episode_id
) -> None:
    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=episode_id,
        task_id="work-task",
        turn_id="work-turn",
        authority="broker",
        timeout_seconds=5,
    )
    assert staged.credential.identity.authority == "broker"
    assert staged.credential_path is None
    assert not list(tmp_path.glob("*.credential.json"))
    with pytest.raises(RuntimeError, match="broker-only"):
        staged.credential.document()
    assert staged.invocation_gate is not None
    seen = []

    def handler(request, identity):
        assert identity.episode_id == episode_id
        assert identity.task_id == "work-task"
        assert identity.authority == "broker"
        seen.append(request)
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
            for arguments in (
                (
                    "launch",
                    "--key",
                    "launch-once",
                    "--cwd",
                    str(tmp_path),
                    "--label",
                    "A run",
                    "--",
                    "python3",
                    "-c",
                    "print('hello')",
                    "",
                ),
            ):
                code, output = await _run_client(staged, *arguments)
                assert code == 0, output
        finally:
            stop.set()
            await server
    assert isinstance(seen[0], LaunchCommandRequest)
    assert seen[0].idempotency_key == "launch-once"
    assert seen[0].arguments.model_dump() == {
        "cwd": str(tmp_path),
        "label": "A run",
        "argv": ["python3", "-c", "print('hello')", ""],
    }
    assert len(seen) == 1
    assert staged.credential.expired


@pytest.mark.parametrize(
    ("verb", "arguments"),
    [
        ("launch", {"cwd": "/tmp", "argv": ["true"]}),
    ],
)
def test_compute_protocol_requires_a_nonblank_key(verb, arguments) -> None:
    envelope = {
        "version": 1,
        "mailbox_id": "a" * 32,
        "request_id": "b" * 32,
        "credential": "c" * 64,
        "verb": verb,
        "arguments": arguments,
    }
    for key in (None, "", " "):
        with pytest.raises(ValueError):
            validate_command_request(json.dumps({**envelope, "idempotency_key": key}))
    with pytest.raises(ValueError):
        validate_command_request(json.dumps(envelope))


@pytest.mark.parametrize("verb", ["job_status", "cancel"])
def test_removed_compute_verbs_are_not_in_the_command_protocol(verb):
    envelope = {
        "version": 1,
        "mailbox_id": "a" * 32,
        "request_id": "b" * 32,
        "credential": "c" * 64,
        "verb": verb,
        "idempotency_key": "once",
        "arguments": {"job_id": "job-1"},
    }
    with pytest.raises(ValueError):
        validate_command_request(json.dumps(envelope))


def _sign(staged, value, domain=b""):
    import hmac

    from rcp.agents.command_protocol import command_authentication_payload

    document = json.dumps(value)
    signature = hmac.new(
        staged.credential.token.encode("ascii"),
        domain + command_authentication_payload(document),
        hashlib.sha256,
    ).hexdigest()
    return {**value, "credential": signature}


@pytest.mark.asyncio
async def test_workspace_notices_stay_inert_and_the_mailbox_keeps_serving(tmp_path) -> None:
    """The agent can write any file here; only a genuine, uncopied notice records."""

    from rcp import limits
    from rcp.agents import command_mailbox, staged_command_broker

    assert staged_command_broker._REJECTION_NOTICE_DOMAIN == command_mailbox.REJECTION_NOTICE_DOMAIN
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id="episode",
        task_id="task",
        turn_id="turn",
        timeout_seconds=10,
    )
    mailbox_id = staged.credential.mailbox_id

    def notice_path(number):
        return workspace / f"rcp-command-{mailbox_id}-{number:032x}.rejected.json"

    staged_command_broker._publish_rejection(
        str(workspace), mailbox_id, staged.credential.token, "invalid", "genuine"
    )
    genuine = next(workspace.glob("*.rejected.json"))
    notice_path(1).write_text(genuine.read_text())  # renamed copy
    notice_path(2).write_text(json.dumps({"mailbox_id": mailbox_id, "credential": "\u2603"}))
    # Requests the broker legitimately signed, replayed as notices: one shaped
    # exactly like a notice, one with a status that used to crash the reader.
    for number, status in ((3, "invalid"), (4, [])):
        notice_path(number).write_text(
            json.dumps(
                _sign(
                    staged,
                    {
                        "version": 1,
                        "mailbox_id": mailbox_id,
                        "notice_id": f"{number:032x}",
                        "status": status,
                        "message": "not a notice",
                    },
                )
            )
        )
    # More unsigned requests than the recording cap.
    for number in range(limits.COMMAND_REJECTION_NOTICE_MAX_COUNT + 5):
        name = f"rcp-command-{mailbox_id}-{number + 100:032x}.request.json"
        (workspace / name).write_text("{}")
    legitimate = _sign(
        staged,
        {
            **json.loads(_request_document("status", {"worker_id": None, "episode_id": None})),
            "mailbox_id": mailbox_id,
            "request_id": "f" * 32,
        },
    )
    (workspace / f"rcp-command-{mailbox_id}-{'f' * 32}.request.json").write_text(
        json.dumps(legitimate)
    )
    recorded: list[str] = []
    handled = []

    def handler(request, _identity):
        handled.append(request.request_id)
        return CommandResponse(request_id=request.request_id, status="ok")

    stop = asyncio.Event()
    server = asyncio.create_task(
        serve_command_mailbox(
            staged=staged,
            handler=handler,
            stop=stop,
            poll_seconds=0.01,
            invocation_gate=staged.invocation_gate,
            record_rejection=lambda _status, message: recorded.append(message),
        )
    )
    try:
        await async_wait_until(lambda: handled)
    finally:
        stop.set()
        await server
    assert handled == ["f" * 32]
    assert len(recorded) == limits.COMMAND_REJECTION_NOTICE_MAX_COUNT
    assert recorded[0] == "genuine"
    assert "genuine" not in recorded[1:]
    assert "not a notice" not in recorded
    staged.cleanup()


def test_a_timed_out_joiner_leaves_the_real_answer_for_the_next_repeat(
    tmp_path, monkeypatch
) -> None:
    import threading

    from rcp.agents import staged_command_broker as broker

    monkeypatch.setattr(broker, "_peer_identity", lambda _connection: (os.getpid(), os.getuid()))
    monkeypatch.setattr(broker, "_is_live_descendant", lambda *_args: True)
    monkeypatch.setattr(broker, "_keyed_commands", {})
    mailbox_id = "a" * 32

    class Connection:
        def __init__(self, value, lost=False):
            self.content = json.dumps(value).encode() + b"\n"
            self.lost = lost
            self.output = None

        def recv(self, count):
            chunk, self.content = self.content[:count], self.content[count:]
            return chunk

        def sendall(self, content):
            if self.lost:
                raise BrokenPipeError("the client already gave up")
            self.output = json.loads(content)

        def close(self):
            pass

    def call(number, lost=False):
        connection = Connection(
            {
                "version": 1,
                "mailbox_id": mailbox_id,
                "request_id": f"{number:032x}",
                "verb": "apply",
                "idempotency_key": "once",
                "arguments": {"patch_file": "patch.json"},
            },
            lost=lost,
        )
        broker._handle(
            connection,
            root_pid=os.getpid(),
            root_birth=None,
            expected_session=None,
            mailbox_id=mailbox_id,
            token="b" * 64,
            workspace=str(tmp_path),
            response_timeout=0.05,
        )
        return connection.output

    reading, finish = threading.Event(), threading.Event()
    sends = []

    def answer(_path, request_id, _timeout):
        sends.append(request_id)
        reading.set()
        assert finish.wait(2)
        return {"version": 1, "request_id": request_id, "status": "ok", "result": {"n": 1}}

    monkeypatch.setattr(broker, "_read_response", answer)
    owner = threading.Thread(target=call, args=(1,), kwargs={"lost": True})
    owner.start()
    assert reading.wait(2)
    joined = broker._joined_response

    def join_then_let_the_owner_finish(entry, request_id, timeout):
        response = joined(entry, request_id, timeout)  # times out first
        finish.set()
        owner.join(2)
        return response

    monkeypatch.setattr(broker, "_joined_response", join_then_let_the_owner_finish)
    assert call(2)["status"] == "unavailable"
    monkeypatch.setattr(broker, "_joined_response", joined)
    # The owner's answer reached nobody, so the next repeat gets it without resending.
    assert call(3)["result"] == {"n": 1}
    assert sends == [f"{1:032x}"]


def test_client_deadline_covers_the_whole_invocation(monkeypatch, capsys) -> None:
    import socket
    import threading
    import time
    from types import SimpleNamespace

    from rcp.agents import staged_command_client as client

    left, right = socket.socketpair()

    class Connected:
        def connect(self, _path):
            pass

        def __getattr__(self, name):
            return getattr(left, name)

    monkeypatch.setattr(client.socket, "socket", lambda *_args: Connected())
    response = json.dumps({"version": 1, "request_id": "a" * 32, "status": "ok"}).encode() + b"\n"

    def trickle():
        right.recv(100)
        for offset in range(0, len(response), 10):
            time.sleep(0.03)
            with suppress(OSError):
                right.sendall(response[offset : offset + 10])
        right.close()

    sender = threading.Thread(target=trickle)
    sender.start()
    started = time.monotonic()
    code = client._run_brokered(
        SimpleNamespace(timeout=0.1, verb="launch"), "unused", b"{}\n", "a" * 32
    )
    elapsed = time.monotonic() - started
    sender.join()
    assert code == 2 and elapsed < 0.2
    assert json.loads(capsys.readouterr().out)["result"] == {"delivery": "unknown"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_step", ["listing", "reading", "handling", "writing"])
async def test_mailbox_retries_transport_step_without_repeating_completed_handler(
    tmp_path, monkeypatch, failure_step
) -> None:
    import threading

    from rcp.transport import StateUnreachable

    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id=None,
        task_id="task",
        turn_id="turn",
        timeout_seconds=3,
    )
    monkeypatch.setattr(command_mailbox_module, "COMMAND_MAILBOX_RETRY_INITIAL_SECONDS", 0.001)
    calls = {"listing": 0, "reading": 0, "handling": 0, "writing": 0}
    effects = []
    events = []
    for step, method in (
        ("listing", "entry_names"),
        ("reading", "read_text"),
        ("writing", "write_text"),
    ):
        original = getattr(RunStageMailbox, method)

        def flaky(self, *args, step=step, original=original, **kwargs):
            # Closure writes are separate from the response boundary under test.
            if step != "writing" or args[0].endswith(".response.json"):
                calls[step] += 1
                if step == failure_step and calls[step] == 1:
                    raise StateUnreachable("temporary transport outage")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(RunStageMailbox, method, flaky)

    def handler(request, _identity):
        calls["handling"] += 1
        if failure_step == "handling" and calls["handling"] == 1:
            raise StateUnreachable("temporary transport outage")
        effects.append(request.request_id)
        return CommandResponse(request_id=request.request_id, status="ok")

    stop = threading.Event()
    server = asyncio.create_task(
        serve_command_mailbox(
            staged=staged,
            handler=handler,
            stop=stop,
            poll_seconds=0.001,
            record_transport=lambda status, _message: events.append(status),
            # Only a checkpointing turn may re-run a handler after a blip.
            checkpoint=lambda: None,
        )
    )
    try:
        code, output = await _run_client(staged, "status")
        assert code == 0, output
        assert len(effects) == 1
        assert events == ["outage", "recovered"]
        if failure_step == "writing":
            assert calls["handling"] == 1
            assert calls["writing"] == 2
    finally:
        stop.set()
        await server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "checkpointed", "expected_calls"),
    [
        ("unreachable", True, 1 + command_mailbox_module.COMMAND_MAILBOX_HANDLER_MAX_RETRIES),
        ("unreachable", False, 1),
        ("timeout", True, 1),
    ],
)
async def test_failing_handler_is_rerun_a_bounded_number_of_times(
    tmp_path, monkeypatch, failure, checkpointed, expected_calls
) -> None:
    import threading

    from rcp.transport import StateUnreachable

    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id=None,
        task_id="task",
        turn_id="turn",
        timeout_seconds=3,
    )
    monkeypatch.setattr(command_mailbox_module, "COMMAND_MAILBOX_RETRY_INITIAL_SECONDS", 0.001)
    calls = []

    def handler(request, _identity):
        calls.append(request.request_id)
        raise StateUnreachable("link gone") if failure == "unreachable" else TimeoutError()

    stop = threading.Event()
    server = asyncio.create_task(
        serve_command_mailbox(
            staged=staged,
            handler=handler,
            stop=stop,
            poll_seconds=0.001,
            checkpoint=(lambda: None) if checkpointed else None,
        )
    )
    try:
        first, _ = await _run_client(staged, "status")
        # The loop moved on: a later request is still answered.
        second, output = await _run_client(staged, "status")
        assert first == second == 2
        assert json.loads(output)["status"] == "unavailable"
        assert len(calls) == 2 * expected_calls
    finally:
        stop.set()
        await server


@pytest.mark.asyncio
async def test_permanent_mailbox_failure_answers_later_calls(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        episode_id=None,
        task_id="task",
        turn_id="turn",
        timeout_seconds=1,
    )
    handled = []

    def malformed(_self):
        raise ValueError("mailbox listing malformed")

    monkeypatch.setattr(RunStageMailbox, "entry_names", malformed)
    terminal = {}
    await serve_command_mailbox(
        staged=staged,
        handler=lambda *args: handled.append(args),
        stop=asyncio.Event(),
        terminal=terminal,
    )
    assert staged.credential.expired
    assert "malformed" in terminal["reason"]
    for _ in range(2):
        code, output = await _run_client(staged, "status")
        result = json.loads(output)
        assert code == 2
        assert result["result"] == {"permanent": True, "delivery": "not_sent"}
        assert "malformed" in result["message"]
    assert not handled


@pytest.mark.asyncio
async def test_suspended_mailbox_restores_completed_response_and_same_credential(
    tmp_path, monkeypatch
) -> None:
    import threading
    from dataclasses import replace

    from rcp.agents.command_mailbox import CommandTurnCredential
    from rcp.transport import StateUnreachable

    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=None,
        task_id="task",
        turn_id="turn",
        timeout_seconds=3,
    )
    token = staged.credential.token
    original_write = RunStageMailbox.write_text
    suspended = threading.Event()
    responses = {}
    effects = []

    def lose_response(self, name, content):
        if name.endswith(".response.json"):
            suspended.set()
            raise StateUnreachable("backend stops during response delivery")
        original_write(self, name, content)

    monkeypatch.setattr(RunStageMailbox, "write_text", lose_response)

    def handler(request, _identity):
        effects.append(request.request_id)
        return CommandResponse(request_id=request.request_id, status="ok")

    first = asyncio.create_task(
        serve_command_mailbox(
            staged=staged,
            handler=handler,
            stop=threading.Event(),
            suspend=suspended,
            responses=responses,
            poll_seconds=0.001,
        )
    )
    caller = asyncio.create_task(_run_client(staged, "status"))
    await async_wait_until(first.done)
    await first
    assert len(effects) == 1
    assert staged.credential.token == token
    assert not list(tmp_path.glob("*.closed.json"))
    saved = {name: response.model_dump_json() for name, response in responses.items()}
    restored = replace(
        staged,
        credential=CommandTurnCredential(
            identity=staged.credential.identity,
            mailbox_id=staged.credential.mailbox_id,
            _token=token,
        ),
    )
    monkeypatch.setattr(RunStageMailbox, "write_text", original_write)
    stop = threading.Event()
    second = asyncio.create_task(
        serve_command_mailbox(
            staged=restored,
            handler=handler,
            stop=stop,
            poll_seconds=0.001,
            responses={
                name: CommandResponse.model_validate_json(value) for name, value in saved.items()
            },
        )
    )
    try:
        code, output = await caller
        assert code == 0, output
        assert len(effects) == 1
        # The previously staged client and its unchanged credential still work.
        code, output = await _run_client(staged, "status")
        assert code == 0, output
        assert len(effects) == 2
    finally:
        stop.set()
        await second


@pytest.mark.asyncio
async def test_stop_during_response_outage_finishes_with_saved_permanent_reason(
    tmp_path, monkeypatch
) -> None:
    import threading

    from rcp.transport import StateUnreachable

    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=None,
        task_id="task",
        turn_id="turn",
        timeout_seconds=1,
    )
    token = staged.credential.token
    request_id = "a" * 32
    request_name = f"rcp-command-{staged.credential.mailbox_id}-{request_id}.request.json"
    (tmp_path / request_name).write_text(
        json.dumps(
            {
                **json.loads(_request_document("status", {"worker_id": None, "episode_id": None})),
                "mailbox_id": staged.credential.mailbox_id,
                "request_id": request_id,
                "credential": token,
            }
        )
    )
    monkeypatch.setattr(command_mailbox_module, "COMMAND_MAILBOX_RETRY_INITIAL_SECONDS", 0.001)
    stop = threading.Event()
    effects = []
    attempts = {"response": 0, "closed": 0}
    responses = {}
    terminal = {}
    saved = {}

    def unreachable(_self, name, _content):
        step = "response" if name.endswith(".response.json") else "closed"
        attempts[step] += 1
        stop.set()
        raise StateUnreachable("host unreachable")

    def handler(request, _identity):
        effects.append(request.request_id)
        return CommandResponse(request_id=request.request_id, status="ok")

    def checkpoint():
        saved["terminal"] = dict(terminal)
        saved["responses"] = {name: response.model_dump() for name, response in responses.items()}

    monkeypatch.setattr(RunStageMailbox, "write_text", unreachable)
    server = asyncio.create_task(
        serve_command_mailbox(
            staged=staged,
            handler=handler,
            stop=stop,
            responses=responses,
            terminal=terminal,
            checkpoint=checkpoint,
            poll_seconds=0.001,
        )
    )
    await async_wait_until(server.done)
    await server
    assert effects == [request_id]
    bound = command_mailbox_module.COMMAND_MAILBOX_STOP_MAX_FAILED_ATTEMPTS
    assert attempts == {"response": bound, "closed": bound}
    assert saved["responses"][request_name]["status"] == "ok"
    assert "stopped" in saved["terminal"]["reason"]
    assert "unavailable" in saved["terminal"]["reason"]
    assert terminal == saved["terminal"]
    assert staged.credential.expired
    assert not list(tmp_path.glob("*.closed.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["answered", "dismissed", "parked"])
async def test_ask_polls_fresh_requests_through_broker_and_mailbox(tmp_path, state) -> None:
    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=None,
        task_id="work-task",
        turn_id="work-turn",
        authority="broker",
        timeout_seconds=5,
    )
    seen = []

    def handler(request, _identity):
        seen.append(request)
        result = {"state": "pending" if len(seen) == 1 else state, "question_id": "question"}
        if result["state"] == "answered":
            result.update(answer="Use A", choices=["A"], receipt_token="f" * 64)
        return CommandResponse(request_id=request.request_id, status="ok", result=result)

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
            code, output = await _run_client(
                staged,
                "ask",
                "--key",
                "choice",
                "--question",
                "A or B?",
                "--choice",
                "A",
                "--choice",
                "B",
                "--multiple",
            )
        finally:
            stop.set()
            await server
    assert code == 0, output
    result = json.loads(output)["result"]
    assert result["state"] == state
    if state == "answered":
        assert result["answer"] == "Use A" and result["choices"] == ["A"]
    assert len(seen) == (3 if state == "answered" else 2)
    if state == "answered":
        assert seen[2].receipt_token == "f" * 64
        assert seen[2].request_id not in {seen[0].request_id, seen[1].request_id}
        assert seen[2].arguments == seen[1].arguments
    assert seen[0].receipt_token is None
    assert seen[1].receipt_token is None
    assert seen[0].request_id != seen[1].request_id
    assert seen[0].idempotency_key == seen[1].idempotency_key == "choice"
    assert seen[0].arguments == seen[1].arguments
    assert seen[0].arguments.model_dump() == {
        "question": "A or B?",
        "choices": ["A", "B"],
        "multiple": True,
    }


@pytest.mark.parametrize("state", ["pending", "answered", "dismissed", "parked"])
def test_ask_polling_uses_one_outer_deadline(tmp_path, monkeypatch, capsys, state) -> None:
    from rcp.agents import staged_command_client as client

    elapsed = [0.0]
    requests = []
    monkeypatch.setattr(client.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        client.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    )
    monkeypatch.setattr(client, "COMMAND_ASK_POLL_SECONDS", 0.4)

    def round_trip(_namespace, _broker, content, request_id, deadline):
        request = json.loads(content)
        requests.append((request, deadline))
        elapsed[0] += 0.1
        return client._handle_response(
            {
                "request_id": request_id,
                "status": "ok",
                "result": {
                    "state": "pending" if len(requests) == 1 else state,
                    "question_id": "question",
                    **(
                        {"receipt_token": "f" * 64}
                        if state == "answered" and len(requests) > 1
                        else {}
                    ),
                },
            },
            "ask",
            request_id,
        )

    monkeypatch.setattr(client, "_run_brokered", round_trip)
    code = client.main(
        [
            "--broker",
            "~/.rcp/sockets/rcp-command-test.sock",
            "--mailbox-id",
            "a" * 32,
            "--timeout",
            "1",
            "--workspace",
            str(tmp_path),
            "ask",
            "--key",
            "once",
            "--question",
            "Proceed?",
        ]
    )
    assert code == 0
    assert elapsed[0] <= 1
    if state == "pending":
        assert elapsed[0] == 1
    assert len(requests) == (3 if state == "answered" else 2)
    if state == "answered":
        assert requests[2][0]["receipt_token"] == "f" * 64
    assert {deadline for _, deadline in requests} == {1}
    assert len({request["request_id"] for request, _ in requests}) == len(requests)
    assert json.loads(capsys.readouterr().out)["result"] == {
        "state": state,
        "question_id": "question",
    }


@pytest.mark.parametrize("delivery", ["not_sent", "unknown"])
def test_ask_transport_failure_preserves_delivery(tmp_path, monkeypatch, capsys, delivery) -> None:
    from rcp.agents import staged_command_client as client

    calls = []
    monkeypatch.setattr(client, "COMMAND_ASK_POLL_SECONDS", 0)

    def failed(_namespace, _broker, _content, request_id, _deadline):
        calls.append(request_id)
        if len(calls) == 1:
            return client._handle_response(
                {
                    "request_id": request_id,
                    "status": "ok",
                    "result": {
                        "state": "pending",
                        "question_id": "question",
                    },
                },
                "ask",
                request_id,
            )
        # A prior pending state must not conceal an uncertain later transport round.
        return client._handle_response(
            {"request_id": request_id, "status": "unavailable", "result": {"delivery": delivery}},
            "ask",
            request_id,
        )

    monkeypatch.setattr(client, "_run_brokered", failed)
    assert (
        client.main(
            [
                "--broker",
                "~/.rcp/sockets/rcp-command-test.sock",
                "--mailbox-id",
                "a" * 32,
                "--timeout",
                "1",
                "--workspace",
                str(tmp_path),
                "ask",
                "--key",
                "once",
                "--question",
                "Proceed?",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["result"] == {"delivery": delivery}


@pytest.mark.asyncio
async def test_ask_broker_refreshes_state_after_an_undelivered_pending(
    tmp_path, monkeypatch
) -> None:
    from rcp.agents import staged_command_broker as broker

    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=None,
        task_id="work-task",
        turn_id="work-turn",
        authority="broker",
        timeout_seconds=2,
    )
    monkeypatch.setattr(broker, "_peer_identity", lambda _connection: (os.getpid(), os.getuid()))
    monkeypatch.setattr(broker, "_is_live_descendant", lambda *_args: True)
    seen = []

    def handler(request, _identity):
        seen.append(request)
        result = {"question_id": "question", "state": "pending"}
        if len(seen) > 1:
            result.update(state="answered", answer="Proceed", choices=[])
        return CommandResponse(request_id=request.request_id, status="ok", result=result)

    class Connection:
        def __init__(self, number):
            self.content = (
                json.dumps(
                    {
                        "version": 1,
                        "mailbox_id": staged.credential.mailbox_id,
                        "request_id": f"{number:032x}",
                        "verb": "ask",
                        "idempotency_key": "once",
                        "arguments": {"question": "Proceed?"},
                    }
                ).encode()
                + b"\n"
            )
            self.lost = number == 1
            self.response = None

        def recv(self, count):
            chunk, self.content = self.content[:count], self.content[count:]
            return chunk

        def sendall(self, content):
            self.response = json.loads(content)
            if self.lost:
                raise BrokenPipeError("client disconnected before receiving pending")

        def close(self):
            pass

    stop = asyncio.Event()
    server = asyncio.create_task(
        serve_command_mailbox(
            staged=staged,
            handler=handler,
            stop=stop,
            poll_seconds=0.01,
            invocation_gate=staged.invocation_gate,
        )
    )
    responses = []
    try:
        for number in (1, 2):
            connection = Connection(number)
            await asyncio.to_thread(
                broker._handle,
                connection,
                root_pid=os.getpid(),
                root_birth=None,
                expected_session=None,
                mailbox_id=staged.credential.mailbox_id,
                token=staged.credential.token,
                workspace=str(tmp_path),
                response_timeout=2,
            )
            responses.append(connection.response)
    finally:
        stop.set()
        await server
    assert [response["result"]["state"] for response in responses] == ["pending", "answered"]
    assert responses[1]["result"]["answer"] == "Proceed"
    assert [request.request_id for request in seen] == [f"{number:032x}" for number in (1, 2)]
    assert seen[0].idempotency_key == seen[1].idempotency_key == "once"
    assert seen[0].arguments == seen[1].arguments


@pytest.mark.parametrize("delivered,ack_failed", [(False, False), (True, False), (True, True)])
def test_file_client_acknowledges_only_consumed_answer(
    tmp_path, monkeypatch, capsys, delivered, ack_failed
):
    from rcp.agents import staged_command_client as client

    mailbox_id = "a" * 32
    credential = tmp_path / "credential.json"
    credential.write_text(json.dumps({"version": 1, "mailbox_id": mailbox_id, "token": "b" * 64}))
    requests = []
    write = client._atomic_request

    def respond(path, content):
        write(path, content)
        request = json.loads(content)
        requests.append(request)
        if delivered:
            response = {
                "request_id": request["request_id"],
                "status": "ok",
                "result": {
                    "state": "answered",
                    "question_id": "q",
                    "answer": "Use A",
                    "receipt_token": "c" * 64,
                },
            }
            write(path.replace(".request.json", ".response.json"), json.dumps(response).encode())

    closed_response = client._closed_response

    def check_closed(*args):
        if ack_failed and requests:
            raise OSError("acknowledgement transport unavailable")
        return closed_response(*args)

    monkeypatch.setattr(client, "_closed_response", check_closed)
    monkeypatch.setattr(client, "_atomic_request", respond)
    code = client.main(
        [
            "--credential",
            str(credential),
            "--timeout",
            "0.1",
            "--workspace",
            str(tmp_path),
            "ask",
            "--key",
            "choice",
            "--question",
            "Which path?",
        ]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert len(requests) == (2 if delivered and not ack_failed else 1)
    assert "receipt_token" not in requests[0]
    if delivered:
        assert code == 0
        assert output["result"] == {"state": "answered", "question_id": "q", "answer": "Use A"}
        if ack_failed:
            assert json.loads(captured.err)["status"] == "unavailable"
        else:
            assert requests[1]["receipt_token"] == "c" * 64
            assert requests[1]["request_id"] != requests[0]["request_id"]
            assert requests[1]["arguments"] == requests[0]["arguments"]
    else:
        assert code == 2
        assert output["result"]["delivery"] == "unknown"
