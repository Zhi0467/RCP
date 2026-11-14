from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from rcp.providers.agy.profile import AgyProfile
from rcp.providers.base import ProviderTurnRequest

FIXTURES = Path(__file__).parent / "fixtures" / "agy"
AGY = AgyProfile()


@pytest.fixture(autouse=True)
def isolated_agy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "account"))


def _wrapped(command):
    return [*AGY.authentication.unmanaged_environment("").command_prefix, *command]


def request(tmp_path, capability="discuss"):
    return ProviderTurnRequest(
        prompt="hello",
        binary="/usr/bin/true",
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[Path("/read")],
        write_dirs=[],
        write_scope=None,
        capability=capability,
        provider_version="1.5.8",
    )


def test_recorded_decoder_and_usage():
    records = [
        json.loads(line) for line in (FIXTURES / "success_two_turns.jsonl").read_text().splitlines()
    ]
    events = [AGY.decode_event(value, json.dumps(value)) for value in records]
    assert events[0].event == "session"
    assert len([e for e in events if e.event == "answer"]) == 2
    usage = [e.usage for e in events if e.usage]
    assert len(usage) == 3
    assert [u.processed_input_tokens for u in usage] == [4288, 4424, 4595]
    assert sum(u.generated_tokens for u in usage) == 148
    for record in records:
        if record["event"] == "result":
            assert AGY.decode_usage(record, json.dumps(record)) is None


@pytest.mark.parametrize("fixture", ["denied_resume.jsonl", "bad_model.jsonl"])
def test_recorded_failures(fixture):
    records = [json.loads(line) for line in (FIXTURES / fixture).read_text().splitlines()]
    errors = [AGY.decode_event(r, json.dumps(r)) for r in records if r["event"] == "result"]
    assert errors[-1].event == "error"
    if fixture == "denied_resume.jsonl":
        assert "command" in errors[-1].text


@pytest.mark.parametrize("capability", ["discuss", "scratch_patch"])
def test_scratch_policy_and_host_staging(tmp_path, capability):
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(request(tmp_path, capability))
    command = turn.command
    policy = json.loads(command[command.index("--settings") + 1])
    assert set(policy["permissions"]) == {"allow", "ask", "deny"}
    assert policy["permissions"]["ask"] == []
    assert policy["agentMode"] == ""
    assert "command(*)" not in policy["permissions"]["allow"]
    assert "--add-dir" not in command
    assert command[command.index("--mode") + 1] == "accept-edits"
    assert not set(command) & {
        "-p",
        "--sandbox",
        "--remote-agent",
        "--remote-control",
        "--dangerously-skip-permissions",
        "--effort",
    }
    assert f"write_file({tmp_path}/.agents)" in policy["permissions"]["deny"]
    subprocess.run(_wrapped(command), cwd=tmp_path, check=True, capture_output=True)
    agent = next((tmp_path / ".agents" / "agents").glob("rcp-*.md"))
    assert agent.exists()
    assert "run_command" not in agent.read_text()
    assert command == AGY.runtime(AGY.legacy_runtime_id).turn(request(tmp_path, capability)).command


def test_steering_replay(tmp_path):
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(request(tmp_path))
    assert json.loads(turn.initial_input())["event"] == "user"
    turn.receive_line(json.dumps({"event": "init", "conversation_id": "session"}))
    state = turn.steering_state()
    assert state.can_steer
    turn.render_steer(state.turn_id, "steer", "follow up")
    result = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}})
    assert not turn.receive_line(result).complete
    assert turn.receive_line(result).complete
    replay = AGY.runtime(AGY.legacy_runtime_id).turn(request(tmp_path))
    replay.adopt_recorded_inputs(["prompt", "steer"], {})
    assert not replay.receive_line(result).complete
    assert replay.receive_line(result).complete


