"""Keep this machine from idle-sleeping while RCP has work.

The hold is an ordinary power assertion: no root, no persistent system state.
The OS still overrides it, so a closed lid or a battery emergency sleeps the
machine, and the assertion dies with this process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable

from rcp.agents.provider_accounts import account_login_refusal
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.config import load_manifest
from rcp.episode_health import load_episode_health
from rcp.limits import MACHINE_POWER_PASS_SECONDS
from rcp.runs.provider_login import provider_login_host

logger = logging.getLogger(__name__)

# One profile per platform: the command that holds an idle-sleep assertion
# until the given process exits. A platform without an entry is unsupported.
IDLE_HOLD_COMMANDS: dict[str, Callable[[int], list[str]]] = {
    "darwin": lambda pid: ["/usr/bin/caffeinate", "-i", "-w", str(pid)],
}


def spawn_command(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


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
    """One thread that holds the idle assertion exactly while there is demand."""

    def __init__(
        self,
        store,
        *,
        demand_reader: Callable[[], list[str]],
        spawn: Callable[[list[str]], subprocess.Popen] = spawn_command,
        platform: str = sys.platform,
    ):
        self.store, self.demand_reader, self.spawn = store, demand_reader, spawn
        self.command = IDLE_HOLD_COMMANDS.get(platform)
        self._lock = threading.Lock()
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

    def status(self) -> dict:
        with self._lock:
            return {
                "supported": self.command is not None,
                "idle_hold": {"enabled": self._enabled, "active": self._held()},
                "demand_reasons": list(self._reasons),
            }

    def update(self, preferences: dict) -> dict:
        if set(preferences) != {"idle_hold"} or type(preferences["idle_hold"]) is not bool:
            raise ValueError("invalid machine power preferences")
        with self._lock:
            self._enabled = preferences["idle_hold"]
            with self.store.connection() as connection:
                connection.execute(
                    "INSERT INTO machine_power_state VALUES (1,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET value=excluded.value",
                    (json.dumps({"idle_hold": self._enabled}),),
                )
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
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        with self._lock:
            self._drop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.safety_pass()
            self._stop.wait(MACHINE_POWER_PASS_SECONDS)

    def safety_pass(self) -> None:
        try:
            reasons = self.demand_reader()
        except Exception:
            # Keep the current hold; a failed read is not evidence that work ended.
            logger.exception("Could not read keep-awake demand")
            return
        with self._lock:
            self._reasons = reasons
            if self.command is None or self._stop.is_set():
                return
            if self._enabled and reasons:
                if not self._held():
                    self._hold = self.spawn(self.command(os.getpid()))
            else:
                self._drop()

    def _held(self) -> bool:
        return self._hold is not None and self._hold.poll() is None

    def _drop(self) -> None:
        if self._hold is not None:
            if self._hold.poll() is None:
                self._hold.terminate()
                self._hold.wait()
            self._hold = None
