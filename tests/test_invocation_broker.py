from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rcp.agents.invocation_broker import ProviderInvocationGate


class _Stdin:
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


class _Stdout:
    async def readline(self) -> bytes:
        raise AssertionError("readiness must not be read after bootstrap failure")


class _Process:
    def __init__(self, *, first_wait_times_out: bool = False, kill_races: bool = False) -> None:
        self.stdin = _Stdin()
        self.stdout = _Stdout()
        self.stderr = None
        self.first_wait_times_out = first_wait_times_out
        self.kill_races = kill_races
        self.wait_calls = 0
        self.kill_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.first_wait_times_out and self.wait_calls == 1:
            raise TimeoutError
        return 0

    def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_races:
            raise ProcessLookupError


def _gate(tmp_path: Path) -> ProviderInvocationGate:
    return ProviderInvocationGate(
        mailbox_id="mailbox",
        broker_path="/tmp/broker.py",
        socket_path="/tmp/broker.sock",
        workspace=str(tmp_path),
        response_timeout_seconds=5.0,
        _token="secret",
    )


@pytest.mark.asyncio
async def test_broker_bootstrap_failure_still_reaps_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process = _Process()

    async def create_process(*_args: object, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(BrokenPipeError, match="broker exited before bootstrap"):
        async with _gate(tmp_path).serve_current_session():
            raise AssertionError("broker session should not become ready")

    assert process.stdin.closed is True
    assert process.stdin.wait_closed_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_broker_exit_race_does_not_replace_bootstrap_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process = _Process(first_wait_times_out=True, kill_races=True)

    async def create_process(*_args: object, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(BrokenPipeError, match="broker exited before bootstrap"):
        async with _gate(tmp_path).serve_current_session():
            raise AssertionError("broker session should not become ready")

    assert process.stdin.closed is True
    assert process.stdin.wait_closed_calls == 1
    assert process.wait_calls == 2
    assert process.kill_calls == 1
