"""Exercise the shipped shell executor without accessing machine power state."""

from __future__ import annotations

import os
import subprocess
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
                printf '    "AppleClamshellState" = %s\\n' "$(cat "$FAKE_STATE/lid")"''',
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


@pytest.mark.parametrize("lid", ["No", "Yes", "Nope", "timeout"])
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
