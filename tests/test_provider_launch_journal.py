from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

from rcp.__main__ import _configure_logging
from rcp.agents import AgentLauncher, ProviderReadiness

_TOKEN_SHAPED = "sk-ant-oat01-SECRETSECRETSECRETSECRET"
_LAUNCHER_LOGGER = "rcp.agents.launcher"


def _codex_exec_launcher(monkeypatch: pytest.MonkeyPatch, *, exit_code: int) -> AgentLauncher:
    wire_events = [
        {"type": "thread.started", "thread_id": "session-test"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed"},
    ]
    script = (
        "import sys\nsys.stdin.read()\n"
        + "\n".join(f"print({json.dumps(event)!r}, flush=True)" for event in wire_events)
        + f"\nprint({_TOKEN_SHAPED!r}, file=sys.stderr, flush=True)\nraise SystemExit({exit_code})\n"
    )
    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *args, **kwargs: ProviderReadiness(
            provider="codex", installed=True, authenticated=True
        ),
    )
    monkeypatch.setattr(
        launcher, "_command", lambda *args, **kwargs: [sys.executable, "-c", script]
    )
    return launcher


async def _drain(launcher: AgentLauncher, tmp_path: Path) -> list:
    return [
        event
        async for event in launcher.stream(
            "codex",
            "inspect the task",
            cwd=tmp_path,
            capability="scratch_patch",
            operation_id="op-journal",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 3])
async def test_provider_process_leaves_two_journal_lines_and_none_of_its_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exit_code: int,
) -> None:
    """An operator tailing the unit sees a start and an end for every provider
    process, joined to the task by its operation id, and never the provider's
    own stderr: a CLI can print a token there."""

    launcher = _codex_exec_launcher(monkeypatch, exit_code=exit_code)
    with caplog.at_level(logging.INFO, logger=_LAUNCHER_LOGGER):
        events = await asyncio.wait_for(_drain(launcher, tmp_path), timeout=10)

    assert any(event.event == "provider_exit" for event in events)
    started = [
        r
        for r in caplog.records
        if "capability=" in r.getMessage() and "return_code=" not in r.getMessage()
    ]
    exited = [r for r in caplog.records if "return_code=" in r.getMessage()]
    assert len(started) == 1 and len(exited) == 1
    start_line, exit_line = started[0].getMessage(), exited[0].getMessage()
    assert "provider=codex" in start_line and "capability=scratch_patch" in start_line
    assert "host=local" in start_line and "operation=op-journal" in start_line
    assert "executable=codex" in start_line and "executable=codex" in exit_line
    pid = next(part for part in start_line.split() if part.startswith("pid=")).removeprefix("pid=")
    assert int(pid) > 0 and f"pid={pid}" in exit_line
    assert f"return_code={exit_code}" in exit_line and "duration_seconds=" in exit_line
    assert exited[0].levelno == (logging.INFO if exit_code == 0 else logging.WARNING)
    if exit_code:
        assert "reason=" in exit_line
    assert all(_TOKEN_SHAPED not in record.getMessage() for record in caplog.records)


def test_serve_attaches_one_stderr_handler_to_the_package_logger() -> None:
    package = logging.getLogger("rcp")
    before = list(package.handlers)
    for handler in before:
        package.removeHandler(handler)
    try:
        _configure_logging()
        _configure_logging()
        assert len(package.handlers) == 1
        assert isinstance(package.handlers[0], logging.StreamHandler)
        assert package.getEffectiveLevel() == logging.INFO
    finally:
        for handler in list(package.handlers):
            package.removeHandler(handler)
        package.setLevel(logging.NOTSET)
        for handler in before:
            package.addHandler(handler)
