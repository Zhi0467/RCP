from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rcp.agents.invocation_broker import ProviderInvocationGate


@pytest.mark.asyncio
async def test_broker_bootstrap_failure_still_reaps_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Stdin:
        def __init__(self) -> None:
            self.closed = False
            self.wait_closed_calls = 0

        def write(self, _value: bytes) -> None:
            return None

        async def drain(self) -> None:
            raise BrokenPipeError("broker exited before bootstrap")

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            self.wait_closed_calls += 1

    class Stdout:
        async def readline(self) -> bytes:
            raise AssertionError("readiness must not be read after bootstrap failure")

    class Process:
        def __init__(self) -> None:
            self.stdin = Stdin()
            self.stdout = Stdout()
            self.stderr = None
            self.wait_calls = 0
            self.kill_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            return 0

        def kill(self) -> None:
            self.kill_calls += 1

    process = Process()

    async def create_process(*_args: object, **_kwargs: object) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    gate = ProviderInvocationGate(
        mailbox_id="mailbox",
        broker_path="/tmp/broker.py",
        socket_path="/tmp/broker.sock",
        workspace=str(tmp_path),
        response_timeout_seconds=5.0,
        _token="secret",
    )

    with pytest.raises(BrokenPipeError, match="broker exited before bootstrap"):
        async with gate.serve_current_session():
            raise AssertionError("broker session should not become ready")

    assert process.stdin.closed is True
    assert process.stdin.wait_closed_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0
