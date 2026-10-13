from __future__ import annotations

import os

import pytest

from rcp.terminals import TerminalUnavailable, launch
from rcp.terminals.runtime import read_ready
from tests.test_terminals import arguments, unstoppable

pytest_plugins = ["tests.test_terminals"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stop", [False, True])
async def test_terminal_watermarks_follow_exposed_runtime_changes(
    manifest, manager, doubles, monkeypatch, failed_stop
):
    versions = [manager.resource_generation("project")]
    other = manager.resource_generation("other")
    session = await manager.open(**arguments(manifest))
    versions.append(manager.resource_generation("project"))
    queue = manager.attach("project", session.session_id)
    versions.append(manager.resource_generation("project"))
    manager.detach("project", session.session_id, queue)
    versions.append(manager.resource_generation("project"))
    await manager.write("project", session.session_id, b"input")
    versions.append(manager.resource_generation("project"))
    runtime = manager.sessions[session.session_id]
    os.close(doubles.slaves.pop())
    read_ready(manager, runtime)
    versions.append(manager.resource_generation("project"))
    if failed_stop:
        monkeypatch.setattr(launch, "stop_unit", unstoppable)
        with pytest.raises(TerminalUnavailable):
            await manager.end("project", session.session_id)
    else:
        await manager.end("project", session.session_id)
    versions.append(manager.resource_generation("project"))
    assert len(set(versions)) == len(versions)
    assert manager.resource_generation("other") == other
    assert manager.resource_generation("project") == versions[-1]
    assert manager.list("project") == []