def test_catalog_skills_and_auth():
    assert AGY.parse_catalog((FIXTURES / "models.txt").read_text())
    assert len(AGY.parse_skills((FIXTURES / "skills.json").read_text())) == 2
    auth = AGY.authentication
    assert auth.methods == ()
    result = subprocess.CompletedProcess([], 0, (FIXTURES / "usage.json").read_text(), "")
    assert auth.verification_succeeded(result)
    assert not auth.verification_succeeded(
        subprocess.CompletedProcess([], 0, '{"status":"ERROR"}', "")
    )


def test_version_and_summary_refusal(tmp_path):
    for capability in ["discuss", "scratch_patch", "paper_suggest", "work_auto", "orchestrate"]:
        with pytest.raises(ValueError):
            AGY.validate_readiness_version("1.5.7", capability=capability)
    with pytest.raises(ValueError):
        AGY.runtime(AGY.legacy_runtime_id).turn(
            replace(request(tmp_path), capability="session_summary")
        )


@pytest.mark.parametrize("capability", ["work_auto", "orchestrate", "paper_suggest"])
def test_work_tools_and_exact_write_roots(tmp_path, capability):
    from rcp.agents.write_scope import ProjectWriteScope

    scope = None
    roots = [Path("/coach/changes")]
    if capability != "paper_suggest":
        scope = ProjectWriteScope.create(
            project_id="project",
            machine_id="backend",
            capability=capability,
            stage_root=str(tmp_path.parent),
            workspace_root=str(tmp_path),
            repositories=[],
            granted_roots=["/repo"],
            protected_write_paths=["/repo/protected"],
            granted_protected_paths=[],
        )
        roots = []
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(
        replace(request(tmp_path, capability), write_scope=scope, write_dirs=roots)
    )
    policy = json.loads(turn.command[turn.command.index("--settings") + 1])
    assert set(policy["permissions"]) == {"allow", "ask", "deny"}
    assert policy["permissions"]["ask"] == []
    assert turn.command[turn.command.index("--mode") + 1] == "accept-edits"
    assert not set(turn.command) & {
        "-p",
        "--sandbox",
        "--remote-agent",
        "--remote-control",
        "--dangerously-skip-permissions",
        "--effort",
    }
    allows = policy["permissions"]["allow"]
    assert "command(*)" in allows
    expected = scope.writable_roots if scope else list(map(str, roots))
    assert {rule for rule in allows if rule.startswith("write_file(")} == {
        f"write_file({root})" for root in expected if root != str(tmp_path)
    }
    subprocess.run(_wrapped(turn.command), cwd=tmp_path, check=True, capture_output=True)
    agent = next((tmp_path / ".agents" / "agents").glob("rcp-*.md"))
    tools = json.loads(agent.read_text().split("tools: ")[1].splitlines()[0])
    assert {"run_command", "command_status", "send_command_input", "invoke_subagent"} <= set(tools)
    known = json.loads((FIXTURES / "success_two_turns.jsonl").read_text().splitlines()[0])["init"][
        "tools"
    ]
    assert set(tools) <= set(known)


@pytest.mark.parametrize("capability", ["scratch_patch", "discuss"])
def test_client_only_grants_scratch_shell(tmp_path, capability):
    import shlex

    from rcp.agents.invocation_broker import ProviderInvocationGate

    gate = ProviderInvocationGate(
        mailbox_id="mail",
        broker_path="/client/broker",
        socket_path="/client/socket",
        workspace=str(tmp_path),
        response_timeout_seconds=5,
        _token="test",
        client_path="/client/client.py",
    )
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(
        replace(request(tmp_path, capability), invocation_gate=gate)
    )
    policy = json.loads(turn.command[turn.command.index("--settings") + 1])
    grant = f"command({shlex.join(gate.client_executable_argv())})"
    assert (grant in policy["permissions"]["allow"]) == (capability == "scratch_patch")
    assert "write_file(/client)" in policy["permissions"]["deny"]
    subprocess.run(_wrapped(turn.command), cwd=tmp_path, check=True, capture_output=True)
    agent = next((tmp_path / ".agents" / "agents").glob("rcp-*.md"))
    assert ("run_command" in agent.read_text()) == (capability == "scratch_patch")
    assert "command_status" not in agent.read_text()


