from __future__ import annotations

import errno
import inspect
import json
import os
import shlex
import subprocess
import sys

import pytest

from rcp import git_identity
from rcp.terminals import git_access, launch, profile, remote
from rcp.terminals.models import TerminalUnavailable
from rcp.transport import remote_terminal


def settings(tmp_path, containment="cooperative"):
    return {
        "repository": str(tmp_path),
        "protected_paths": [str(tmp_path / ".research")],
        "unit": "rcp-terminal-test",
        "containment": containment,
        "stop_timeout": 1,
        "profile_source": inspect.getsource(profile),
        "git_access_source": inspect.getsource(git_access),
    }


def test_remote_launch_uses_strict_unshared_ssh_pty_and_shipped_sources(tmp_path, monkeypatch):
    captured = {}

    def fake_launch(command, unit, **kwargs):
        captured.update(command=command, unit=unit, kwargs=kwargs)
        return "process", 5

    monkeypatch.setattr(launch, "launch", fake_launch)
    assert remote.start_remote(
        "member@execution",
        unit="unit",
        repository=tmp_path,
        protected_paths=["/remote/.research"],
        containment="mirrored",
    ) == ("process", 5)
    command = captured["command"]
    assert command[:2] == ["ssh", "-tt"]
    assert "StrictHostKeyChecking=yes" in command
    assert command[command.index("-S") + 1] == "none"
    assert captured["unit"] is None
    shipped = shlex.split(command[-1])
    assert shipped[:3] == ["exec", "python3", "-c"]
    assert shipped[3] == inspect.getsource(remote_terminal)
    request = json.loads(shipped[4])
    assert request["profile_source"] == inspect.getsource(profile)
    assert request["git_access_source"] == inspect.getsource(git_access)
    assert request["protected_paths"] == ["/remote/.research"]


def test_the_shipped_stop_request_also_refuses_a_wrong_account(tmp_path):
    """Stopping under the wrong account would find no unit, call that success,
    and finish a record whose unit is still running on the right one.
    """
    payload = {
        "action": "stop",
        "unit": "rcp-terminal-test",
        "os_account": "an-account-this-machine-is-not",
        "stop_timeout": 1,
    }
    result = subprocess.run(
        [sys.executable, "-c", inspect.getsource(remote_terminal), json.dumps(payload)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=10,
    )
    assert result.returncode == 1
    assert "different account" in result.stderr


def test_the_shipped_wrapper_refuses_a_connection_on_the_wrong_account(tmp_path):
    """The capability probe answered on an earlier connection and its result is
    cached for the manager's lifetime. SSH configuration can change in between,
    so the launch verifies the account inside its own connection.
    """
    payload = {**settings(tmp_path), "os_account": "an-account-this-machine-is-not"}
    result = subprocess.run(
        [sys.executable, "-c", inspect.getsource(remote_terminal), json.dumps(payload)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=10,
    )
    assert result.returncode == 1
    assert "different account" in result.stderr
    assert profile._READY_MARKER.decode() not in result.stdout


def test_shipped_cooperative_shell_emits_completion_even_for_exit_255(tmp_path):
    # Execute the exact shipped module in a subprocess with a disposable HOME;
    # no SSH process, key, config or remote host participates in this test.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            inspect.getsource(remote_terminal),
            json.dumps(
                {
                    **settings(tmp_path),
                    "git_identity": {"user_id": "member", "display_name": "Member Name"},
                    "git_identity_source": inspect.getsource(git_identity),
                }
            ),
        ],
        input=b"git config user.name\ngit config user.email\nprintf 'remote-shell-output\\n'\nexit 255\n",
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=10,
    )
    assert result.returncode == 255
    assert profile._READY_MARKER in result.stdout
    assert b"Member Name" in result.stdout
    assert b"member@members.rcp.invalid" in result.stdout
    assert b"remote-shell-output" in result.stdout
    assert result.stdout.endswith(
        remote_terminal.EXIT_PREFIX + b"255" + remote_terminal.EXIT_SUFFIX
    )


@pytest.mark.parametrize("boundary", range(len(remote_terminal.EXIT_PREFIX) + 5))
def test_completion_parser_removes_every_split_marker(boundary):
    parser = remote.CompletionParser()
    marker = remote_terminal.EXIT_PREFIX + b"255" + remote_terminal.EXIT_SUFFIX
    visible = parser.feed(b"prompt" + marker[:boundary])
    visible += parser.feed(marker[boundary:] + b"bye")
    visible += parser.finish()
    assert visible == b"promptbye"
    assert parser.exit_code == 255


def test_completion_parser_preserves_incomplete_and_invalid_sequences():
    parser = remote.CompletionParser()
    data = b"hello" + remote_terminal.EXIT_PREFIX + b"999" + remote_terminal.EXIT_SUFFIX
    assert parser.feed(data) == data
    assert parser.exit_code is None
    partial = remote_terminal.EXIT_PREFIX[:5]
    assert parser.feed(partial) == b""
    assert parser.finish() == partial


