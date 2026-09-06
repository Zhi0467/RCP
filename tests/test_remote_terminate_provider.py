from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from rcp.transport import remote_terminate_provider


@pytest.fixture
def owned_group(tmp_path):
    processes: list[subprocess.Popen] = []
    reapers: list[threading.Thread] = []

    def launch(*, ignore_term: bool):
        program = (
            "import os,signal,sys,time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "print(os.getpid(), flush=True)\nwhile True: time.sleep(1)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(process)
        assert process.stdout is not None
        assert process.stdout.readline().strip() == str(process.pid)
        pid_file = tmp_path / f"{process.pid}.pid"
        pid_file.write_text(str(process.pid))
        # The remote wrapper's parent reaps its child. Reproduce that ownership
        # locally so killpg(..., 0) does not keep seeing an unreaped test zombie.
        reaper = threading.Thread(target=process.wait, daemon=True)
        reapers.append(reaper)
        reaper.start()
        return process, pid_file

    yield launch
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    for reaper in reapers:
        reaper.join(timeout=5)
        assert not reaper.is_alive()


@pytest.mark.parametrize("ignore_term", [False, True])
def test_shipped_helper_confirms_stop_and_escalates_when_needed(owned_group, ignore_term):
    process, pid_file = owned_group(ignore_term=ignore_term)
    source = Path(remote_terminate_provider.__file__).read_text()
    result = subprocess.run(
        [sys.executable, "-c", source, str(pid_file), "0.5", "0.1", "2", "0.01"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert process.wait(timeout=5) == -(signal.SIGKILL if ignore_term else signal.SIGTERM)
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_missing_pidfile_is_not_success(tmp_path):
    assert (
        remote_terminate_provider.main(["helper", str(tmp_path / "missing"), "0", "0", "0", "0.01"])
        == 1
    )


@pytest.mark.parametrize("value", ["0", "-1", "1", "not-a-pid", "9" * 100])
def test_invalid_pidfile_cannot_signal_a_group(tmp_path, value):
    pid_file = tmp_path / "invalid.pid"
    pid_file.write_text(value)
    assert remote_terminate_provider.main(["helper", str(pid_file), "0", "0", "0", "0.01"]) == 1


def test_pidfile_cannot_target_the_helpers_own_group(tmp_path):
    pid_file = tmp_path / "own-group.pid"
    pid_file.write_text(str(os.getpgrp()))
    assert remote_terminate_provider.main(["helper", str(pid_file), "0", "0", "0", "0.01"]) == 1


def test_symlink_pidfile_is_refused_without_stopping_provider(owned_group, tmp_path):
    process, pid_file = owned_group(ignore_term=False)
    symlink = tmp_path / "link.pid"
    symlink.symlink_to(pid_file)
    assert remote_terminate_provider.main(["helper", str(symlink), "0", "0", "0", "0.01"]) == 1
    assert process.poll() is None


def test_unconfirmed_signal_is_failure(owned_group, monkeypatch):
    process, pid_file = owned_group(ignore_term=True)
    sent = []
    real_killpg = os.killpg

    def no_delivery(pid, requested_signal):
        if requested_signal:
            sent.append(requested_signal)
        else:
            real_killpg(pid, requested_signal)

    with monkeypatch.context() as patch:
        patch.setattr(remote_terminate_provider.os, "killpg", no_delivery)
        assert not remote_terminate_provider.terminate_provider(
            str(pid_file), pid_file_timeout=0, term_timeout=0, kill_timeout=0, poll_interval=0.01
        )
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert process.poll() is None


@pytest.mark.parametrize(
    "durations", [["nan", "1", "1", "0.01"], ["0", "-1", "1", "0.01"], ["0", "1", "1", "0"]]
)
def test_invalid_durations_are_rejected(tmp_path, durations):
    assert remote_terminate_provider.main(["helper", str(tmp_path / "missing"), *durations]) == 2


def test_launcher_ships_helper_and_passes_runtime_limits_to_owned_local_group(
    owned_group, monkeypatch
):
    from rcp.agents import launcher

    process, pid_file = owned_group(ignore_term=False)
    shipped = []

    def local_transport(host, remote_command):
        assert host == "unused-test-host"
        command = shlex.split(remote_command)
        assert command[0] == "python3"
        assert command[1] == "-c"
        assert command[2] == Path(remote_terminate_provider.__file__).read_text()
        shipped.append(command)
        return [sys.executable, *command[1:]]

    monkeypatch.setattr(launcher, "ssh_arguments", local_transport)
    assert launcher.AgentProcessControl._terminate_remote("unused-test-host", str(pid_file))
    assert len(shipped) == 1
    assert process.wait(timeout=5) == -signal.SIGTERM
    # A repeated stop on the same owned pidfile also proves absence, not just
    # successful signal submission.
    assert remote_terminate_provider.terminate_provider(
        str(pid_file), pid_file_timeout=0, term_timeout=0, kill_timeout=0, poll_interval=0.01
    )
