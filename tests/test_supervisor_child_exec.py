from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import signal
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor import child_exec

from tests.helpers import wait_until


class Prctl:
    def __init__(self, callback=lambda *_arguments: 0):
        self.callback = callback
        self.calls = []

    def __call__(self, *arguments):
        self.calls.append(arguments)
        return self.callback(*arguments)


def test_child_arms_after_credential_drop_and_checks_parent_twice(monkeypatch):
    parents = iter((42, 42))
    monkeypatch.setattr(child_exec.sys, "platform", "linux")
    monkeypatch.setattr(child_exec.os, "getppid", lambda: next(parents))
    prctl = Prctl()
    monkeypatch.setattr(
        child_exec.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=prctl)
    )
    child_exec.arm_parent_death(42)
    assert prctl.calls == [(38, 1, 0, 0, 0), (1, signal.SIGKILL, 0, 0, 0)]


@pytest.mark.parametrize("parents", [(1,), (42, 1)])
def test_parent_death_before_or_during_arming_never_executes(monkeypatch, parents, capsys):
    observed = iter(parents)
    monkeypatch.setattr(child_exec.sys, "platform", "linux")
    monkeypatch.setattr(child_exec.os, "getppid", lambda: next(observed))
    monkeypatch.setattr(
        child_exec.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=Prctl())
    )
    monkeypatch.setattr(child_exec.os, "execv", lambda *_args: pytest.fail("orphan exec"))
    assert child_exec.main(["42", "/unused/python", "-I", "-m", "rcp"]) == 1
    assert "coordinator disappeared" in capsys.readouterr().err


def test_parent_death_syscall_failure_never_executes(monkeypatch, capsys):
    monkeypatch.setattr(child_exec.sys, "platform", "linux")
    monkeypatch.setattr(child_exec.os, "getppid", lambda: 42)
    monkeypatch.setattr(child_exec.ctypes, "get_errno", lambda: errno.EPERM)
    monkeypatch.setattr(
        child_exec.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(prctl=Prctl(lambda *_args: -1)),
    )
    monkeypatch.setattr(child_exec.os, "execv", lambda *_args: pytest.fail("unowned exec"))
    assert child_exec.main(["42", "/unused/python"]) == 1
    assert "Could not establish" in capsys.readouterr().err


def test_non_linux_child_refuses_instead_of_losing_parent_ownership(monkeypatch, capsys):
    monkeypatch.setattr(child_exec.sys, "platform", "darwin")
    monkeypatch.setattr(child_exec.os, "execv", lambda *_args: pytest.fail("unsupported exec"))
    assert child_exec.main(["42", "/unused/python"]) == 1
    assert "requires Linux" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl and pidfd kernel proof")
def test_coordinator_death_kills_execed_child_and_releases_lock_and_listener(tmp_path):
    fixture = Path(__file__).with_name("supervisor_child_process.py")
    pidfd = None
    with (tmp_path / "child.log").open("w") as log:
        parent = subprocess.Popen(
            [
                sys.executable,
                str(fixture),
                "parent",
                str(tmp_path),
                str(Path(child_exec.__file__).resolve()),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            receipt = wait_until(
                lambda: (
                    json.loads((tmp_path / "ready.json").read_text())
                    if (tmp_path / "ready.json").exists()
                    and (tmp_path / "parent-closed-lock").exists()
                    else None
                ),
                timeout=10,
                detail="The real service-child fixture did not acquire its lock and listener.",
            )
            assert receipt["parent_pid"] == parent.pid
            pidfd = os.pidfd_open(receipt["pid"])
            with (
                (tmp_path / "data.lock").open("r+") as lock,
                (tmp_path / "deployment.lock").open("r+") as deployment_lock,
            ):
                assert receipt["deployment_lock_inode"] == os.fstat(deployment_lock.fileno()).st_ino
                for held in (lock, deployment_lock):
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(tmp_path / "control.sock"))
                parent.kill()
                parent.wait(timeout=5)
                wait_until(
                    lambda: bool(select.select([pidfd], [], [], 0)[0]),
                    timeout=5,
                    detail="The execed service child survived its coordinator's SIGKILL.",
                )
                for held in (lock, deployment_lock):
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with socket.socket(socket.AF_UNIX) as client, pytest.raises(ConnectionRefusedError):
                    client.connect(str(tmp_path / "control.sock"))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if pidfd is not None:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                os.close(pidfd)
