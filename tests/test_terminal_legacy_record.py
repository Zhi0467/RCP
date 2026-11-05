"""A terminal record an older build wrote names its machine by host, not by card."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tty

import pytest

from rcp.agents.write_scope import registered_repository_roots
from rcp.storage import AppStore
from rcp.terminals import TerminalManager, TerminalUnavailable, launch


class _Process:
    returncode: int | None = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode


def _legacy_record(manager: TerminalManager, execution_host: str) -> None:
    record = {
        "session_id": "legacy",
        "project_id": "project",
        "member_id": "member",
        "repository_id": "repo-a",
        "path": "/checkout",
        "started_at": "start",
        "last_activity_at": "start",
        "unit": f"{manager._unit_prefix}-legacy",
        "declared_path": "/checkout",
        "declared_machine": "laptop",
        "declared_account": "",
        "execution_host": execution_host,
        "containment": "mirrored",
        "state": "idle",
        "ended_at": "end",
        "termination_reason": "idle",
    }
    (manager.directory / "legacy.json").write_text(json.dumps(record))


@pytest.mark.asyncio
@pytest.mark.parametrize(("execution_host", "opens"), [("", True), ("gone.example", False)])
async def test_an_old_writer_record_resolves_through_its_card(
    manifest, tmp_path, monkeypatch, execution_host, opens
):
    slaves: list[int] = []

    def start(command, unit):
        master, slave = os.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        slaves.append(slave)
        return _Process(), master

    async def never_sweeps(manager):
        await asyncio.Event().wait()

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch, "launch", start)
    monkeypatch.setattr(launch, "stop_unit", lambda unit: None)
    monkeypatch.setattr("rcp.terminals.manager.sweep_loop", never_sweeps)
    manager = TerminalManager(
        tmp_path / "data", lambda _project, _member: True, AppStore(tmp_path / "app.sqlite3")
    )
    manager.directory.mkdir(parents=True)
    _legacy_record(manager, execution_host)
    arguments = dict(
        project_id="project",
        member_id="member",
        manifest=manifest,
        repository_alias="repo-a",
        repository_inventory=registered_repository_roots(manifest, project_id="project"),
    )
    try:
        await manager.start()
        if opens:
            await manager.open(**arguments)
        else:
            with pytest.raises(TerminalUnavailable):
                await manager.open(**arguments)
    finally:
        with contextlib.suppress(TerminalUnavailable):
            await manager.close()
        for descriptor in slaves:
            os.close(descriptor)
