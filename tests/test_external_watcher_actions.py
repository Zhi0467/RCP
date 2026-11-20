from __future__ import annotations

import shlex

from rcp.compute_jobs.models import ComputeBackendProbe
from rcp.storage import AppStore
from rcp.watchers import AgentWatchSpec, run_watcher_cancel


def test_probe_storage_is_latest_per_project_and_machine(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    probe = ComputeBackendProbe(
        machine_id="local",
        backend_id="systemd_user",
        state="ready",
        ready=True,
        diagnostic="Ready",
        containment="cooperative",
        status_label="Ready",
        status_tone="ready",
    )
    assert store.compute_backend_probe("project", "local", "helper") is None
    store.record_compute_backend_probe("project", probe, "helper")
    failed = probe.model_copy(update={"ready": False, "state": "failed", "diagnostic": "Offline"})
    store.record_compute_backend_probe("project", failed, "helper")
    reopened = AppStore(store.path)
    assert reopened.compute_backend_probe("project", "local", "helper") == failed
    assert reopened.compute_backend_probe("other", "local", "helper") is None
    assert reopened.compute_backend_probe("project", "other", "helper") is None
    with reopened.connection() as connection:
        rows = connection.execute("SELECT probed_at FROM compute_backend_probes").fetchall()
    assert len(rows) == 1 and rows[0][0]


def test_cancel_process_is_bounded(tmp_path):
    marker = tmp_path / "late-side-effect"
    spec = AgentWatchSpec(
        purpose="job finishes",
        check_command="false",
        log_path=str(tmp_path / "log"),
        cwd=str(tmp_path),
        cancel_command=f"sleep 5; touch {shlex.quote(str(marker))}",
    )
    diagnostic = run_watcher_cancel(spec, timeout=0.01)
    assert diagnostic is not None
    assert not marker.exists()
