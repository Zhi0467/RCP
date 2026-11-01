from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rcp.compute_jobs import jobs
from rcp.compute_jobs.backend_context import ComputeTransportError
from rcp.storage import AppStore
from rcp.transport import compute_process_owner
from rcp.watchers import WatcherCheckResult, WatcherPoller

from .test_compute_jobs_storage import job_record
from .test_watchers import _record


@pytest.fixture
def helper(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "app.sqlite")
    root = tmp_path / "jobs" / "helper"
    root.mkdir(parents=True)
    job = job_record(
        job_root=str(root),
        log_path=str(root / "log"),
        exit_path=str(root / "exit"),
        containment="mirrored",
        origin_operation_id="origin",
    )
    store.create_compute_job(job)
    watcher = _record("helper").model_copy(update=jobs.helper_watch_spec(job))
    store.create_watchers([watcher])

    class Backend:
        alive_state = False
        failure = None
        calls = 0

        def cancel(self, handle, context):
            assert handle == job.backend_handle
            assert context.execution_host == job.execution_host
            assert context.writable_roots == context.protected_paths == ()
            self.calls += 1
            if self.failure:
                raise self.failure
            self.alive_state = False

        def alive(self, handle, context):
            return self.alive_state

    backend = Backend()
    monkeypatch.setitem(jobs.COMPUTE_BACKENDS, job.backend_id, backend)

    def poller(owner=store):
        return WatcherPoller(
            owner,
            check_runner=lambda *_: WatcherCheckResult(
                state="error", checked_at=owner.now(), error="No valid exit receipt"
            ),
            cancel_runner=lambda *_: pytest.fail("helper cancellation must use its server owner"),
            helper_cancel_action=lambda record: jobs.helper_cancel_action(owner, record),
        )

    return store, job, watcher, backend, poller


@pytest.mark.parametrize("notified", [False, True])
def test_partial_read_only_cancel_recovers_lost_helper_without_replaying_delivery(
    helper, monkeypatch, notified
):
    store, job, watcher, backend, poller = helper
    backend.alive_state = True

    def stopped(*_args, **_kwargs):
        backend.alive_state = False
        return jobs.subprocess.CompletedProcess([], 0, "", "")

    original = Path.write_text

    def read_only(path, *args, **kwargs):
        if path.name == ".cancelled.tmp":
            raise PermissionError("Read-only file system")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(compute_process_owner.subprocess, "run", stopped)
        patch.setattr(Path, "write_text", read_only)
        assert (
            compute_process_owner.main(
                [
                    str(Path(job.job_root) / "owner.py"),
                    "cancel",
                    job.backend_id,
                    job.backend_handle,
                    "1",
                ]
            )
            == 2
        )
    assert not backend.alive_state
    assert not Path(job.job_root, "cancelled").exists()
    lost = store.record_compute_job_refresh(
        job.job_id,
        status="lost",
        ended_at="2026-09-06T01:00:00Z",
        diagnostic="Compute job disappeared without an exit file.",
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET status='degraded', last_error='No valid exit receipt', "
            "notified=?, notification_operation_id=?, consecutive_error_count=10 WHERE watcher_id=?",
            (int(notified), "delivered-warning" if notified else None, watcher.watcher_id),
        )
    result = poller().cancel("project", watcher.watcher_id, "human")
    cancelled = store.compute_job(job.job_id)
    assert cancelled.status == "cancelled"
    assert cancelled.exit_status is None and cancelled.diagnostic == lost.diagnostic
    assert cancelled.ended_at != lost.ended_at
    assert Path(job.job_root, "cancelled").read_text().isdigit()
    assert not Path(job.exit_path).exists()
    assert result.status == "completed" and result.completed_at
    assert result.cancel_requested_by == "human" and result.cancel_error is None
    assert result.notified == notified
    assert result.notification_operation_id == ("delivered-warning" if notified else None)
    assert not result.can_cancel
    if notified:
        assert store.completed_watcher_groups() == []
        poller().poll_once()
        assert store.watcher(watcher.watcher_id) == result


