from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from rcp.server_ops.provider_update import (
    LocalUpdateTarget,
    ProviderUpdateRefused,
    RemoteUpdateTarget,
    update_installed_provider,
)
from tests.test_server_provider_update import _account, _layout


class Transport:
    def __init__(self, home: str) -> None:
        self.home = home
        self.version = "1.0"
        self.failure = False
        self.calls: list[tuple[str, ...]] = []
        self.destinations: list[str] = []
        self.mode = "700"

    def run(self, argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        output = ""
        code = 0
        if argv == ("printenv", "HOME"):
            output = self.home
        elif argv == ("id", "-u"):
            output = "1000"
        elif argv == ("uname", "-s"):
            output = "Linux"
        elif argv[0] == "stat":
            output = f"1000:{self.mode}:directory"
        elif argv[:2] == ("mktemp", "-d"):
            output = self.home + "/.rcp/tmp/rcp-provider-12345"
        elif argv[:2] == ("sh", "-lc"):
            output = self.home + "/.local/bin/" + shlex.split(argv[2])[-1]
        elif argv[-1] == "--version":
            output = self.version
        elif argv[-1] in {"upgrade", "update"}:
            code = int(self.failure)
            if not code:
                self.version = "2.0"
        elif argv[0] in {"mkdir", "rm", "/usr/bin/curl", "/usr/bin/env"}:
            code = int(self.failure)
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, code, output, "failed" if code else "")

    def ssh(self, argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "ssh"
        self.destinations.append(argv[-2])
        return self.run(tuple(shlex.split(argv[-1])), timeout)


@pytest.fixture(params=["local", "remote"])
def target(request, tmp_path):
    account = _account(_layout(tmp_path))
    transport = Transport(account.pw_dir)
    if request.param == "local":
        result = LocalUpdateTarget(account, lambda _, argv, timeout: transport.run(argv, timeout))
    else:
        result = RemoteUpdateTarget("machine", "worker", runner=transport.ssh)
    return result, transport


def test_native_update_verifies_new_version(target) -> None:
    destination, transport = target
    binary = destination.home + "/.local/bin/opencode"
    if isinstance(destination, LocalUpdateTarget):
        Path(binary).parent.mkdir(parents=True)
        Path(binary).touch(mode=0o755)
    change = update_installed_provider(destination, "opencode", binary)
    assert (change.before, change.after) == ("1.0", "2.0")
    if transport.destinations:
        assert set(transport.destinations) == {"worker@machine"}


def test_native_update_failure_is_reported(target) -> None:
    destination, transport = target
    transport.failure = True
    with pytest.raises(ProviderUpdateRefused):
        update_installed_provider(destination, "opencode", destination.home + "/bin/opencode")
    assert transport.version == "1.0"


@pytest.mark.parametrize("binary", ["/usr/local/bin/opencode", "/home/../usr/bin/opencode"])
def test_outside_home_is_refused_without_running_binary(target, binary: str) -> None:
    destination, transport = target
    assert destination.home
    transport.calls.clear()
    with pytest.raises(ProviderUpdateRefused):
        update_installed_provider(destination, "opencode", binary)
    assert transport.calls == []


def test_remote_installer_uses_private_directory_and_cleans_it() -> None:
    transport = Transport("/home/worker")
    target = RemoteUpdateTarget("other@machine", "worker", runner=transport.ssh)
    update_installed_provider(target, "codex", target.home + "/.local/bin/codex")
    assert set(transport.destinations) == {"worker@machine"}
    assert ("rm", "-rf", "--", "/home/worker/.rcp/tmp/rcp-provider-12345") in transport.calls


def test_remote_refuses_unsafe_temporary_directory() -> None:
    transport = Transport("/home/worker")
    target = RemoteUpdateTarget("machine", "worker", runner=transport.ssh)
    transport.mode = "777"
    with pytest.raises(ProviderUpdateRefused), target.temp_dir():
        pass
    assert not any(call[0] == "mktemp" for call in transport.calls)


@pytest.mark.parametrize("mode", ["777", "755"])
def test_remote_installer_rejects_nonprivate_new_directory(mode: str) -> None:
    transport = Transport("/home/worker")
    original = transport.run

    def run(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "stat" and "rcp-provider-" in argv[-1]:
            transport.mode = mode
        return original(argv, timeout)

    transport.run = run
    target = RemoteUpdateTarget("machine", "worker", runner=transport.ssh)
    with pytest.raises(ProviderUpdateRefused), target.temp_dir():
        pass
    assert any(call[0] == "rm" for call in transport.calls)


def test_remote_installer_failure_still_removes_staged_directory() -> None:
    transport = Transport("/home/worker")
    original = transport.run

    def run(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "/usr/bin/curl":
            return subprocess.CompletedProcess(argv, 1, "", "download failed")
        return original(argv, timeout)

    transport.run = run
    target = RemoteUpdateTarget("machine", "worker", runner=transport.ssh)
    with pytest.raises(ProviderUpdateRefused):
        update_installed_provider(target, "codex", target.home + "/bin/codex")
    assert any(call[0] == "rm" for call in transport.calls)


def test_remote_agy_maintenance_sends_only_plain_commands() -> None:
    transport = Transport("/home/worker")
    target = RemoteUpdateTarget("machine", "worker", runner=transport.ssh)
    update_installed_provider(target, "agy", target.home + "/.local/bin/agy")
    commands = [call for call in transport.calls if call[0] == "env"]
    assert len(commands) == 3
    assert all(call[:2] == ("env", "BROWSER=/bin/false") for call in commands)
    assert [call[-1] for call in commands] == ["--version", "update", "--version"]


@pytest.mark.parametrize("host", ["machine", "worker@machine"])
def test_remote_target_resolves_unspecified_account_lazily(host: str) -> None:
    transport = Transport("/home/worker")
    original = transport.run

    def run(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv == ("id", "-un"):
            return subprocess.CompletedProcess(argv, 0, "worker\n", "")
        return original(argv, timeout)

    transport.run = run
    target = RemoteUpdateTarget(host, "", runner=transport.ssh)
    assert transport.destinations == []
    change = update_installed_provider(target, "opencode", target.home + "/bin/opencode")
    assert (change.before, change.after) == ("1.0", "2.0")
    assert transport.destinations[0] == host
    assert set(transport.destinations[1:]) == {"worker@machine"}


def test_update_verifies_the_selected_executable_when_discovery_differs(target):
    destination, transport = target
    binary = destination.home + "/custom/codex"
    if isinstance(destination, LocalUpdateTarget):
        discovered = Path(destination.home) / ".local/bin/codex"
        discovered.parent.mkdir(parents=True)
        discovered.touch(mode=0o755)
    original = transport.run

    def run(argv, timeout):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "1.0" if argv[-2] == binary else "2.0", "")
        return original(argv, timeout)

    transport.run = run
    change = update_installed_provider(destination, "codex", binary)
    assert (change.before, change.after) == ("1.0", "1.0")
