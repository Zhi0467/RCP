from __future__ import annotations

import threading

import pytest

from rcp.server_ops.maintenance import MaintenanceAdmissionClosed, RuntimeAdmissionGate
from tests.helpers import TASK_SETTLE_TIMEOUT, wait_until


def test_runtime_admission_drains_entered_mutation_before_reopening() -> None:
    gate = RuntimeAdmissionGate()
    entered = threading.Event()
    release = threading.Event()
    drained = threading.Event()

    def mutation():
        with gate.mutation("existing provider task"):
            entered.set()
            assert release.wait(TASK_SETTLE_TIMEOUT)

    def close():
        gate.close_and_wait(timeout=TASK_SETTLE_TIMEOUT)
        drained.set()

    worker = threading.Thread(target=mutation)
    closer = threading.Thread(target=close)
    worker.start()
    assert entered.wait(TASK_SETTLE_TIMEOUT)
    closer.start()
    try:
        wait_until(lambda: gate.closed, detail="maintenance never closed new admission")
        with pytest.raises(MaintenanceAdmissionClosed):
            gate.require_open("new provider task")
        assert not drained.is_set()
    finally:
        release.set()
        worker.join(TASK_SETTLE_TIMEOUT)
        closer.join(TASK_SETTLE_TIMEOUT)
    assert drained.is_set()
    gate.reopen()
    gate.require_open("new provider task")


def test_timed_out_ordinary_enter_can_abort_its_exact_boundary():
    import uuid
    from types import SimpleNamespace

    from rcp.server_ops.maintenance import (
        MaintenanceCoordinator,
        MaintenanceIdentity,
        MaintenanceRefused,
    )

    admission, background_gate = RuntimeAdmissionGate(), RuntimeAdmissionGate()
    events = []
    background = SimpleNamespace(
        close_watcher_notifications=lambda: events.append("closed"),
        accept_watcher_notifications=lambda: events.append("opened"),
        runtime_is_idle=lambda: False,
    )
    coordinator = MaintenanceCoordinator(
        admission=admission,
        background_admission=background_gate,
        background=background,
        capture_sqlite=lambda: pytest.fail("busy boundary captured"),
        catalog=None,
        store=None,
        resume_runtime_owners=lambda: events.append("resumed"),
    )
    identity = MaintenanceIdentity(str(uuid.uuid4()), "a" * 64)
    with pytest.raises(MaintenanceRefused, match="Timed out"):
        coordinator.enter(identity, timeout=0.01)
    assert admission.closed and background_gate.closed
    with pytest.raises(MaintenanceRefused, match="another maintenance"):
        coordinator.release(MaintenanceIdentity(str(uuid.uuid4()), "b" * 64), timeout=1)
    coordinator.release(identity, timeout=1)
    assert not admission.closed and not background_gate.closed
    assert events == ["closed", "opened", "resumed"]