def test_helper_lookup_is_uncapped_and_exact(helper, monkeypatch):
    store, job, watcher, backend, poller = helper
    store.create_compute_job(job_record("newer", created_at="2026-09-07T00:00:00Z"))
    monkeypatch.setattr("rcp.storage.compute_jobs.COMPUTE_JOBS_PER_PROJECT_LIST_LIMIT", 1)
    assert [row.job_id for row in store.compute_jobs("project")] == ["newer"]
    assert poller().cancel("project", watcher.watcher_id, "human").status == "completed"
    assert backend.calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "other"),
        ("execution_host", "other-host"),
        ("origin_operation_id", "other-origin"),
        ("check_command", "false"),
        ("cancel_command", "true"),
        ("cwd", "/other"),
        ("log_path", "/other/log"),
    ],
)
def test_wrong_helper_identity_keeps_generic_check_error_refusal(helper, field, value):
    store, _job, watcher, backend, poller = helper
    with store.connection() as connection:
        connection.execute(
            f"UPDATE watchers SET {field}=? WHERE watcher_id=?", (value, watcher.watcher_id)
        )
    record = store.watcher(watcher.watcher_id)
    assert jobs.helper_cancel_action(store, record) is None
    refused = poller().cancel(record.project_id, record.watcher_id, "human")
    assert refused.cancel_requested_at is None
    assert "Cancel was not run" in refused.cancel_error
    assert backend.calls == 0


def test_concurrent_helper_cancel_claim_precedes_owner_effects(helper):
    store, _job, watcher, backend, poller = helper
    entered, release = threading.Event(), threading.Event()
    cancel = backend.cancel

    def blocking(*args):
        claim = store.watcher(watcher.watcher_id)
        assert claim.cancel_requested_by == "first"
        entered.set()
        assert release.wait(5)
        cancel(*args)

    backend.cancel = blocking
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(poller().cancel, "project", watcher.watcher_id, "first")
        assert entered.wait(5)
        second = pool.submit(
            poller(AppStore(store.path)).cancel, "project", watcher.watcher_id, "second"
        ).result(timeout=5)
        assert second.cancel_requested_by == "first"
        release.set()
        assert first.result(timeout=5).status == "completed"
    assert backend.calls == 1


@pytest.mark.parametrize("failure", ["unreachable", "receipt", "active", "unknown", "storage"])
def test_helper_cancel_uncertainty_stays_retryable(helper, monkeypatch, failure):
    store, job, watcher, backend, poller = helper
    store.record_compute_job_refresh(job.job_id, status="lost", diagnostic="Earlier loss")
    if failure == "unreachable":
        backend.failure = ComputeTransportError("SSH unreachable")
    elif failure == "receipt":
        monkeypatch.setattr(
            jobs,
            "write_job_file",
            lambda *_: (_ for _ in ()).throw(PermissionError("Read-only receipt")),
        )
    elif failure in {"active", "unknown"}:
        backend.alive = lambda *_: True if failure == "active" else None
    else:
        monkeypatch.setattr(
            store,
            "record_compute_job_cancelled",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
        )
    result = poller().cancel("project", watcher.watcher_id, "human")
    assert result.can_cancel and result.cancel_error
    assert result.completed_at is None
    assert result.cancel_requested_by == "human"
    assert store.compute_job(job.job_id).status == "lost"


def test_cancel_completion_requires_current_claim_timestamp(helper):
    store, _job, watcher, _backend, _poller = helper
    claimed = store.claim_watcher_cancel("project", watcher.watcher_id, "human")
    unchanged = store.complete_watcher_cancel(watcher.watcher_id, requested_at="older")
    assert unchanged == claimed
    completed = store.complete_watcher_cancel(
        watcher.watcher_id, requested_at=claimed.cancel_requested_at
    )
    assert completed.status == "completed"


def test_helper_cancel_retries_after_registry_settles_but_watcher_persistence_fails(
    helper, monkeypatch
):
    store, job, watcher, _backend, poller = helper
    with store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET status='degraded', notified=1, "
            "notification_operation_id='warning' WHERE watcher_id=?",
            (watcher.watcher_id,),
        )
    complete = store.complete_watcher_cancel
    with monkeypatch.context() as patch:
        patch.setattr(
            store,
            "complete_watcher_cancel",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("watcher storage unavailable")
            ),
        )
        failed = poller().cancel("project", watcher.watcher_id, "first")
    assert failed.can_cancel and failed.cancel_error
    assert store.compute_job(job.job_id).status == "cancelled"
    assert store.complete_watcher_cancel == complete
    settled = poller().cancel("project", watcher.watcher_id, "second")
    assert settled.status == "completed" and settled.notified
    assert settled.notification_operation_id == "warning"
    assert settled.cancel_requested_by == "second" and settled.cancel_error is None
