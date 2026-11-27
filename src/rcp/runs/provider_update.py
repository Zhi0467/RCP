"""Process-local provider update jobs with synchronous admission."""

from __future__ import annotations

import os
import pwd
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.agents import AgentLauncher
from rcp.machine_cards import card
from rcp.providers import ProviderId
from rcp.runs.provider_auto_update import update_provider
from rcp.server_ops.provider_update import (
    LocalUpdateTarget,
    ProviderUpdateRefused,
    RemoteUpdateTarget,
    UpdateTarget,
    run_provider_process_as_self,
)
from rcp.storage import AppStore
from rcp.storage.models import SpaceMachineRecord


class ProviderUpdateJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    machine_id: str
    state: Literal["running", "succeeded", "failed"]
    detail: str | None = None
    before: str | None = None
    after: str | None = None
    started_at: str
    finished_at: str | None = None


class ProviderUpdateRunner:
    def __init__(
        self,
        store: AppStore,
        launcher: AgentLauncher,
        *,
        guard: Callable[[], AbstractContextManager[object]] = nullcontext,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self._guard = guard
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], ProviderUpdateJob] = {}

    def status(self, provider: ProviderId, machine_id: str) -> ProviderUpdateJob | None:
        with self._lock:
            return self._jobs.get((provider, machine_id))

    def _target(self, machine: SpaceMachineRecord) -> UpdateTarget:
        if machine.host:
            return RemoteUpdateTarget(machine.host, machine.os_account)
        return LocalUpdateTarget(pwd.getpwuid(os.geteuid()), run_provider_process_as_self)

    def start(self, provider: ProviderId, machine_id: str) -> ProviderUpdateJob:
        machine = card(self.store, machine_id)
        key = (provider, machine_id)
        with self._lock:
            if self._stop.is_set():
                raise ProviderUpdateRefused("Provider updates are stopping.")
            previous = self._jobs.get(key)
            if previous is not None and previous.state == "running":
                raise ProviderUpdateRefused("Already updating.")
            job = ProviderUpdateJob(
                provider=provider,
                machine_id=machine_id,
                state="running",
                started_at=self.store.now(),
            )
            self._jobs[key] = job
        settled = threading.Event()
        admitted = threading.Event()
        refusal: list[ProviderUpdateRefused] = []

        @contextmanager
        def guard() -> Iterator[None]:
            with self._guard():
                admitted.set()
                settled.set()
                yield

        def run() -> None:
            target_created = False
            try:
                target = self._target(machine)
                target_created = True
                change = update_provider(
                    self.store,
                    self.launcher,
                    provider,
                    machine_id,
                    target=target,
                    stop=self._stop,
                    guard=guard,
                )
                result = job.model_copy(
                    update={
                        "state": "succeeded",
                        "before": change.before,
                        "after": change.after,
                        "finished_at": self.store.now(),
                    }
                )
            except Exception as exc:
                if (
                    target_created
                    and isinstance(exc, ProviderUpdateRefused)
                    and not admitted.is_set()
                ):
                    refusal.append(exc)
                else:
                    self.launcher.set_update_warning(provider, machine_id, str(exc))
                result = job.model_copy(
                    update={"state": "failed", "detail": str(exc), "finished_at": self.store.now()}
                )
            with self._lock:
                self._jobs[key] = result
                self._threads.discard(threading.current_thread())
            settled.set()

        thread = threading.Thread(target=run, name="rcp-provider-update", daemon=True)
        try:
            with self._lock:
                if self._stop.is_set():
                    raise ProviderUpdateRefused("Provider updates are stopping.")
                self._threads.add(thread)
                thread.start()
        except Exception:
            with self._lock:
                self._jobs.pop(key, None)
                self._threads.discard(thread)
            raise
        settled.wait()
        if refusal:
            raise refusal[0]
        return job

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            threads = tuple(self._threads)
        for thread in threads:
            thread.join()
