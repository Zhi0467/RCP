"""The suite must not run an installed provider CLI or outlive one it started."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.agents import launcher as launcher_module
from rcp.agents.launcher import AgentLauncher, AgentProcessControl
from rcp.config import Manifest

from .helpers import create_named_app, wait_until


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_local_provider_discovery_stays_unconfigured(provider: str) -> None:
    """`unconfigured_local_providers` must stay autouse.

    Discovery is what readiness runs `--version` and `login status` through, so
    a live path here is a suite that executes the developer's own authenticated
    CLI and bills a provider turn to prove nothing.
    """

    assert launcher_module._discover_local_provider(provider) is None
    readiness = AgentLauncher().readiness(provider)
    assert readiness.installed is False
    assert readiness.authenticated is False
    assert readiness.path_state == "unconfigured"


def test_a_real_dispatch_without_a_lifespan_stages_no_provider_process(
    manifest: Manifest, tmp_path: Path
) -> None:
    """A dispatch over the real launcher must refuse before a broker exists.

    This is the leak's exact shape: `create_app` with an unentered `TestClient`
    runs no lifespan, so nothing pauses the daemon worker this starts. A staged
    broker would outlive the pytest process that orphaned it, holding its
    `/tmp/rcp-command-*.sock` for as long as it ran.

    The staged broker script stands in for the socket: `/tmp` is shared machine
    state that a concurrent suite or the developer's own server also writes, so
    only this data directory can be asserted on.
    """

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    client = TestClient(app)

    started = client.post(
        f"/api/projects/{project_id}/tasks/seed",
        json={"run_truth_scope": ["repo-a"]},
    )
    assert started.status_code == 202, started.text
    operation_id = started.json()["operation_id"]
    store = app.state.background_tasks.store

    settled = wait_until(
        lambda: (
            record
            if (record := store.agent_task(operation_id)) is not None and not record.active
            else None
        ),
        detail=f"agent task {operation_id} never settled",
    )
    assert settled.status == "failed"
    assert "codex" in (settled.error or "")
    assert not list((tmp_path / "data" / "run-stage").glob("*/inputs/rcp-command-broker-*.py"))


@pytest.mark.asyncio
async def test_terminating_a_reused_pid_does_not_fail_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaped child's pid can already belong to another user.

    `killpg` then fails with EPERM rather than ESRCH. Our process is gone either
    way, and signalling the stranger that inherited its pid would be worse, so
    the turn must not fail on it.
    """

    class Reaped:
        returncode = None
        pid = 424242

        async def wait(self):
            return 0

    def killpg(_pid: int, _signal: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", killpg)

    await AgentProcessControl._terminate(Reaped())
