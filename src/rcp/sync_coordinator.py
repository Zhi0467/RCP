"""Lease-driven remote captures; display requests never join transfer workers."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from rcp.limits import (
    REMOTE_STATE_CLIENT_LEASE_SECONDS,
    REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS,
)
from rcp.projects import ProjectCatalog
from rcp.transport.state import SSHStateWorkspace

_LOGGER = logging.getLogger(__name__)


@dataclass
class _Capture:
    project_ids: set[str]
    workspace: SSHStateWorkspace
    worker: threading.Thread | None = None
    running: bool = False
    last_started: float | None = None
    follow_up: bool = False


class SyncCoordinator:
    def __init__(
        self,
        catalog: ProjectCatalog,
        *,
        on_capture: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._catalog = catalog
        self._on_capture = on_capture
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, tuple[float, set[str]]] = {}
        self._activated: set[str] = set()
        self._requested: set[str] = set()
        self._captures: dict[str, _Capture] = {}
        self._wake = threading.Event()
        self._runner: threading.Thread | None = None
        self._stopping = False
        catalog.request_sync = self.request

    def note_demand(
        self, client_id: str, open_project_ids: set[str], activated: str | None
    ) -> None:
        """The caller has already filtered membership; only remote roots get leases."""
        remote = {
            self._catalog.resolve_project_id(project_id)
            for project_id in open_project_ids
            if isinstance(self._catalog.workspace_for_display(project_id), SSHStateWorkspace)
        }
        with self._lock:
            if self._stopping:
                return
            if remote:
                self._leases[client_id] = (
                    self._clock() + REMOTE_STATE_CLIENT_LEASE_SECONDS,
                    remote,
                )
            else:
                self._leases.pop(client_id, None)
            if activated is not None:
                activated = self._catalog.resolve_project_id(activated)
                if activated in remote:
                    self._activated.add(activated)
        self._notify()

    def request(self, project_id: str) -> None:
        """Schedule a cold display read once, without extending a browser lease."""
        project_id = self._catalog.resolve_project_id(project_id)
        if not isinstance(self._catalog.workspace_for_display(project_id), SSHStateWorkspace):
            return
        with self._lock:
            if self._stopping:
                return
            self._requested.add(project_id)
        self._notify()

    def _notify(self) -> None:
        self._wake.set()

    def sync_health(self, project_id: str) -> dict[str, object]:
        workspace = self._catalog.workspace_for_display(project_id)
        if isinstance(workspace, SSHStateWorkspace):
            return workspace.sync_health()
        return {"last_completed_at": None, "status": "local", "error": None}

    def start(self) -> None:
        with self._lock:
            if self._runner is not None and self._runner.is_alive():
                return
            self._stopping = False
            self._runner = threading.Thread(target=self._run, name="remote-sync", daemon=True)
            self._runner.start()

    async def stop(self) -> None:
        """Stop admission, then drain real transfer workers before releasing ownership."""
        with self._lock:
            self._stopping = True
            runner = self._runner
        self._wake.set()
        if runner is not None:
            # Transfers belong to the process, not the caller's event loop.
            await asyncio.to_thread(runner.join)

    def _run(self) -> None:
        try:
            while True:
                self._wake.clear()
                with self._lock:
                    if self._stopping:
                        return
                delay = self._schedule()
                self._wake.wait(delay)
        finally:
            with self._lock:
                workers = [capture.worker for capture in self._captures.values()]
            for worker in workers:
                if worker is not None:
                    worker.join()

    def _schedule(self) -> float:
        now = self._clock()
        with self._lock:
            if self._stopping:
                return 0
            self._leases = {
                client: lease for client, lease in self._leases.items() if lease[0] > now
            }
            demanded = set().union(*(lease[1] for lease in self._leases.values()))
            activated, self._activated = self._activated, set()
            requested, self._requested = self._requested, set()
            delays = [expiry - now for expiry, _projects in self._leases.values()]
            grouped: dict[str, tuple[SSHStateWorkspace, set[str]]] = {}
            for project_id in demanded | activated | requested:
                workspace = self._catalog.workspace_for_display(project_id)
                if not isinstance(workspace, SSHStateWorkspace):
                    continue
                key = str(workspace.root.resolve())
                if key not in grouped:
                    grouped[key] = (workspace, set())
                grouped[key][1].add(project_id)
            for key, (workspace, project_ids) in grouped.items():
                capture = self._captures.setdefault(key, _Capture(set(), workspace))
                capture.project_ids.update(project_ids)
                forced = bool(project_ids & activated)
                if capture.running:
                    capture.follow_up |= forced
                    continue
                due = (
                    capture.last_started is None
                    or now - capture.last_started >= REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS
                )
                if forced or capture.follow_up or due:
                    capture.follow_up = False
                    capture.last_started = now
                    capture.running = True
                    capture.worker = threading.Thread(
                        target=self._capture, args=(capture,), name="remote-capture", daemon=True
                    )
                    capture.worker.start()
                elif capture.last_started is not None:
                    self._requested.update(project_ids & requested)
                    delays.append(
                        capture.last_started + REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS - now
                    )
            return min(delays, default=REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS)

    def _capture(self, capture: _Capture) -> None:
        try:
            changed = capture.workspace.capture()
            if changed and self._on_capture is not None:
                with self._lock:
                    project_ids = tuple(capture.project_ids)
                for project_id in project_ids:
                    self._on_capture(project_id)
        except Exception:
            _LOGGER.warning("Remote state capture failed", exc_info=True)
        finally:
            with self._lock:
                capture.running = False
                if capture.follow_up and not self._stopping:
                    self._requested.update(capture.project_ids)
            self._wake.set()
