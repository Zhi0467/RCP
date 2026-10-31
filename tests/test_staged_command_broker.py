from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from rcp import limits
from rcp.agents import staged_command_broker as broker


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
    # No sockets or staged credentials: exercise the actual standalone wait with
    # a signal deliberately delivered to another thread, leaving stdin open.
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
