"""Machine-local demand and safety policy; only the detached watchdog changes pmset."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from rcp.agents.provider_accounts import account_login_refusal
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.config import load_manifest
from rcp.episode_health import load_episode_health
from rcp.limits import (
    MACHINE_POWER_BATTERY_FLOOR,
    MACHINE_POWER_COMMAND_TIMEOUT_SECONDS,
    MACHINE_POWER_HEARTBEAT_STALE_SECONDS,
    MACHINE_POWER_PASS_SECONDS,
    MACHINE_POWER_WATCHDOG_INTERVAL_SECONDS,
)
from rcp.machine_power_install import InstallError
from rcp.runs.provider_login import provider_login_host

logger = logging.getLogger(__name__)
PMSET = "/usr/bin/pmset"
IOREG = "/usr/sbin/ioreg"


def run_command(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, timeout=timeout, capture_output=True, text=True, check=False)


def spawn_command(argv: list[str], *, pass_fds: tuple[int, ...] = ()) -> subprocess.Popen:
    # The watchdog inherits the advisory owner lock. A dead backend cannot free
    # ownership before its surviving watchdog has finished releasing the flag.
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=pass_fds,
    )


def parse_battery(output: str) -> tuple[bool, int | None]:
    match = re.search(r"Now drawing from '(AC Power|Battery Power)'", output)
    if match is None:
        raise ValueError("unknown battery source")
    ac = match[1] == "AC Power"
    percentages = re.findall(r"\b(\d+)%;", output)
    if not percentages:
        if ac and "InternalBattery" not in output:
            return True, None  # desktop Mac, no battery
        raise ValueError("unknown battery percentage")
    level = min(map(int, percentages))
    if not 0 <= level <= 100:
        raise ValueError("invalid battery percentage")
    return ac, level


def parse_thermal(output: str) -> bool:
    """True is a warning; unrecognized output never means healthy."""
    if re.search(
        r"(?:Thermal|Performance)\s+(?:Warning(?:\s+Level)?|Level)\s*[:=]\s*[1-9]", output, re.I
    ):
        return True
    limits = re.findall(r"CPU_(?:Speed_Limit|Scheduler_Limit|Available_CPUs)\s*=\s*(\d+)", output)
    speed = re.findall(r"CPU_(?:Speed_Limit|Scheduler_Limit)\s*=\s*(\d+)", output)
    if any(int(value) < 100 for value in speed):
        return True
    thermal_ok = "No thermal warning level has been recorded" in output
    performance_ok = "No performance warning level has been recorded" in output
    if thermal_ok and performance_ok:
        return False
    if limits or re.search(
        r"(?:Thermal|Performance)\s+(?:Warning|Level)\s*[:=]\s*0\b", output, re.I
    ):
        # A report that contains a warning history but no affirmative all-clear
        # is a warning even if CPU speed has since recovered.
        return True
    raise ValueError("unknown thermal output")


def parse_lid(output: str) -> bool:
    values = re.findall(r'"AppleClamshellState"\s*=\s*(Yes|No)\s*$', output, re.M)
    if not values or len(set(values)) != 1:
        raise ValueError("unknown lid state")
    return values[0] == "Yes"


def parse_flag(output: str) -> bool:
    match = re.search(r"^\s*(?:SleepDisabled|disablesleep)\s+([01])\s*$", output, re.M)
    if match is not None:
        return match[1] == "1"
    # pmset omits the line until the flag has been set once since boot.
    if "System-wide power settings:" in output:
        return False
    raise ValueError("unknown SleepDisabled flag")


def demand_snapshot(store, background) -> list[str]:
    """Coarse, local reads only; armed watchers alone never create demand."""
    reasons = []
    projects = store.projects()
    episodes = [
        episode
        for project in projects
        if project.home_space_id == store.space_id
        for episode in store.episodes(project.project_id, limit=None)
        if episode.status not in {"completed", "stopped", "failed"}
    ]
    health = load_episode_health(store, episodes)
    if any(
        value[0] in {"starting", "active", "recovering", "stopping", "wrapping_up"}
        and not (value[0] == "wrapping_up" and value[3] == "sign_in")
        for value in health.values()
    ):
        reasons.append("episode")
    credentials = ProviderCredentialStore.for_data_dir(store.path.parent)
    for project in projects if store.has_any_active_agent_task() else ():
        for task in store.all_project_agent_tasks(project.project_id):
            if task.status in {"running", "pausing"}:
                reasons.append("task")
                break
            if task.status == "queued":
                provider = task.request.get("provider")
                host = task.stage_host
                if host is None:
                    run_on = task.request.get("run_on")
                    host = (
                        provider_login_host(load_manifest(project.locator), run_on)
                        if run_on not in {None, "local"}
                        else ""
                    )
                if (
                    not provider
                    or account_login_refusal(store, credentials, provider, host) is None
                ):
                    reasons.append("task")
                    break
        if "task" in reasons:
            break
    if not background.runtime_is_idle():
        reasons.append("runtime")
    if background.has_pending_transport_retry():
        reasons.append("retry")
    return reasons


def read_record(path: Path) -> dict[str, str]:
    try:
        return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    except FileNotFoundError:
        return {}


def write_record(path: Path, values: dict) -> None:
    """Atomic exchange; no shell ever sources these records."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            for key, value in values.items():
                if "\n" in str(value):
                    raise ValueError("record value contains a newline")
                stream.write(f"{key}={value}\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class MachinePowerController:
    """One safety thread, a worker-bound idle hold, and a revocable lid executor."""

    def __init__(
        self,
        store,
        *,
        demand_reader: Callable[[], list[str]],
        run=run_command,
        spawn=None,
        clock=time.time,
        monotonic=time.monotonic,
        process_identity: Callable[[], tuple[int, str]] | None = None,
        directory: Path | None = None,
        installer=None,
        platform: str = sys.platform,
    ):
        from rcp.machine_power_install import MachinePowerInstaller

        self.store, self.demand_reader = store, demand_reader
        self.run, self.clock, self.monotonic = run, clock, monotonic
        self.spawn = spawn or self._spawn
        self.platform = (
            "macos"
            if platform == "darwin"
            else "linux"
            if platform.startswith("linux")
            else "other"
        )
        self.installer = installer or MachinePowerInstaller(run=run)
        self.directory = directory or self.installer.paths.directory
        self.identity_reader = process_identity or self._process_identity
        self._identity = None
        self._lock = threading.RLock()
        self._admin_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._owner = None
        self._watchdog = None
        self._idle = None
        self._generation = 0
        self._desired = "off"
        self._reasons = []
        self._external = False
        self._lid_active = False
        self._running = False
        self._release_cause = None
        with store.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS machine_power_state "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM machine_power_state WHERE singleton=1"
            ).fetchone()
        self._state = {
            "idle_hold": True,
            "lid_mode": False,
            "latched": None,
            "last_release": None,
            "cleanup_failure": None,
            "battery_blocked": False,
            "result_generation": None,
        }
        if row:
            self._state.update(json.loads(row[0]))

    def _spawn(self, argv):
        # Only the watchdog inherits the machine lock. An idle hold or unrelated
        # child must not keep another data directory from acquiring ownership.
        fds = (self._owner,) if argv[0] == "/bin/sh" and self._owner is not None else ()
        return spawn_command(argv, pass_fds=fds)

    def _process_identity(self) -> tuple[int, str]:
        pid = os.getpid()
        start = self._read(["/bin/ps", "-p", str(pid), "-o", "lstart="]).strip()
        if not start:
            raise ValueError("missing process start time")
        return pid, start

    def _read(self, argv):
        result = self.run(argv, MACHINE_POWER_COMMAND_TIMEOUT_SECONDS)
        if result.returncode:
            raise OSError(f"command failed: {argv[0]}")
        return result.stdout

    def _save(self):
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO machine_power_state VALUES (1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET value=excluded.value",
                (json.dumps(self._state),),
            )

    def _save_best_effort(self):
        try:
            self._save()
        except Exception:
            logger.exception("Could not save machine power release state")

    def status(self) -> dict:
        with self._lock:
            supported = self.platform == "macos"
            installed = self.installer.status() if supported else None
            return {
                "platform": self.platform,
                "supported": supported,
                "installed": installed.installed if installed else False,
                "install_problem": installed.install_problem if installed else "not_installed",
                "idle_hold": {
                    "enabled": self._state["idle_hold"],
                    "active": self._idle is not None and self._idle.poll() is None,
                },
                "lid_mode": {"enabled": self._state["lid_mode"], "active": self._lid_active},
                "demand": bool(self._reasons),
                "demand_reasons": list(self._reasons),
                "latched": self._state["latched"],
                "last_release": self._state["last_release"],
                "cleanup_failure": self._state["cleanup_failure"],
                "external_owner": self._external,
            }

    def update(self, preferences: dict) -> dict:
        with self._lock:
            if set(preferences) - {"idle_hold", "lid_mode"} or any(
                type(value) is not bool for value in preferences.values()
            ):
                raise ValueError("invalid machine power preferences")
            self._state.update(preferences)
            if preferences.get("lid_mode"):
                self._state["latched"] = None
                self._state["cleanup_failure"] = None
            self._save()
            # A fenced replacement may serve settings, but cannot start any
            # runtime owner until ordinary deferred startup has completed.
            if self._running:
                self.safety_pass()
            return self.status()

    def install(self):
        with self._admin_lock:
            with self._lock:
                before = self.status()
                if self.platform != "macos" or before["installed"]:
                    return before
            # A prompt can outlive heartbeat staleness. Safety passes continue
            # while the human decides; the admin action owns its machine lock.
            try:
                self.installer.install()
            except InstallError as exc:
                if exc.code == "clear_failed":
                    with self._lock:
                        self._cleanup_failed("clear_failed")
                raise
            with self._lock:
                if self.installer.cancelled:
                    return before
                if self._running:
                    self.safety_pass()
                return self.status()

    def uninstall(self):
        with self._admin_lock:
            with self._lock:
                before = self.status()
                if self.platform != "macos":
                    return before
            quiesced = False

            def quiesce():
                nonlocal quiesced
                self._lock.acquire()
                quiesced = True
                self._release("disabled")
                if not self._wait_watchdog():
                    raise InstallError("owner_busy")

            try:
                # Acceptance precedes quiescence. Once accepted, retain the
                # controller fence through removal so no pass can re-arm it.
                self.installer.uninstall(before_remove=quiesce)
                with self._lock:
                    if self.installer.cancelled:
                        return before
                    self._state["lid_mode"] = False
                    self._save_best_effort()
                    return self.status()
            except InstallError as exc:
                if exc.code == "clear_failed":
                    with self._lock:
                        self._cleanup_failed("clear_failed")
                raise
            finally:
                if quiesced:
                    self._lock.release()

    def start(self):
        with self._lock:
            if self.platform != "macos" or self._running:
                return
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="rcp-machine-power", daemon=True
            )
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            self.safety_pass()
            self._stop.wait(MACHINE_POWER_PASS_SECONDS)

    def stop(self):
        self._stop.set()
        with self._lock:
            if (
                self._running
                or self._idle is not None
                or self._watchdog is not None
                or (
                    self.platform == "macos"
                    and read_record(self.directory / "activation").get("set") == "1"
                )
            ):
                self._running = False
                try:
                    self._release("shutdown")
                except Exception:
                    logger.exception(
                        "Could not publish machine power shutdown; watchdog will expire"
                    )
        if self._thread:
            self._thread.join(MACHINE_POWER_COMMAND_TIMEOUT_SECONDS * 5)
        with self._lock:
            try:
                self._wait_watchdog()
            except Exception:
                logger.exception("Machine power shutdown cleanup failed")

    def _close_owner(self):
        if self._owner is not None:
            os.close(self._owner)
            self._owner = None

    def _wait_watchdog(self, *, recover: bool = True) -> bool:
        if self._watchdog:
            try:
                self._watchdog.wait(
                    timeout=MACHINE_POWER_COMMAND_TIMEOUT_SECONDS * 5
                    + MACHINE_POWER_WATCHDOG_INTERVAL_SECONDS
                )
            except subprocess.TimeoutExpired:
                # Keep the cleanup executor alive, with its inherited lock.
                return False
            complete = self._consume_result()
            self._watchdog = None
            # It died after `off` was published but before clearing: recover
            # the owned flag before ownership is dropped.
            owned = read_record(self.directory / "activation").get("set") == "1"
            if recover and not complete and owned:
                self._start_watchdog(recover=True)
                self._heartbeat("off", "shutdown")
                return self._wait_watchdog(recover=False)
        self._close_owner()
        return True

    def _heartbeat(self, desired, cause=""):
        pid, start = self._identity
        write_record(
            self.directory / "heartbeat",
            {
                "generation": self._generation,
                "pid": pid,
                "start": start,
                "desired": desired,
                "cause": cause,
                "at": int(self.clock()),
            },
        )
        self._desired = desired

    def _acquire(self) -> bool:
        if self._owner is not None:
            return True
        fd = os.open(self.directory / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            self._external = True
            return False
        self._owner = fd
        self._external = False
        # The old executor has released ownership; consume its durable outcome
        # before allocating a generation or deciding whether re-arming is safe.
        records = [
            read_record(self.directory / name) for name in ("activation", "result", "revoked")
        ]
        previous_generation = self._generation
        if not records[1]:
            # Uninstall removes every record, so a reinstall restarts numbering.
            self._state["result_generation"] = None
        self._generation = int(records[1].get("generation", 0))
        self._consume_result()
        self._generation = max(
            previous_generation, *(int(record.get("generation", 0)) for record in records)
        )
        return True

    def _start_watchdog(self, *, recover=False):
        old = read_record(self.directory / "activation")
        revoked = read_record(self.directory / "revoked")
        self._generation = (
            max(self._generation, int(old.get("generation", 0)), int(revoked.get("generation", 0)))
            + 1
        )
        self._identity = self.identity_reader()
        source = Path(__file__).with_name("machine_power_watchdog.sh")
        script = self.directory / source.name
        fd, temporary = tempfile.mkstemp(prefix=".watchdog.", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(source.read_bytes())
            os.replace(temporary, script)
        finally:
            Path(temporary).unlink(missing_ok=True)
        (self.directory / "heartbeat").unlink(missing_ok=True)
        (self.directory / "ack").unlink(missing_ok=True)
        if recover:
            write_record(
                self.directory / "activation",
                {
                    "generation": self._generation,
                    "pid": self._identity[0],
                    "start": self._identity[1],
                    "set": 1,
                },
            )
        self._watchdog = self.spawn(
            [
                "/bin/sh",
                str(script),
                str(self.directory),
                str(self._generation),
                str(MACHINE_POWER_WATCHDOG_INTERVAL_SECONDS),
                str(MACHINE_POWER_HEARTBEAT_STALE_SECONDS),
                str(MACHINE_POWER_COMMAND_TIMEOUT_SECONDS),
            ]
        )
        self._desired = "off"
        deadline = self.monotonic() + MACHINE_POWER_COMMAND_TIMEOUT_SECONDS
        while self._watchdog.poll() is None:
            ack = read_record(self.directory / "ack")
            if ack.get("generation") == str(self._generation) and ack.get("watchdog_pid") == str(
                self._watchdog.pid
            ):
                return
            if self.monotonic() >= deadline:
                break
            # Shutdown can wake this wait; its wall-clock deadline still bounds it.
            self._stop.wait(0.02)
        raise OSError("watchdog did not acknowledge")

    def _retire_watchdog_group(self, process):
        """A killed shell may have left an in-flight pmset command behind."""
        group = f"-{process.pid}"
        self.run(["/bin/kill", "-TERM", group], MACHINE_POWER_COMMAND_TIMEOUT_SECONDS)
        deadline = self.monotonic() + MACHINE_POWER_COMMAND_TIMEOUT_SECONDS
        while True:
            alive = self.run(["/bin/kill", "-0", group], MACHINE_POWER_COMMAND_TIMEOUT_SECONDS)
            if alive.returncode:
                if "not permitted" in (alive.stderr or "").lower():
                    raise OSError("watchdog command group could not be retired")
                return
            if self.monotonic() >= deadline:
                break
            self._stop.wait(0.02)
        self.run(["/bin/kill", "-KILL", group], MACHINE_POWER_COMMAND_TIMEOUT_SECONDS)
        alive = self.run(["/bin/kill", "-0", group], MACHINE_POWER_COMMAND_TIMEOUT_SECONDS)
        if alive.returncode == 0 or "not permitted" in (alive.stderr or "").lower():
            raise OSError("watchdog command group is still alive")

    def _replace_lost_watchdog(self):
        process = self._watchdog
        owned = read_record(self.directory / "activation").get("set") == "1"
        try:
            self._retire_watchdog_group(process)
            self._watchdog = None
            self._start_watchdog(recover=owned)
        except Exception:
            if owned:
                self._cleanup_failed("clear_failed")
            raise

    def _consume_result(self) -> bool:
        result = read_record(self.directory / "result")
        generation = str(self._generation)
        if result.get("generation") != generation or result.get("complete") != "1":
            return False
        if self._state["result_generation"] == generation:
            return True
        cause = result.get("cause")
        if cause:
            self._record_release(cause)
        # Both failures remain independently recorded in the durable watchdog
        # result. The single API warning prioritizes the failed clear remedy.
        if result.get("clear_failed") == "1" or result.get("sleep_failed") == "1":
            self._cleanup_failed(
                "clear_failed" if result.get("clear_failed") == "1" else "sleep_failed"
            )
        self._lid_active = False
        self._state["result_generation"] = generation
        self._save_best_effort()
        return True

    def _cleanup_failed(self, kind):
        self._state["latched"] = "cleanup_failure"
        self._state["cleanup_failure"] = {
            "kind": kind,
            "command": "sudo pmset -a disablesleep 0",
        }
        self._save_best_effort()

    def _record_release(self, cause):
        self._release_cause = cause
        self._state["last_release"] = {
            "cause": cause,
            "at": datetime.fromtimestamp(self.clock(), UTC).isoformat().replace("+00:00", "Z"),
        }
        if cause == "thermal":
            self._state["latched"] = "thermal"
        if cause == "battery_floor":
            self._state["battery_blocked"] = True
        self._save_best_effort()

    def _drop_idle(self):
        if self._idle is None:
            return
        process, self._idle = self._idle, None
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=MACHINE_POWER_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=MACHINE_POWER_COMMAND_TIMEOUT_SECONDS)

    def _release(self, cause, *, keep_idle=False):
        dropping_idle = self._idle is not None and not keep_idle
        held = dropping_idle or self._desired == "on"
        # Ending demand is only a release when something was held; a safety
        # cause is always recorded because it latches or blocks re-arming.
        if held or (cause != self._release_cause and cause != "demand_gone"):
            self._record_release(cause)  # Best effort, before any cleanup.
        # Publish off before waiting for the idle assertion to terminate.
        try:
            if (
                self._watchdog is None
                and self.installer.status().install_problem in {None, "partial"}
                and self._acquire()
            ):
                if read_record(self.directory / "activation").get("set") == "1":
                    self._start_watchdog(recover=True)
                else:
                    self._close_owner()
            if self._watchdog is not None:
                if self._watchdog.poll() is not None:
                    complete = self._consume_result()
                    if read_record(self.directory / "activation").get("set") == "1" or (
                        self._desired == "on" and not complete
                    ):
                        self._replace_lost_watchdog()
                if self._watchdog is not None:
                    self._heartbeat("off", cause)
        finally:
            self._lid_active = False
            if not keep_idle:
                self._drop_idle()

    def safety_pass(self):
        with self._lock:
            # A pass queued behind stop() must not re-arm after shutdown cleanup.
            if self.platform != "macos" or (self._stop.is_set() and not self._running):
                return
            try:
                self._pass()
            except Exception:
                logger.exception("Machine power safety pass failed")
                try:
                    self._release("reading_failed")
                except Exception:
                    # A failed heartbeat still expires through the watchdog.
                    logger.exception("Could not publish machine power release")

    def _pass(self):
        complete = self._consume_result()
        if self._watchdog is not None and self._watchdog.poll() is not None:
            was_on = self._desired == "on"
            owned = read_record(self.directory / "activation").get("set") == "1"
            if owned or (was_on and not complete):
                self._record_release("watchdog_lost")
                self._replace_lost_watchdog()
                self._release("watchdog_lost")
                return
            self._watchdog = None
            self._close_owner()

        # Attempt each reading on every pass; one failure never silently defaults
        # a battery, thermal, lid, or flag input to a safe value.
        readings = {}
        failed = False
        for name, argv, parser in (
            ("battery", [PMSET, "-g", "batt"], parse_battery),
            ("thermal", [PMSET, "-g", "therm"], parse_thermal),
            ("lid", [IOREG, "-r", "-k", "AppleClamshellState"], parse_lid),
            ("flag", [PMSET, "-g"], parse_flag),
        ):
            try:
                readings[name] = parser(self._read(argv))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                failed = True
        try:
            self._reasons = self.demand_reader()
        except Exception:
            logger.exception("Could not read machine power demand")
            self._reasons = []
            failed = True
        battery = readings.get("battery")
        if battery and battery[0] and self._state["battery_blocked"]:
            self._state["battery_blocked"] = False
            self._save_best_effort()
        cause = None
        if readings.get("thermal"):
            cause = "thermal"
        elif (
            battery
            and not battery[0]
            and battery[1] <= MACHINE_POWER_BATTERY_FLOOR
            or self._state["battery_blocked"]
        ):
            cause = "battery_floor"
        elif failed:
            cause = "reading_failed"
        elif not self._reasons:
            cause = "demand_gone"

        installation = self.installer.status()
        installed = installation.installed
        if self._watchdog is None:
            can_recover = installation.install_problem in {None, "partial"}
            if can_recover and not self._acquire():
                return
            activation = read_record(self.directory / "activation")
            owned = activation.get("set") == "1"
            self._external = readings.get("flag", False) and not owned
            # Recover a previously owned flag even with partial installation or
            # failed safety inputs. The replacement executor receives only off.
            if owned and can_recover:
                self._start_watchdog(recover=True)
                self._release(cause or "watchdog_lost")
                return
            self._close_owner()
        # A failed reading only blocks lid mode. The idle hold is an ordinary
        # assertion that macOS still overrides at low battery, and a desktop Mac
        # has no lid to read.
        if cause and cause != "reading_failed":
            self._release(cause)
            return
        if self._state["idle_hold"] and self._reasons:
            if self._idle is None or self._idle.poll() is not None:
                pid, _ = self.identity_reader()
                self._idle = self.spawn(["/usr/bin/caffeinate", "-i", "-w", str(pid)])
        else:
            self._drop_idle()
        if cause:
            self._release(cause, keep_idle=True)
            return

        if not self._state["lid_mode"] or self._state["latched"]:
            if self._watchdog is not None:
                if self._desired == "on":
                    self._record_release("disabled")
                self._heartbeat("off", "disabled")
                self._lid_active = False
            return
        if not installed or self._external or not self._acquire():
            return
        if self._state["latched"]:
            self._close_owner()
            return
        if self._watchdog is None:
            self._start_watchdog()
        elif self._desired == "off":
            # Off is terminal. Never revive the same executor while its release
            # is in flight; a later pass allocates a fresh generation after exit.
            return
        self._heartbeat("on")
        self._release_cause = None
        activation = read_record(self.directory / "activation")
        self._lid_active = (
            readings["flag"]
            and activation.get("generation") == str(self._generation)
            and activation.get("set") == "1"
        )