def test_remote_mirrored_failure_never_launches_cooperative_shell(tmp_path, monkeypatch):
    commands = []
    stopped = []

    def fail_launch(command, **kwargs):
        commands.append(command)
        raise OSError("systemd mount setup failed")

    monkeypatch.setattr(remote_terminal.subprocess, "Popen", fail_launch)
    monkeypatch.setattr(remote_terminal, "stop_unit", lambda unit, timeout: stopped.append(unit))
    monkeypatch.setattr(remote_terminal.signal, "signal", lambda *args: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(OSError, match="mount setup failed"):
        remote_terminal.run_session(settings(tmp_path, "mirrored"))
    assert len(commands) == 1
    assert commands[0][0] == "systemd-run"
    assert stopped == ["rcp-terminal-test"]


def test_shipped_profile_uses_remote_existing_paths_and_remote_home(tmp_path, monkeypatch):
    home = tmp_path / "remote-home"
    home.mkdir()
    (home / ".ssh").mkdir()
    monkeypatch.setenv("HOME", str(home))
    commands = []

    class Child:
        def wait(self, **kwargs):
            return 0

        def poll(self):
            return 0

    def capture(command, **kwargs):
        commands.append(command)
        return Child()

    monkeypatch.setattr(remote_terminal.subprocess, "Popen", capture)
    monkeypatch.setattr(remote_terminal, "stop_unit", lambda *args: None)
    monkeypatch.setattr(remote_terminal.signal, "signal", lambda *args: None)
    monkeypatch.setattr(remote_terminal.os, "write", lambda *args: None)
    remote_terminal.run_session(settings(tmp_path, "mirrored"))
    command = commands[0]
    assert f"HOME={home}" in command
    assert f'BindReadOnlyPaths="{home / ".ssh"}"' in command
    absent = str(tmp_path / ".research")
    assert any(
        arg.startswith("BindReadOnlyPaths=") and arg.endswith(f':"{absent}"') for arg in command
    )
    assert profile._SHELL_PREFLIGHT in command


def test_remote_refuses_canonical_repository_after_far_side_resolution(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    request = settings(tmp_path)
    request.update(repository=str(alias), protected_paths=[str(canonical)])
    monkeypatch.setattr(
        remote_terminal.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("launched")
    )
    with pytest.raises(ValueError, match="Canonical state"):
        remote_terminal.run_session(request)


def test_remote_cleanup_runs_on_hangup(tmp_path, monkeypatch):
    stopped = []

    class Child:
        def wait(self, **kwargs):
            raise InterruptedError("SSH channel hung up")

        def poll(self):
            return 0

    monkeypatch.setattr(remote_terminal.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(remote_terminal, "stop_unit", lambda unit, timeout: stopped.append(unit))
    monkeypatch.setattr(remote_terminal.signal, "signal", lambda *args: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(InterruptedError):
        remote_terminal.run_session(settings(tmp_path, "mirrored"))
    assert stopped == ["rcp-terminal-test"]


def test_remote_stop_surfaces_unreachable_cleanup(tmp_path, monkeypatch):
    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 255, "", "Connection timed out")

    monkeypatch.setattr(remote.subprocess, "run", fail)
    with pytest.raises(TerminalUnavailable):
        remote.stop_remote_unit("member@execution", "unit")


def test_completed_shell_evidence_survives_remote_cleanup_failure(tmp_path, monkeypatch):
    output = []

    class Child:
        def wait(self, **kwargs):
            return 255

        def poll(self):
            return 255

    def fail_stop(*args):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(remote_terminal.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(remote_terminal, "stop_unit", fail_stop)
    monkeypatch.setattr(remote_terminal.signal, "signal", lambda *args: None)
    monkeypatch.setattr(remote_terminal.os, "write", lambda fd, data: output.append(data))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        remote_terminal.run_session(settings(tmp_path, "mirrored"))
    assert output == [remote_terminal.EXIT_PREFIX + b"255" + remote_terminal.EXIT_SUFFIX]


def test_remote_profile_preserves_lexical_and_resolved_denies(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / ".research"
    alias.symlink_to(actual, target_is_directory=True)
    commands = []

    class Child:
        def wait(self, **kwargs):
            return 0

        def poll(self):
            return 0

    def capture(command, **kwargs):
        commands.append(command)
        return Child()

    monkeypatch.setattr(remote_terminal.subprocess, "Popen", capture)
    monkeypatch.setattr(remote_terminal, "stop_unit", lambda *args: None)
    monkeypatch.setattr(remote_terminal.signal, "signal", lambda *args: None)
    monkeypatch.setattr(remote_terminal.os, "write", lambda *args: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    remote_terminal.run_session(settings(tmp_path, "mirrored"))
    assert f'ReadOnlyPaths="{alias}"' in commands[0]
    assert f'ReadOnlyPaths="{actual}"' in commands[0]


def test_shipped_wrapper_has_job_control_and_hangs_up_with_local_pty(tmp_path):
    import fcntl
    import pty
    import select
    import termios
    import time

    def own_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", inspect.getsource(remote_terminal), json.dumps(settings(tmp_path))],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "HOME": str(tmp_path)},
        preexec_fn=own_terminal,
    )
    os.close(slave)
    output = bytearray()
    # Bash's default prompt ends in "# " for root and "$ " otherwise.
    PROMPTS = (b"$ ", b"# ")

    def read_until(marker, seconds=10, start=0):
        markers = marker if isinstance(marker, tuple) else (marker,)
        deadline = time.monotonic() + seconds
        while not any(each in output[start:] for each in markers):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                return False
            try:
                chunk = os.read(master, 4096)
            except OSError as error:
                # EIO: the session hung up. Report it through the caller's
                # assertion, which prints everything the shell wrote.
                if error.errno != errno.EIO:
                    raise
                return False
            output.extend(chunk)
        return True

    def require(marker, seconds=10):
        assert read_until(marker, seconds), output.decode(errors="replace")

    def interrupt():
        """Send INTR and wait until it is handled before anything else is typed.

        The tty flushes its input queue while processing INTR, so a command
        written before then loses its leading characters — which reads as
        `bash: rintf: command not found` rather than as a lost interrupt. The
        tty echoes `^C` while a job runs; at the prompt, bash 3.2's readline
        prints only a fresh prompt.
        """
        mark = len(output)
        os.write(master, b"\x03")
        assert read_until((b"^C", *PROMPTS), 5, mark), output.decode(errors="replace")

    try:
        require(profile._READY_MARKER)
        # The marker is printed just before bash starts. Type nothing until bash
        # prompts: CI once lost the whole session to an interrupt sent in that gap.
        assert read_until(PROMPTS, 10, output.index(profile._READY_MARKER)), output.decode(
            errors="replace"
        )
        os.write(master, b"sleep 30\n")
        require(b"sleep 30\r\n")
        interrupt()
        # An interrupt delivered while the shell is still forking its job can
        # arrive before that job exists, leaving it running and swallowing what
        # follows. Ask again, interrupting once more, until the shell answers.
        deadline = time.monotonic() + 30
        while True:
            mark = len(output)
            os.write(master, b"printf 'job-%s\\n' control\n")
            if read_until(b"job-control\r\n", 3, mark):
                break
            assert time.monotonic() < deadline, output.decode(errors="replace")
            interrupt()
        assert b"no job control" not in output
        os.close(master)
        master = -1
        assert process.wait(timeout=10) != 0
    finally:
        if master >= 0:
            os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_remote_source_shipping_does_not_require_inspectable_module_files(tmp_path, monkeypatch):
    def no_inspection(*args):
        raise AssertionError("Source modules are not inspectable in a frozen backend")

    monkeypatch.setattr(inspect, "getsource", no_inspection)
    monkeypatch.setattr(remote_terminal, "__file__", "remote_terminal.pyc")
    monkeypatch.setattr(profile, "__file__", "profile.pyc")
    monkeypatch.setattr(git_access, "__file__", "git_access.pyc")
    captured = []
    monkeypatch.setattr(launch, "launch", lambda command, *args, **kwargs: captured.append(command))
    remote.start_remote(
        "member@execution",
        unit="unit",
        repository=tmp_path,
        protected_paths=[],
        containment="mirrored",
    )
    shipped = shlex.split(captured[0][-1])
    assert "def run_session" in shipped[3]
    payload = json.loads(shipped[4])
    assert "def launch_command" in payload["profile_source"]
    assert "def terminal_git_access" in payload["git_access_source"]


def test_remote_launch_carries_the_deploy_key_path_to_the_far_side(monkeypatch):
    """The key path must survive the whole route-to-launcher chain.

    A signature that accepts it while the call site drops it looks plumbed and
    authenticates nothing, which is how this first shipped.
    """
    import pathlib

    from rcp.terminals import remote

    captured = {}

    def fake_launch(command, unit, **kwargs):
        captured["command"] = command
        return (None, 0)

    monkeypatch.setattr(remote.launch, "launch", fake_launch)
    remote.start_remote(
        "example.invalid",
        unit="u1",
        repository=pathlib.Path("/srv/code"),
        protected_paths=[],
        containment="mirrored",
        git_key_relative=".local/share/rcp/credentials/projects/p1/code/id_ed25519",
    )
    blob = " ".join(captured["command"])
    assert "projects/p1/code/id_ed25519" in blob
