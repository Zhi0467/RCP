from __future__ import annotations

import json
import subprocess

import pytest

from rcp.compute import _probe_one, probe_compute_connections, selected_compute_connections
from rcp.config import ComputeConnectionConfig, Manifest
from rcp.transport.remote_compute_probe import classify_ssh_failure, probe_connection


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("ssh: connect to host gpu port 22: Operation timed out", "unreachable"),
        ("Permission denied (publickey).", "authentication_failed"),
        ("Host key verification failed.", "host_key_failed"),
        ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", "host_key_failed"),
    ],
)
def test_ssh_probe_distinguishes_actionable_failures(diagnostic: str, expected: str) -> None:
    assert classify_ssh_failure(diagnostic) == expected


def test_ssh_probe_uses_existing_credentials_and_strict_host_key_checking() -> None:
    seen: list[str] = []

    def runner(command, **kwargs):
        seen.extend(command)
        assert kwargs["timeout"] == 15
        return subprocess.CompletedProcess(command, 255, "", "Permission denied (publickey).")

    result = probe_connection("ssh", "alice@gpu.example", runner=runner)

    assert result["state"] == "authentication_failed"
    assert "BatchMode=yes" in seen
    assert "StrictHostKeyChecking=yes" in seen
    assert "alice@gpu.example" in seen


def test_authentication_failure_names_the_agent_execution_machine() -> None:
    connection = ComputeConnectionConfig(
        id="gpu",
        name="GPU VM",
        kind="ssh",
        ssh_target="alice@gpu.example",
    )

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 255, "", "Permission denied (publickey).")

    result = _probe_one(
        connection,
        execution_machine="lab-mac",
        execution_host="",
        runner=runner,
    )

    assert result.state == "authentication_failed"
    assert result.reachable is False
    assert 'agent machine "lab-mac"' in result.required_action
    assert "does not collect keys or passwords" in result.required_action


def test_remote_execution_machine_runs_the_shipped_probe_there(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["machines"].append(
        {"alias": "remote-agent", "host": "agent.example", "os_account": "researcher"}
    )
    payload["agent"]["paper_coach"]["run_on"] = "remote-agent"
    payload["compute_connections"] = [{"id": "current", "name": "Current machine", "kind": "local"}]
    configured = Manifest.model_validate(payload)
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"state": "reachable", "diagnostic": "Available."}),
            "",
        )

    status = probe_compute_connections(configured, runner=runner)

    assert status["laptop"]["current"]["reachable"] is True
    assert status["remote-agent"]["current"]["reachable"] is True
    assert len(commands) == 1
    assert "agent.example" in commands[0]
    assert "remote_compute_probe.py" not in commands[0][-1]
    assert "StrictHostKeyChecking=no" not in commands[0][-1]


def test_compute_selection_rejects_unknown_ids_without_affecting_run_on(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["compute_connections"] = [
        {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "alice@gpu"}
    ]
    configured = Manifest.model_validate(payload)

    assert selected_compute_connections(configured, ["gpu"])[0].name == "GPU"
    assert configured.agent_profile("project_chat").run_on == "laptop"
    with pytest.raises(ValueError, match="unknown compute connections"):
        selected_compute_connections(configured, ["missing"])
