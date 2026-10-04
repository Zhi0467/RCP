"""Keep this machine from idle-sleeping while RCP has work.

The hold is an ordinary power assertion: no root, no persistent system state.
The OS still overrides it, so a closed lid or a battery emergency sleeps the
machine, and the assertion dies with this process. Opt-in lid mode layers a
separate watchdog-controlled SleepDisabled flag onto this idle-hold policy.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
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
    MACHINE_POWER_PASS_SECONDS,
    MACHINE_POWER_WATCHDOG_INTERVAL_SECONDS,
)
from rcp.machine_power_macos import InstallError, MacOSProfile
from rcp.runs.provider_login import provider_login_host

logger = logging.getLogger(__name__)
# Profiles own platform commands and optional lid-mode capabilities.
PLATFORM_PROFILES = {"darwin": MacOSProfile()}


def spawn_command(argv: list[str], *, pass_fds: tuple[int, ...] = ()) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=pass_fds,
    )


def run_command(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, timeout=timeout, capture_output=True, text=True, check=False)


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


def demand_snapshot(store, background) -> list[str]:
    """Coarse, local reads only; armed watchers alone never create demand."""
    reasons = []
    projects = store.projects()
    episodes = [
        episode
        for project in projects
        if project.home_space_id == store.space_id
        for episode in store.live_episodes(project.project_id)
    ]
    health = load_episode_health(store, episodes)
    if any(
        value[0] in {"starting", "active", "recovering", "stopping", "wrapping_up"}
        and not (value[0] == "wrapping_up" and value[3] == "sign_in")
        for value in health.values()
    ):
        reasons.append("episode")
    credentials = ProviderCredentialStore.for_data_dir(store.path.parent)
    for project in projects:
        for task in store.active_project_agent_tasks(project.project_id):
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


class MachinePowerController:
    """Demand-driven idle hold with a separately revocable lid executor."""

    def __init__(
        self,
        store,
        *,
        demand_reader: Callable[[], list[str]],
        spawn: Callable[[list[str]], subprocess.Popen] | None = None,
        run=run_command,
        clock=time.time,
        monotonic=time.monotonic,
        process_identity: Callable[[], tuple[int, str]] | None = None,
        directory: Path | None = None,
        installer=None,
        platform: str = sys.platform,
        profile=None,
    ):
        self.store, self.demand_reader = store, demand_reader
        self.spawn = spawn or self._spawn
        self.run, self.clock, self.monotonic = run, clock, monotonic
        self.profile = profile if profile is not None else PLATFORM_PROFILES.get(platform)
        self.lid = self.profile.lid_mode if self.profile is not None else None
        self.installer = installer or (self.lid.create_installer(run=run) if self.lid else None)
        self.directory = directory or (self.installer.paths.directory if self.installer else None)
        self.identity_reader = process_identity or (lambda: self.lid.process_identity(self.run))
        self._identity = None
        self._admin_lock = threading.Lock()
        self._uninstalling = False
        self._owner = None
        self._watchdog = None
        self._generation = 0
        self._desired = "off"
        self._external = False
        self._lid_active = False
        self._release_cause = None
        self.command = self.profile.idle_hold_command if self.profile is not None else None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hold: subprocess.Popen | None = None
        self._reasons: list[str] = []
        with store.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS machine_power_state "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM machine_power_state WHERE singleton=1"
            ).fetchone()
        self._enabled = bool(json.loads(row[0]).get("idle_hold", True)) if row else True

        self._state = {
            "lid_mode": False,
            "latched": None,
            "last_release": None,
            "cleanup_failure": None,
            "battery_blocked": False,
            "result_generation": None,
        }
        if row:
            self._state.update(
                {key: value for key, value in json.loads(row[0]).items() if key != "idle_hold"}
            )

    def status(self) -> dict:
        with self._lock:
            supported = self.command is not None
            installed = self.installer.status() if self.lid is not None else None
            return {
                "supported": supported,
                "installed": installed.installed if installed else False,
                "install_problem": installed.install_problem if installed else "not_installed",
                "idle_hold": {
                    "enabled": self._enabled,
                    "active": self._held(),
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
        if (
            not preferences
            or set(preferences) - {"idle_hold", "lid_mode"}
            or any(type(value) is not bool for value in preferences.values())
        ):
            raise ValueError("invalid machine power preferences")
        with self._lock:
            enabled = preferences.get("idle_hold", self._enabled)
            state = self._state | {
                key: value for key, value in preferences.items() if key != "idle_hold"
            }
            if preferences.get("lid_mode"):
                state["latched"] = None
                state["cleanup_failure"] = None
            # Persist first, so a failed write changes nothing live.
            with self.store.connection() as connection:
                connection.execute(
                    "INSERT INTO machine_power_state VALUES (1,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET value=excluded.value",
                    (json.dumps({"idle_hold": enabled, **state}),),
                )
            self._enabled = enabled
            self._state = state
            if not enabled:
                # Turning it off never waits on a demand read.
                self._drop()
        if self._thread is not None:
            self.safety_pass()
        return self.status()

    def start(self) -> None:
        if self.command is None or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rcp-machine-power", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if (
                self._thread is not None
                or self._hold is not None
                or self._watchdog is not None
                or (
                    self.lid is not None
                    and read_record(self.directory / "activation").get("set") == "1"
                )
            ):
                try:
                    self._release("shutdown")
                except Exception:
                    logger.exception(
                        "Could not publish machine power shutdown; watchdog will expire"
                    )
                finally:
                    self._drop()
        if self._thread:
            self._thread.join()
            self._thread = None
        with self._lock:
            try:
                self._wait_watchdog()
            except Exception:
                logger.exception("Machine power shutdown cleanup failed")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.safety_pass()
            self._stop.wait(MACHINE_POWER_PASS_SECONDS)

    def safety_pass(self) -> None:
        demand_failed = False
        try:
            reasons = self.demand_reader()
        except Exception:
            # Keep the current hold; a failed read is not evidence that work ended.
            logger.exception("Could not read keep-awake demand")
            demand_failed = True
        with self._lock:
            if not demand_failed:
                self._reasons = reasons
            if self.command is None or self._stop.is_set():
                return
            if not demand_failed and self._enabled and reasons:
                if not self._held():
                    try:
                        self._hold = self.spawn(self.command(os.getpid()))
                    except OSError:
                        # The next pass retries; the thread must outlive this.
                        logger.exception("Could not start the keep-awake hold")
            elif not demand_failed:
                self._drop()
            if self.lid is not None:
                try:
                    self._pass(demand_failed=demand_failed)
                except Exception:
                    logger.exception("Machine power safety pass failed")
                    try:
                        self._release("reading_failed")
                    except Exception:
                        logger.exception("Could not publish machine power release")

    def _held(self) -> bool:
        return self._hold is not None and self._hold.poll() is None

    def _drop(self) -> None:
        if self._hold is not None:
            if self._hold.poll() is None:
                self._hold.terminate()
                self._hold.wait()
            self._hold = None

    def _spawn(self, argv):
        # Only the watchdog inherits the machine lock. An idle hold or unrelated
        # child must not keep another data directory from acquiring ownership.
        fds = (
            (self._owner,)
            if self.lid is not None
            and self.lid.is_watchdog_command(argv)
            and self._owner is not None
            else ()
        )
        return spawn_command(argv, pass_fds=fds)

    def _save(self):
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO machine_power_state VALUES (1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET value=excluded.value",
                (json.dumps({"idle_hold": self._enabled, **self._state}),),
            )

    def _save_best_effort(self):
        try:
            self._save()
        except Exception:
            logger.exception("Could not save machine power release state")

    def install(self):
        with self._admin_lock:
            with self._lock:
                before = self.status()
                if self.lid is None or before["installed"]:
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
                if self._thread is not None:
                    self.safety_pass()
                return self.status()

    def uninstall(self):
        with self._admin_lock:
            with self._lock:
                before = self.status()
                if self.lid is None:
                    return before

            def quiesce():
                with self._lock:
                    self._uninstalling = True
                    self._release("disabled")
                if not self._wait_watchdog():
                    raise InstallError("owner_busy")

            try:
                # Acceptance precedes quiescence. Once accepted, retain the
                # lid-mode fence through removal so no pass can re-arm it.
                self.installer.uninstall(before_remove=quiesce)
                with self._lock:
                    if self.installer.cancelled:
                        return before
                    # Uninstall verified the flag clear, so its latches are moot.
                    self._state["lid_mode"] = False
                    self._state["latched"] = None
                    self._state["cleanup_failure"] = None
                    self._save_best_effort()
                    return self.status()
            except InstallError as exc:
                if exc.code == "clear_failed":
                    with self._lock:
                        self._cleanup_failed("clear_failed")
                raise
            finally:
                with self._lock:
                    self._uninstalling = False

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
            # It died after `off` was published but before clearing: recover
            # the owned flag before ownership is dropped.
            owned = read_record(self.directory / "activation").get("set") == "1"
            if recover and not complete and owned:
                self._replace_lost_watchdog()
                self._heartbeat("off", "shutdown")
                return self._wait_watchdog(recover=False)
            self._watchdog = None
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
        if not self.directory.is_dir():
            # A damaged install lost the state directory; nothing can own it.
            return False
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
        source = self.lid.watchdog_script
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
            self.lid.watchdog_command(script, self.directory, self._generation)
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
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            # A privileged child may still be exiting; prove the group is gone.
            pass
        if self._wait_group_gone(process.pid):
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        # A killed process can still exist for a moment after SIGKILL.
        if not self._wait_group_gone(process.pid):
            raise OSError("watchdog command group is still alive")

    def _wait_group_gone(self, pid) -> bool:
        deadline = self.monotonic() + MACHINE_POWER_COMMAND_TIMEOUT_SECONDS
        while True:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass  # The group exists, but is not ours to signal.
            if self.monotonic() >= deadline:
                return False
            self._stop.wait(0.02)

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

    def _consume_result(self, generation: str | None = None) -> bool:
        result = read_record(self.directory / "result")
        generation = str(self._generation) if generation is None else generation
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
            # A cleared flag does not sleep a closed Mac, so a failed sleep
            # needs its own remedy.
            "command": self.lid.cleanup_command(kind),
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

    def _release(self, cause):
        if self.lid is None:
            return
        self._consume_result()
        if (self._state["cleanup_failure"] or {}).get("kind") == "clear_failed":
            return
        held = self._desired == "on"
        # Ending demand is only a release when something was held; a safety
        # cause is always recorded because it latches or blocks re-arming.
        if held or (cause != self._release_cause and cause != "demand_gone"):
            self._record_release(cause)  # Best effort, before any cleanup.
        # Shutdown publishes off before waiting for the idle assertion to terminate.
        try:
            if (
                self._watchdog is None
                and self.installer.status().install_problem in {None, "partial"}
                and self._acquire()
            ):
                if (self._state["cleanup_failure"] or {}).get("kind") == "clear_failed":
                    self._close_owner()
                    return
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

    def _pass(self, *, demand_failed=False):
        if self._uninstalling:
            return
        complete = self._consume_result()
        if self._watchdog is not None and self._watchdog.poll() is not None:
            was_on = self._desired == "on"
            owned = read_record(self.directory / "activation").get("set") == "1"
            if not complete and (owned or was_on):
                self._record_release("watchdog_lost")
                self._replace_lost_watchdog()
                self._release("watchdog_lost")
                return
            self._watchdog = None
            self._close_owner()

        if (self._state["cleanup_failure"] or {}).get("kind") == "clear_failed":
            return
        if not self._state["lid_mode"] and self._watchdog is None:
            # A result left by a backend that died before reading it still
            # carries a cleanup failure the human must see.
            previous = read_record(self.directory / "result").get("generation")
            if previous:
                self._consume_result(previous)
            if self.installer.status().install_problem in {"other_account", "foreign_file"}:
                return
            if read_record(self.directory / "activation").get("set") != "1":
                return

        # Attempt each reading on every pass; one failure never silently defaults
        # a battery, thermal, lid, or flag input to a safe value.
        readings = {}
        failed = demand_failed
        for name, reader in (
            ("battery", self.lid.read_battery),
            ("thermal", self.lid.read_thermal),
            ("lid", self.lid.read_lid),
            ("flag", self.lid.read_flag),
        ):
            try:
                readings[name] = reader()
            except (OSError, ValueError):
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
        # Another account's installation files are not ours to read; only the
        # independent idle hold applies.
        trusted = installation.install_problem not in {"other_account", "foreign_file"}
        if self._watchdog is None and trusted:
            can_recover = installation.install_problem in {None, "partial"}
            # Another backend owning lid mode gates only recovery and lid mode;
            # this backend's idle hold still follows its own demand.
            if not can_recover or self._acquire():
                if (self._state["cleanup_failure"] or {}).get("kind") == "clear_failed":
                    self._close_owner()
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
        if cause:
            self._release(cause)
            return

        # A broken installation may have lost the boot reset, so it releases now
        # rather than when the heartbeat goes stale.
        if not self._state["lid_mode"] or self._state["latched"] or not installed:
            if self._watchdog is not None:
                if self._desired == "on":
                    self._record_release("disabled")
                    self._heartbeat("off", "disabled")
                self._lid_active = False
            return
        if self._external or not self._acquire():
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
