"""Exercise the shipped shell executor without accessing machine power state."""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.helpers import wait_until

SCRIPT = Path(__file__).parents[1] / "src/rcp/machine_power_watchdog.sh"
START = "Thu Oct  1 10:00:00 2026"


class Watchdog:
    def __init__(self, root: Path):
        self.root = root
        self.process: subprocess.Popen | None = None
        self.env = dict(os.environ, RCP_POWER_TEST="1", FAKE_STATE=str(root))
        self.write("flag", "0")
        self.write("lid", "No")
        self.write("start", START)
        for name, body in {
            "sudo": 'shift; exec "$@"',
            "pmset": """case "$*" in
                '-g') printf ' SleepDisabled %s\\n' "$(cat "$FAKE_STATE/flag")" ;;
                '-a disablesleep 1') echo on >> "$FAKE_STATE/calls"; echo 1 > "$FAKE_STATE/flag" ;;
                '-a disablesleep 0')
                    echo clear >> "$FAKE_STATE/calls"
                    [ ! -f "$FAKE_STATE/fail_clear" ] || exit 1
                    echo 0 > "$FAKE_STATE/flag" ;;
                sleepnow) echo sleep >> "$FAKE_STATE/calls"; [ ! -f "$FAKE_STATE/fail_sleep" ] ;;
                *) exit 2 ;;
            esac""",
            "ioreg": '''[ ! -f "$FAKE_STATE/fail_lid" ] || exit 1
                printf '  |   "AppleClamshellState" = %s\\n' "$(cat "$FAKE_STATE/lid")"''',
            "ps": '''[ ! -f "$FAKE_STATE/dead" ] || exit 1
                if [ -f "$FAKE_STATE/hang_ps" ]; then exec sleep 60; fi
                cat "$FAKE_STATE/start"''',
        }.items():
            executable = root / name
            executable.write_text(f"#!/bin/sh\n{body}\n")
            executable.chmod(0o755)
            self.env[f"RCP_POWER_{name.upper()}"] = str(executable)

    def write(self, name: str, content: str):
        temporary = self.root / f".{name}.test"
        temporary.write_text(content + "\n")
        temporary.replace(self.root / name)

    def heartbeat(self, *, desired="on", cause="disabled", at=None, pid=123):
        self.write(
            "heartbeat",
            f"generation=1\npid={pid}\nstart={START}\ndesired={desired}\ncause={cause}\n"
            f"at={int(time.time()) if at is None else at}",
        )

    def start(self):
        self.process = subprocess.Popen(
            ["/bin/sh", str(SCRIPT), str(self.root), "1", "0.05", "60", "1"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self.process

    def active(self):
        def activated():
            assert self.process is not None
            assert self.process.poll() is None, "watchdog exited before activation"
            return "on" in self.calls()

        wait_until(activated)

    def calls(self):
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def finish(self):
        assert self.process is not None
        return self.process.wait(timeout=5)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.heartbeat(desired="off", cause="shutdown")
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@pytest.fixture
def watchdog(tmp_path):
    watchdog = Watchdog(tmp_path)
    yield watchdog
    watchdog.close()


def test_acknowledges_before_activation_and_on_requires_fresh_heartbeat(watchdog):
    watchdog.start()
    wait_until(lambda: (watchdog.root / "ack").exists())
    assert watchdog.calls() == []
    watchdog.heartbeat()
    watchdog.active()
    assert "generation=1" in (watchdog.root / "activation").read_text()
    assert "set=1" in (watchdog.root / "activation").read_text()
    watchdog.heartbeat(desired="off", cause="demand_gone")
    assert watchdog.finish() == 0
    assert watchdog.calls() == ["on", "clear"]
    assert "set=0" in (watchdog.root / "activation").read_text()


@pytest.mark.parametrize(
    "failure", ["stale", "malformed", "dead", "start_changed", "ps_timeout", "pid_changed"]
)
def test_invalid_heartbeat_revokes_and_cannot_rearm(watchdog, failure):
    watchdog.heartbeat()
    watchdog.start()
    watchdog.active()
    if failure == "stale":
        watchdog.heartbeat(at=int(time.time()) - 61)
    elif failure == "malformed":
        watchdog.heartbeat(at="1:2")
    elif failure == "start_changed":
        watchdog.write("start", "a different process")
    elif failure == "pid_changed":
        # The fake ps only recognizes the backend PID for this case.
        path = watchdog.root / "ps"
        path.write_text(f'#!/bin/sh\n[ "$2" = 123 ] || exit 1\nprintf "%s\\n" "{START}"\n')
        watchdog.heartbeat(pid=456)
    else:
        watchdog.write("hang_ps" if failure == "ps_timeout" else "dead", "1")
    assert watchdog.finish() == 0
    assert watchdog.calls() == ["on", "clear"]
    assert (watchdog.root / "revoked").read_text() == "generation=1\n"
    assert "cause=heartbeat_stale" in (watchdog.root / "result").read_text()
    watchdog.heartbeat()
    watchdog.start()
    assert watchdog.finish() == 0
    assert watchdog.calls() == ["on", "clear"]


# The last case is two ioreg rows that disagree; only a unanimous No is open.
@pytest.mark.parametrize(
    "lid", ["No", "Yes", "Nope", "timeout", 'No\n  |   "AppleClamshellState" = Yes']
)
def test_release_sleeps_only_closed_or_unknown_lid(watchdog, lid):
    watchdog.write("lid", lid)
    if lid == "timeout":
        path = watchdog.root / "ioreg"
        path.write_text("#!/bin/sh\nexec sleep 60\n")
    watchdog.heartbeat()
    watchdog.start()
    watchdog.active()
    watchdog.heartbeat(desired="off", cause="thermal")
    assert watchdog.finish() == 0
    assert watchdog.calls() == (["on", "clear"] if lid == "No" else ["on", "clear", "sleep"])
    assert "cause=thermal" in (watchdog.root / "result").read_text()


def test_cleanup_records_clear_and_sleep_failures_separately(watchdog):
    watchdog.write("lid", "Yes")
    watchdog.write("fail_clear", "1")
    watchdog.write("fail_sleep", "1")
    watchdog.heartbeat()
    watchdog.start()
    watchdog.active()
    watchdog.heartbeat(desired="off", cause="shutdown")
    assert watchdog.finish() == 0
    result = (watchdog.root / "result").read_text()
    assert "clear_failed=1" in result
    assert "sleep_failed=1" in result
    assert "set=1" in (watchdog.root / "activation").read_text()


def test_unknown_flag_reading_releases_instead_of_reasserting(watchdog):
    watchdog.heartbeat()
    watchdog.start()
    watchdog.active()
    watchdog.write("flag", "unknown")
    assert watchdog.finish() == 1
    assert watchdog.calls() == ["on", "clear"]
    assert "cause=reading_failed" in (watchdog.root / "result").read_text()


@pytest.mark.parametrize("fresh_heartbeat", [False, True])
def test_never_clears_or_adopts_unowned_flag(watchdog, fresh_heartbeat):
    watchdog.write("flag", "1")
    if fresh_heartbeat:
        watchdog.heartbeat()
    watchdog.start()
    assert watchdog.finish() == (1 if fresh_heartbeat else 0)
    assert watchdog.calls() == []
    assert (watchdog.root / "flag").read_text() == "1\n"


def test_watchdog_keeps_machine_lock_after_backend_death_until_cleanup(watchdog):
    root = watchdog.root
    gate = root / "allow_clear"
    os.mkfifo(gate)
    gate_fd = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
    pmset = root / "pmset"
    pmset.write_text(
        pmset.read_text().replace(
            'echo clear >> "$FAKE_STATE/calls"',
            'echo clear >> "$FAKE_STATE/calls"\nread release < "$FAKE_STATE/allow_clear"',
        )
    )
    # The sandbox denies /bin/ps; probe the real PID with the shell builtin.
    (root / "ps").write_text(
        '#!/bin/sh\nkill -0 "$2" 2>/dev/null || exit 1\ncat "$FAKE_STATE/start"\n'
    )
    backend = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import fcntl
import os
import signal
import sys
import time
from pathlib import Path
from rcp.machine_power import spawn_command

root = Path(sys.argv[1])
fd = os.open(root / 'owner.lock', os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
pid = os.getpid()
start = (root / 'start').read_text().strip()
(root / 'heartbeat').write_text(
    f'generation=1\\npid={pid}\\nstart={start}\\ndesired=on\\nat={int(time.time())}\\n'
)
spawn_command(['/bin/sh', sys.argv[2], str(root), '1', '0.05', '60', '30'], pass_fds=(fd,))
signal.pause()
""",
            str(root),
            str(SCRIPT),
        ],
        env=watchdog.env,
    )
    contender_fd = None
    watchdog_pid = None
    released = False
    try:
        # A loaded CI runner can take several seconds to reach the first "on".
        wait_until(lambda: (root / "ack").exists(), timeout=30)
        ack = dict(line.split("=", 1) for line in (root / "ack").read_text().splitlines())
        watchdog_pid = int(ack["watchdog_pid"])
        wait_until(lambda: "on" in watchdog.calls(), timeout=30)
        contender_fd = os.open(root / "owner.lock", os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backend.kill()
        assert backend.wait(timeout=5) == -signal.SIGKILL
        wait_until(lambda: "clear" in watchdog.calls())
        assert (root / "flag").read_text().strip() == "1"
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(gate_fd, b"release\n")

        def acquire_after_cleanup():
            try:
                fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

        wait_until(acquire_after_cleanup)
        released = True
        assert (root / "flag").read_text().strip() == "0"
        result = (root / "result").read_text()
        assert "cause=heartbeat_stale" in result
        assert "complete=1" in result
        assert "clear_failed=0" in result
    finally:
        if backend.poll() is None:
            backend.kill()
            backend.wait(timeout=5)
        os.write(gate_fd, b"release\n")
        os.close(gate_fd)
        if watchdog_pid is not None and not released:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(watchdog_pid, signal.SIGKILL)
        if contender_fd is not None:
            os.close(contender_fd)