def test_temp_policy_uses_execution_host_and_reports_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/host-temp")
    from rcp.providers.agy.remote import _agy_host_policy

    outside = replace(request(tmp_path), cwd=Path("/work"))
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(outside)
    monkeypatch.chdir(tmp_path)
    _agy_host_policy(turn.command, (), probe=False)
    policy = json.loads(turn.command[turn.command.index("--settings") + 1])
    assert {"write_file(/tmp)", "write_file(/host-temp)"} <= set(policy["permissions"]["deny"])
    monkeypatch.setenv("TMPDIR", str(tmp_path.parent))
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(request(tmp_path))
    result = subprocess.run(
        _wrapped(turn.command), cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert result.stderr
    _agy_host_policy(turn.command, (), probe=False)
    policy = json.loads(turn.command[turn.command.index("--settings") + 1])
    assert f"write_file({tmp_path.parent.resolve()})" not in policy["permissions"]["deny"]


def test_every_profile_command_and_auth_prefix_suppress_browsers(tmp_path):
    fake = tmp_path / "fake-provider"
    fake.write_text(
        '#!/usr/bin/env python3\nimport json,os,subprocess\nprint(json.dumps({"browser":os.environ["BROWSER"],"codes":[subprocess.run([name]).returncode for name in ["open","xdg-open","sensible-browser",os.environ["BROWSER"]]],"cwd":os.getcwd()}))\n'
    )
    fake.chmod(0o700)
    commands = [
        AGY.auth_command(str(fake)),
        AGY.catalog_command(str(fake)),
        AGY.login_command(str(fake)),
        AGY.login_probe_command(str(fake)),
        AGY.skill_probe(str(fake)).command,
        AGY.work_like_probe_command(str(fake)),
        AGY.runtime(AGY.legacy_runtime_id)
        .turn(replace(request(tmp_path), binary=str(fake)))
        .command,
    ]
    for command in commands:
        result = subprocess.run(
            _wrapped(command), cwd=tmp_path, input="", capture_output=True, text=True, check=True
        )
        value = json.loads(result.stdout)
        assert value["codes"] == [1, 1, 1, 1]
        if "--rcp-agy-probe" in command:
            assert value["cwd"] != str(tmp_path)
    for host in ["", "test-host"]:
        prefix = AGY.authentication.unmanaged_environment(host).command_prefix
        result = subprocess.run(
            [*prefix, str(fake), "--version"], capture_output=True, text=True, check=True
        )
        assert json.loads(result.stdout)["codes"] == [1, 1, 1, 1]


def test_agent_digest_changes_with_tools_and_policy(tmp_path):
    runtime = AGY.runtime(AGY.legacy_runtime_id)
    first = runtime.turn(request(tmp_path)).command
    second = runtime.turn(replace(request(tmp_path), read_dirs=[Path("/other")])).command
    assert first[first.index("--agent") + 1] != second[second.index("--agent") + 1]


@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_malformed_nested_events_are_bounded(payload, tmp_path):
    from rcp.providers.agy.remote import AgyStreamTurnFence

    value = {"event": "result", "result": payload}
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(request(tmp_path))
    assert turn.receive_line(json.dumps(value)).complete
    assert turn.last_error
    fence = AgyStreamTurnFence(AGY.legacy_runtime_id)
    fence.input({"event": "user"})
    fence.output(value)
    assert fence.terminal
    value = {"event": "step_update", "step_update": payload}
    assert AGY.decode_event(value, json.dumps(value)).event == "raw"


def test_executed_policy_hides_canonical_paths_and_login_folders(tmp_path):
    from rcp.core.models import HiddenReadScope, HiddenReadStatus

    secret = tmp_path / "secret"
    secret.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(secret, target_is_directory=True)
    scope = HiddenReadScope(
        machine_id="backend",
        os_account="user",
        account_home="/home/user",
        hidden_directories=(
            str(alias.resolve()),
            "/data/provider-homes",
            "/home/user/.rcp/provider-homes",
        ),
        hidden_files=("/home/user/.gemini/antigravity-cli/antigravity-oauth-token",),
        hidden_globs=("/home/user/private-*.jsonl",),
        enforcement=HiddenReadStatus(status="enforced"),
    )
    fake = tmp_path / "capture"
    fake.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    fake.chmod(0o700)
    turn = AGY.runtime(AGY.legacy_runtime_id).turn(
        replace(request(tmp_path), binary=str(fake), hidden_read_scope=scope)
    )
    result = subprocess.run(
        _wrapped(turn.command), cwd=tmp_path, text=True, capture_output=True, check=True
    )
    actual = json.loads(result.stdout)
    policy = json.loads(actual[actual.index("--settings") + 1])
    deny = policy["permissions"]["deny"]
    assert {
        f"read_file({path})" for path in (*scope.hidden_directories, *scope.hidden_files)
    } <= set(deny)
    assert not any("private-*" in rule for rule in deny)
    assert f"write_file({tmp_path}/.agents)" in deny
    agent = tmp_path / ".agents" / "agents" / (actual[actual.index("--agent") + 1] + ".md")
    assert agent.exists()
    assert "glob_rules" in AGY.hidden_read_gaps


def test_profile_commands_leave_wrapping_to_authentication(tmp_path):
    binary = "/work/fake-agy"
    commands = [
        AGY.auth_command(binary),
        AGY.catalog_command(binary),
        AGY.login_command(binary),
        AGY.login_probe_command(binary),
        AGY.skill_probe(binary).command,
        AGY.work_like_probe_command(binary),
        AGY.runtime(AGY.legacy_runtime_id).turn(replace(request(tmp_path), binary=binary)).command,
    ]
    assert all(command[0] == binary and command.count(binary) == 1 for command in commands)
    for command in commands[-2:]:
        tools = json.loads(command[command.index("--rcp-agy-tools") + 1])
        assert "view_file" in tools


@pytest.mark.parametrize("capability", ["work_auto", "orchestrate", "paper_suggest"])
def test_work_read_context_never_becomes_an_editable_workspace(tmp_path, capability):
    from rcp.agents.write_scope import ProjectWriteScope

    scope = None
    writes = [tmp_path / "changes"]
    if capability != "paper_suggest":
        scope = ProjectWriteScope.create(
            project_id="project",
            machine_id="backend",
            capability=capability,
            stage_root=str(tmp_path),
            workspace_root=str(tmp_path / "workspace"),
            repositories=[],
            granted_roots=[str(tmp_path / "outputs")],
            protected_write_paths=[],
        )
        writes = []
    context = tmp_path / "read-context"
    command = (
        AGY.runtime(AGY.legacy_runtime_id)
        .turn(
            replace(
                request(tmp_path, capability),
                write_scope=scope,
                write_dirs=writes,
                read_dirs=[context],
            )
        )
        .command
    )
    add_dirs = {command[index + 1] for index, arg in enumerate(command) if arg == "--add-dir"}
    assert str(context) not in add_dirs
    assert add_dirs == set(scope.writable_roots if scope else map(str, writes)) - {str(tmp_path)}
    policy = json.loads(command[command.index("--settings") + 1])
    assert f"write_file({context})" not in policy["permissions"]["allow"]


def test_refused_action_kinds_are_named():
    event = AGY.decode_event(
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "denied_actions": [{"action": "command"}, {"action": "write_file"}],
            },
        },
        "",
    )
    assert event.event == "error"
    assert all(action in event.text for action in ("command", "write_file"))
