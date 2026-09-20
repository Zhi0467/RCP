from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import threading
from pathlib import Path

import pytest

from rcp.config import MachineConfig
from rcp.terminals.probe import TerminalProbe, TerminalProbeCache, probe_remote_terminal
from rcp.transport import remote_terminal_probe


@pytest.fixture
def machine():
    return MachineConfig(alias="remote", host="terminal.example")


@pytest.mark.parametrize(
    ("returncode", "diagnostic", "state"),
    [
        (255, "ssh: connect to host: Connection refused", "unreachable"),
        (255, "Permission denied (publickey).", "authentication_failed"),
        (255, "Host key verification failed.", "host_key_failed"),
        (127, "python3: command not found", "incapable"),
    ],
)
def test_probe_classifies_failures(machine, returncode, diagnostic, state):
    result = probe_remote_terminal(
        machine,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, returncode, "", diagnostic
        ),
    )
    assert result.state == state
    assert result.diagnostic == diagnostic
    assert not result.ready


def test_probe_timeout_is_unreachable(machine):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = probe_remote_terminal(machine, runner=run)
    assert result.state == "unreachable"
    assert result.diagnostic == "The terminal capability probe timed out."


@pytest.mark.parametrize("os_name", ["Linux", "Darwin", "FreeBSD"])
def test_probe_ships_source_and_uses_execution_machine_os(machine, os_name):
    def run(command, **kwargs):
        assert command[0] == "ssh"
        assert "StrictHostKeyChecking=yes" in command
        assert command[-2] == machine.host
        remote = shlex.split(command[-1])
        assert remote[:2] == ["python3", "-c"]
        assert remote[2] == Path(remote_terminal_probe.__file__).read_text()
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {"os_name": os_name, "state": "reachable", "diagnostic": "Ready to launch."}
            ),
            "",
        )

    result = probe_remote_terminal(machine, runner=run)
    assert result.os_name == os_name
    assert result.ready


@pytest.mark.parametrize("payload", ["invalid", "{}", "[]", '{"state":"reachable","os_name":""}'])
def test_probe_invalid_reply_is_incapable(machine, payload):
    result = probe_remote_terminal(
        machine,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, payload, ""),
    )
    assert result.state == "incapable"


