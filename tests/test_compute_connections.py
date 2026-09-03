from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import ValidationError

from rcp.compute import _probe_one, probe_compute_connections, selected_compute_connections
from rcp.config import (
    ComputeConnectionConfig,
    Manifest,
    ResolvedComputeContext,
    ResolvedComputeProfile,
)
from rcp.limits import ACTIVE_COMPUTE_ID_MAX_COUNT
from rcp.service import RunRequest
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
    assert result.status_label == "Authentication failed"
    assert result.status_tone == "error"
    assert 'agent machine "lab-mac"' in result.required_action
    assert "does not collect keys or passwords" in result.required_action


def test_probe_redacts_and_normalizes_remote_payload_diagnostics() -> None:
    connection = ComputeConnectionConfig(
        id="gpu",
        name="GPU VM",
        kind="ssh",
        ssh_target="alice@gpu.example",
    )

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "state": "unreachable",
                    "diagnostic": "token=super-secret-token\nBearer abcdefghijklmnop",
                }
            ),
            "",
        )

    result = _probe_one(
        connection,
        execution_machine="remote-agent",
        execution_host="agent.example",
        runner=runner,
    )

    assert result.state == "unreachable"
    assert result.status_label == "Unreachable"
    assert result.status_tone == "error"
    assert "super-secret-token" not in result.diagnostic
    assert "abcdefghijklmnop" not in result.diagnostic
    assert "[REDACTED]" in result.diagnostic
    assert "\n" not in result.diagnostic


def test_probe_redacts_credential_shaped_remote_stderr() -> None:
    connection = ComputeConnectionConfig(
        id="gpu",
        name="GPU VM",
        kind="ssh",
        ssh_target="alice@gpu.example",
    )

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            255,
            "",
            "Authorization: Bearer abcdefghijklmnop\npassword=hunter2",
        )

    result = _probe_one(
        connection,
        execution_machine="remote-agent",
        execution_host="agent.example",
        runner=runner,
    )

    assert result.state == "unreachable"
    assert "abcdefghijklmnop" not in result.diagnostic
    assert "hunter2" not in result.diagnostic
    assert "Authorization: [REDACTED]" in result.diagnostic
    assert "password=[REDACTED]" in result.diagnostic
    assert "\n" not in result.diagnostic


def test_probe_redacts_runtime_errors_from_ssh_setup(monkeypatch) -> None:
    connection = ComputeConnectionConfig(
        id="gpu",
        name="GPU VM",
        kind="ssh",
        ssh_target="alice@gpu.example",
    )

    def fail_ssh_arguments(*_args, **_kwargs):
        raise RuntimeError("password=hunter2\nprivate_key=/tmp/key")

    monkeypatch.setattr("rcp.compute.ssh_arguments", fail_ssh_arguments)
    result = _probe_one(
        connection,
        execution_machine="remote-agent",
        execution_host="agent.example",
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert result.state == "unreachable"
    assert "hunter2" not in result.diagnostic
    assert "/tmp/key" not in result.diagnostic
    assert "password=[REDACTED]" in result.diagnostic
    assert "private_key=[REDACTED]" in result.diagnostic
    assert "\n" not in result.diagnostic


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


def test_active_compute_ids_are_bounded_before_request_or_prompt_assembly(manifest) -> None:
    ids = [f"compute-{index}" for index in range(ACTIVE_COMPUTE_ID_MAX_COUNT + 1)]

    with pytest.raises(ValueError, match="at most 32 items"):
        RunRequest(active_compute_ids=ids)
    with pytest.raises(ValueError, match="exceed the limit of 32"):
        selected_compute_connections(manifest, ids)

    profiles = tuple(
        ResolvedComputeProfile(id=compute_id, name=compute_id, kind="local") for compute_id in ids
    )
    with pytest.raises(ValidationError, match="at most 32 items"):
        ResolvedComputeContext(active=profiles)


def test_resolved_compute_context_is_immutable() -> None:
    context = ResolvedComputeContext(
        active=(ResolvedComputeProfile(id="gpu", name="GPU", kind="local"),)
    )

    with pytest.raises(ValidationError, match="Instance is frozen"):
        context.active[0].name = "Changed"  # type: ignore[misc]
