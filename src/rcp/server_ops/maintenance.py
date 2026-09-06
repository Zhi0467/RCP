"""Application-owned admission and quiescence, independent of deployment journals."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks, StartupEffectFence
    from rcp.projects import ProjectCatalog
    from rcp.server_ops.control import ServerControlBackupCaptureResult
    from rcp.storage import AppStore


class MaintenanceRefused(RuntimeError):
    """The application could not prove its requested maintenance boundary."""


class MaintenanceAdmissionClosed(RuntimeError):
    """New work was refused while the application is quiescent."""


@dataclass(frozen=True)
class MaintenanceIdentity:
    maintenance_id: str
    boundary_sha256: str

    def __post_init__(self) -> None:
        value = uuid.UUID(self.maintenance_id)
        if value.version != 4 or str(value) != self.maintenance_id:
            raise ValueError("maintenance identity must be a canonical UUID4")
        if len(self.boundary_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.boundary_sha256
        ):
            raise ValueError("maintenance boundary must be a lowercase SHA-256 digest")


class RuntimeAdmissionGate:
    """Close new mutations while allowing already-entered calls to finish."""

    def __init__(
        self,
        *,
        closed: bool = False,
        reason: str = "Server maintenance",
    ) -> None:
        if not reason or reason != reason.strip():
            raise ValueError("admission reason must be one nonempty line")
        self._closed = closed
        self._reason = reason
        self._active = 0
        self._condition = threading.Condition()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def require_open(self, effect: str) -> None:
        if not effect or effect != effect.strip():
            raise ValueError("admission effect must be one nonempty line")
        with self._condition:
            if not self._closed:
                return
        raise MaintenanceAdmissionClosed(f"{self._reason} blocks {effect}.")

    @contextmanager
    def mutation(self, effect: str) -> Iterator[None]:
        if not effect or effect != effect.strip():
            raise ValueError("mutation effect must be one nonempty line")
        with self._condition:
            if self._closed:
                raise MaintenanceAdmissionClosed(f"{self._reason} blocks {effect}.")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close_and_wait(
        self,
        *,
        timeout: float,
        additional_idle: Callable[[], bool] = lambda: True,
    ) -> None:
        if timeout <= 0:
            raise ValueError("maintenance admission timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._condition:
            self._closed = True
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MaintenanceRefused(
                        "Timed out waiting for in-flight server mutations to settle."
                    )
                self._condition.wait(min(remaining, 0.25))
        while not additional_idle():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MaintenanceRefused(
                    "Timed out waiting for in-flight provider work to reach a durable boundary."
                )
            time.sleep(min(remaining, 0.05))

    def reopen(self) -> None:
        with self._condition:
            self._closed = False
            self._condition.notify_all()


class MaintenanceCoordinator:
    """Prove a process boundary without selecting releases or persisting transitions."""

    def __init__(
        self,
        *,
        identity: MaintenanceIdentity | None = None,
        admission: RuntimeAdmissionGate,
        background_admission: RuntimeAdmissionGate,
        background: BackgroundAgentTasks,
        capture_sqlite: Callable[[], ServerControlBackupCaptureResult],
        catalog: ProjectCatalog,
        store: AppStore,
        startup_effect_fence: StartupEffectFence | None = None,
        runtime_started: threading.Event | None = None,
        runtime_error: Callable[[], str | None] = lambda: None,
        pause_runtime_owners: Callable[[float], None] = lambda timeout: None,
        resume_runtime_owners: Callable[[], None] = lambda: None,
    ) -> None:
        self.identity = identity
        self.admission = admission
        self.background_admission = background_admission
        self.background = background
        self.capture_sqlite = capture_sqlite
        self.catalog = catalog
        self.store = store
        self.fence = startup_effect_fence
        self.runtime_started = runtime_started
        self.runtime_error = runtime_error
        self.pause_runtime_owners = pause_runtime_owners
        self.resume_runtime_owners = resume_runtime_owners
        self.quiescent = identity is not None
        self.capture = None
        self.verification_sha256 = None

    def require_identity(self, identity: MaintenanceIdentity) -> None:
        if self.identity != identity:
            raise MaintenanceRefused("This process belongs to another maintenance boundary.")

    def enter(self, identity: MaintenanceIdentity, *, timeout: float) -> None:
        if (
            self.identity is not None
            and not self.quiescent
            and not self.admission.closed
            and not self.background_admission.closed
        ):
            self.identity = None
            self.capture = None
            self.verification_sha256 = None
            if self.fence is not None and not self.fence.active:
                self.fence = None
        if self.identity is not None:
            self.require_identity(identity)
            if self.quiescent:
                return
        else:
            self.identity = identity
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise MaintenanceRefused("Timed out waiting for application quiescence.")
            return value

        self.background.close_watcher_notifications()
        self.admission.close_and_wait(timeout=remaining())
        self.background_admission.close_and_wait(
            timeout=remaining(), additional_idle=self.background.runtime_is_idle
        )
        self.pause_runtime_owners(remaining())
        self.background_admission.close_and_wait(
            timeout=remaining(), additional_idle=self.background.runtime_is_idle
        )
        self.capture = self.capture_sqlite()
        self.quiescent = True

    def verify(self, identity: MaintenanceIdentity, *, proof_path: Path, proof_sha256: str) -> None:
        self.require_identity(identity)
        if not self.quiescent or not self.admission.closed or not self.background_admission.closed:
            raise MaintenanceRefused(
                "Application verification requires closed, quiescent admission."
            )
        if self.fence is None or not self.fence.active or self.fence.attempted_effects:
            raise MaintenanceRefused(
                "Application verification requires an intact startup-effect fence."
            )
        from rcp.server_ops.deployment import verify_live_application

        self.verification_sha256 = verify_live_application(
            proof_path,
            proof_sha256=proof_sha256,
            background=self.background,
            catalog=self.catalog,
            store=self.store,
        )

    def release(self, identity: MaintenanceIdentity, *, timeout: float) -> None:
        self.require_identity(identity)
        # An ordinary enter may time out after closing only part of admission.
        # The exact caller can abort that in-memory boundary and resume owners;
        # a probationary startup still requires its complete verified fence.
        if not self.quiescent and self.fence is not None:
            raise MaintenanceRefused("The maintenance boundary has not reached quiescence.")
        if self.fence is not None and self.verification_sha256 is None:
            raise MaintenanceRefused(
                "A fenced startup requires application verification before admission opens."
            )
        self.background_admission.reopen()
        if self.fence is None:
            self.background.accept_watcher_notifications()
            self.resume_runtime_owners()
        else:
            self.fence.release()
            if self.runtime_started is not None and not self.runtime_started.wait(timeout):
                raise MaintenanceRefused(
                    "Deferred runtime startup did not complete before timeout."
                )
            if self.runtime_error():
                raise MaintenanceRefused("Deferred runtime startup failed after admission release.")
        self.admission.reopen()
        self.quiescent = False