@pytest.fixture
def remote_linux(monkeypatch):
    monkeypatch.setattr(remote_terminal_probe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(remote_terminal_probe.os, "access", lambda *args: True)
    monkeypatch.setattr(remote_terminal_probe.shutil, "which", lambda name: f"/usr/bin/{name}")


@pytest.mark.parametrize("missing", ["systemd-run", "systemctl", "findmnt"])
def test_linux_missing_prerequisite_is_incapable(remote_linux, monkeypatch, missing):
    monkeypatch.setattr(
        remote_terminal_probe.shutil, "which", lambda name: None if name == missing else name
    )
    result = remote_terminal_probe.probe_machine(
        command_timeout=1, runner=lambda *args, **kwargs: pytest.fail("Unexpected command")
    )
    assert result["state"] == "incapable"
    assert result["os_name"] == "Linux"
    assert missing in result["diagnostic"]


@pytest.mark.parametrize("failed", ["systemd-run", "findmnt", "systemctl"])
def test_linux_unusable_prerequisite_is_incapable(remote_linux, failed):
    def run(command, **kwargs):
        assert kwargs["env"]["XDG_RUNTIME_DIR"].startswith("/run/user/")
        return subprocess.CompletedProcess(
            command,
            int(command[0] == failed),
            "yes",
            "broken prerequisite" if command[0] == failed else "",
        )

    result = remote_terminal_probe.probe_machine(command_timeout=1, runner=run)
    assert result["state"] == "incapable"
    assert "broken prerequisite" in result["diagnostic"]


def test_linux_does_not_require_lingering_manager(remote_linux):
    """A terminal's PTY lives inside the SSH session that owns the manager.

    Requiring linger rejected every real machine this feature was built for,
    while a session-scoped manager runs transient units perfectly well and
    tears down with the link, which is what a terminal wants.
    """
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = remote_terminal_probe.probe_machine(command_timeout=1, runner=run)
    assert result["state"] == "reachable"
    assert ["systemctl", "--user", "show-environment"] in commands
    assert all(command[0] != "loginctl" for command in commands)


def test_linux_prerequisite_timeout_is_incapable(remote_linux):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = remote_terminal_probe.probe_machine(command_timeout=1, runner=run)
    assert result["state"] == "incapable"
    assert result["diagnostic"] == "Terminal prerequisite systemd-run timed out."


def test_remote_shell_is_required(remote_linux, monkeypatch):
    monkeypatch.setattr(remote_terminal_probe.os, "access", lambda *args: False)
    result = remote_terminal_probe.probe_machine(command_timeout=1)
    assert result["state"] == "incapable"
    assert "/bin/bash" in result["diagnostic"]


def test_non_linux_selects_cooperative_without_linux_probe(remote_linux, monkeypatch):
    monkeypatch.setattr(remote_terminal_probe.platform, "system", lambda: "Darwin")
    result = remote_terminal_probe.probe_machine(
        command_timeout=1, runner=lambda *args, **kwargs: pytest.fail("Unexpected Linux command")
    )
    assert result["state"] == "reachable"
    assert result["os_name"] == "Darwin"
    assert "protection is unavailable" in result["diagnostic"]


@pytest.mark.asyncio
async def test_cache_miss_is_pending_hit_shared_and_explicit_invalidation(machine):
    release = threading.Event()
    calls = []
    ready = TerminalProbe("Linux", "reachable", "Ready.")

    def probe(value):
        calls.append(value.host)
        assert release.wait(5)
        return ready

    cache = TerminalProbeCache(probe)
    try:
        assert cache.get(machine).state == "pending"
        assert cache.get(machine).state == "pending"
        release.set()
        assert await cache.ensure(machine) == ready
        assert cache.get(machine) == ready
        assert calls == [machine.host]
        cache.invalidate(machine)
        assert cache.get(machine).state == "pending"
        assert await cache.ensure(machine) == ready
        assert calls == [machine.host, machine.host]
        cache.invalidate()
        assert cache.get(machine).state == "pending"
    finally:
        release.set()
        await cache.close()


@pytest.mark.asyncio
async def test_cache_configuration_change_is_a_miss(machine):
    cache = TerminalProbeCache(lambda machine: TerminalProbe("Linux", "reachable", machine.host))
    try:
        await cache.ensure(machine)
        changed = machine.model_copy(update={"host": "changed.example"})
        assert cache.get(changed).state == "pending"
        assert (await cache.ensure(changed)).diagnostic == changed.host
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_invalidated_inflight_probe_cannot_replace_new_result(machine):
    release = threading.Event()
    started = threading.Event()
    call_count = 0

    def probe(machine):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            started.set()
            assert release.wait(5)
            return TerminalProbe("Linux", "incapable", "Stale result.")
        return TerminalProbe("Linux", "reachable", "Fresh result.")

    cache = TerminalProbeCache(probe)
    try:
        waiter = asyncio.create_task(cache.ensure(machine))
        assert await asyncio.to_thread(started.wait, 5)
        cache.invalidate(machine)
        assert (await cache.ensure(machine)).diagnostic == "Fresh result."
        release.set()
        assert (await waiter).diagnostic == "Fresh result."
        assert cache.get(machine).diagnostic == "Fresh result."
    finally:
        release.set()
        await cache.close()


@pytest.mark.asyncio
async def test_closed_cache_does_not_start_another_probe(machine):
    cache = TerminalProbeCache(lambda machine: pytest.fail("Unexpected probe"))
    await cache.close()
    with pytest.raises(RuntimeError, match="closed"):
        cache.get(machine)


@pytest.mark.parametrize(
    ("registered", "answered", "offered"),
    [
        ("rcp", "rcp", True),
        # An unregistered account has nothing to compare, so nothing is refused.
        ("", "someone-else", True),
        ("rcp", "", True),
        ("rcp", "someone-else", False),
    ],
)
def test_a_machine_answering_as_another_account_is_not_offered(registered, answered, offered):
    """A destination carrying no user takes its account from the client's SSH
    configuration, which can drift away from the registered one. A shell there
    would have the wrong home, credentials and write authority.
    """
    from rcp.terminals.backends import machine_capability

    machine = MachineConfig(alias="remote", host="terminal.example", os_account=registered)
    probe = TerminalProbe("Linux", "reachable", "Ready.", 254, answered)
    capability = machine_capability(machine, probe)
    assert (capability.backend is not None) is offered
    if not offered:
        assert "different account" in capability.reason


def test_the_shipped_probe_reports_the_account_it_answers_as():
    """The server cannot see which account SSH landed on; only the far side can."""
    import os
    import pwd

    def ready(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "systemd 254 (254)\n", "")

    payload = remote_terminal_probe.probe_machine(command_timeout=1, runner=ready)
    assert payload["os_account"] == pwd.getpwuid(os.geteuid()).pw_name
