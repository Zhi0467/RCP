from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from rcp import limits
from rcp.agents import staged_command_broker as broker
from rcp.agents.command_mailbox import stage_command_mailbox


def test_standalone_wait_reads_only_ready_input_until_eof(monkeypatch) -> None:
    descriptor = 123
    polls = iter([False, True, False, True])
    chunks = iter([b"ignored input", b""])
    reads = []

    def select(readers, writers, errors, timeout):
        assert readers == [descriptor]
        assert writers == errors == []
        assert timeout == limits.COMMAND_BROKER_STDIN_POLL_SECONDS
        return ([descriptor] if next(polls) else [], [], [])

    def read(fd, size):
        assert fd == descriptor and size > 0
        reads.append(fd)
        return next(chunks)

    monkeypatch.setattr(broker.sys, "stdin", SimpleNamespace(fileno=lambda: descriptor))
    monkeypatch.setattr(broker.select, "select", select)
    monkeypatch.setattr(broker.os, "read", read)
    broker._wait_for_stdin_close()
    assert reads == [descriptor, descriptor]


def test_standalone_wait_handles_signal_received_by_worker_with_stdin_open() -> None:
    """The signal goes deliberately to another thread with stdin left open."""
    script = """
import runpy
import signal
import sys
import threading

broker = runpy.run_path(sys.argv[1])
signal.signal(signal.SIGTERM, broker['_exit_on_signal'])
waiting = threading.Event()
select = broker['select'].select

def begin_wait(*args):
    waiting.set()
    return select(*args)

def signal_worker():
    waiting.wait()
    signal.pthread_kill(threading.get_ident(), signal.SIGTERM)

broker['select'].select = begin_wait
threading.Thread(target=signal_worker, daemon=True).start()
broker['_wait_for_stdin_close']()
raise AssertionError('stdin must remain open until the signal exits the broker')
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", script, str(Path(broker.__file__))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.wait(timeout=5) == 128 + signal.SIGTERM
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def test_broker_exits_cleanly_while_the_backend_keeps_stdin_open(tmp_path: Path) -> None:
    """Regression: interpreter finalization used to abort while the stdin relay held
    stdin's buffer, once per agent turn.
    """
    staged = stage_command_mailbox(
        local_stage=tmp_path, remote_stage=None, episode_id="e", task_id="t", turn_id="turn"
    )
    gate = staged.invocation_gate
    assert gate is not None
    process = subprocess.Popen(
        gate.wrap_command([sys.executable, "-c", "print('answer')"]),
        cwd=gate.workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(gate.bootstrap(b""))
        process.stdin.flush()
        assert process.stdout.read().splitlines() == [gate.ready_line.encode(), b"answer"]
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        staged.cleanup()
