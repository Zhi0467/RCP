from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import ValidationError

from rcp.compute import (
    _probe_one,
    compute_probe_cache_key,
    machine_compute_context,
    probe_compute_connections,
)
from rcp.config import (
    Manifest,
    ResolvedComputeContext,
    ResolvedComputeProfile,
)
from rcp.operator_commands import operator_route
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


@pytest.mark.parametrize("space_kind", ["personal", "team"])
@pytest.mark.parametrize(
    ("stderr", "state"),
    [
        ("Permission denied (publickey).", "authentication_failed"),
        ("Host key verification failed.", "host_key_failed"),
    ],
)
def test_ssh_failure_action_carries_the_operator_routed_check(
    space_kind: str, stderr: str, state: str
) -> None:
    connection = ResolvedComputeProfile(
        id="gpu",
        name="gpu",
        kind="ssh",
        ssh_target="alice@gpu.example",
    )

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 255, "", stderr)

    result = _probe_one(
        connection,
        execution_machine="lab-mac",
        execution_host="",
        space_kind=space_kind,
        runner=runner,
    )

    assert result.state == state
    assert result.reachable is False
    assert result.status_tone == "error"
    check = operator_route(space_kind).command(["ssh", "alice@gpu.example"])
    assert result.required_action is not None and check in result.required_action


def test_probe_redacts_and_normalizes_remote_payload_diagnostics() -> None:
    connection = ResolvedComputeProfile(
        id="gpu",
        name="gpu",
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
        space_kind="personal",
        runner=runner,
    )

    assert result.state == "unreachable"
    assert result.status_tone == "error"
    assert "super-secret-token" not in result.diagnostic
    assert "abcdefghijklmnop" not in result.diagnostic
    assert "[REDACTED]" in result.diagnostic
    assert "\n" not in result.diagnostic


def test_probe_redacts_credential_shaped_remote_stderr() -> None:
    connection = ResolvedComputeProfile(
        id="gpu",
        name="gpu",
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
        space_kind="personal",
        runner=runner,
    )

    assert result.state == "unreachable"
    assert "abcdefghijklmnop" not in result.diagnostic
    assert "hunter2" not in result.diagnostic
    assert "Authorization: [REDACTED]" in result.diagnostic
    assert "password=[REDACTED]" in result.diagnostic
    assert "\n" not in result.diagnostic


def test_probe_redacts_runtime_errors_from_ssh_setup(monkeypatch) -> None:
    connection = ResolvedComputeProfile(
        id="gpu",
        name="gpu",
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
        space_kind="personal",
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert result.state == "unreachable"
    assert "hunter2" not in result.diagnostic
    assert "/tmp/key" not in result.diagnostic
    assert "password=[REDACTED]" in result.diagnostic
    assert "private_key=[REDACTED]" in result.diagnostic
    assert "\n" not in result.diagnostic


def test_probe_matrix_covers_other_routable_machines_from_each_agent_machine(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["machines"].extend(
        [
            {"alias": "remote-agent", "host": "agent.example", "os_account": "researcher"},
            {"alias": "gpu", "host": "gpu.example", "os_account": "alice"},
            {"alias": "server-local", "host": ""},
        ]
    )
    payload["agent"]["paper_coach"]["run_on"] = "remote-agent"
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

    status = probe_compute_connections(configured, space_kind="personal", runner=runner)

    assert {alias: set(row) for alias, row in status.items()} == {
        "laptop": {"remote-agent", "gpu"},
        "remote-agent": {"gpu"},
    }
    assert status["remote-agent"]["gpu"]["reachable"] is True
    remote_runs = [command for command in commands if "agent.example" in command]
    assert len(remote_runs) == 1
    assert "remote_compute_probe.py" not in remote_runs[0][-1]
    assert "alice@gpu.example" in remote_runs[0][-1]


def test_probe_cache_key_follows_routes_but_not_access_hints(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["machines"].append({"alias": "gpu", "host": "gpu.example", "os_account": "alice"})
    base = compute_probe_cache_key(Manifest.model_validate(payload))

    payload["machines"][-1]["access_hint"] = "Use /scratch"
    assert compute_probe_cache_key(Manifest.model_validate(payload)) == base
    payload["machines"][-1]["os_account"] = "bob"
    assert compute_probe_cache_key(Manifest.model_validate(payload)) != base


def test_every_machine_the_manifest_accepts_resolves(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["machines"].extend(
        [{"alias": "old box", "host": "fe80::1", "os_account": "alice"}]
        + [{"alias": f"node-{index}", "host": f"node-{index}.example"} for index in range(40)]
    )
    context = machine_compute_context(
        Manifest.model_validate(payload), execution_machine=manifest.machines[0].alias
    )

    assert len(context.active) == len(payload["machines"])
    assert ("old box", "alice@fe80::1") in {(p.id, p.ssh_target) for p in context.active}


def test_each_machine_carries_its_own_job_manager_into_the_prompt(manifest) -> None:
    from rcp.agents.continuation_prompt import _compute_delta_section
    from rcp.agents.prompts import compute_connection_section
    from rcp.compute_jobs.job_managers import JOB_MANAGERS

    payload = manifest.model_dump(mode="python")
    payload["machines"].append(
        {"alias": "cluster", "host": "cluster.example", "compute": {"job_manager": "slurm"}}
    )
    profiles = [
        profile.model_dump(mode="json")
        for profile in machine_compute_context(
            Manifest.model_validate(payload), execution_machine=manifest.machines[0].alias
        ).active
    ]
    rules = JOB_MANAGERS["slurm"].instructions

    assert [p["job_manager"] for p in profiles] == [""] * (len(profiles) - 1) + ["slurm"]
    assert compute_connection_section(profiles).count(rules) == 1
    assert rules not in compute_connection_section(profiles[:-1])
    assert rules in _compute_delta_section({"updated": [profiles[-1]]})


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "ssh", "ssh_target": "alice@gpu.example", "access_hint": "line\nbreak"},
        {"kind": "ssh", "ssh_target": "alice@gpu.example", "access_hint": "password=hunter2"},
        {"kind": "ssh", "ssh_target": "alice@gpu.example", "access_hint": "use ~/.ssh/id_rsa"},
        {"kind": "ssh", "ssh_target": "alice@gpu.example;rm"},
        {"kind": "ssh", "ssh_target": ""},
        {"kind": "local", "ssh_target": "alice@gpu.example"},
    ],
)
def test_resolved_compute_profile_refuses_unsafe_metadata(fields) -> None:
    with pytest.raises(ValidationError):
        ResolvedComputeProfile(id="gpu", name="gpu", **fields)


def test_resolved_compute_context_is_immutable() -> None:
    context = ResolvedComputeContext(
        active=(ResolvedComputeProfile(id="gpu", name="GPU", kind="local"),)
    )

    with pytest.raises(ValidationError, match="Instance is frozen"):
        context.active[0].name = "Changed"  # type: ignore[misc]


def test_watcher_row_with_a_frozen_compute_selection_still_loads() -> None:
    from rcp.storage.models import WatcherContinuation

    continuation = WatcherContinuation.model_validate(
        {
            "provider": "codex",
            "run_on": "laptop",
            "active_compute_ids": ["gpu"],
            "resolved_compute_context": {"active": []},
        }
    )

    assert "active_compute_ids" not in continuation.model_dump()
    assert "resolved_compute_context" not in continuation.model_dump()
