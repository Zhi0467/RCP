from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rcp.compute_jobs import jobs
from rcp.compute_jobs.probe import _result
from rcp.config import load_manifest

from .helpers import create_named_app
from .test_compute_jobs_storage import job_record


@pytest.fixture
def compute_api(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data", compute_ready=False)
    return app, TestClient(app), f"/api/projects/{app.state.default_project_id}"


def settings_body(snapshot):
    return {
        "default_run_truth_scope": snapshot["default_run_truth_scope"],
        "agent_profiles": {
            surface: {key: profile[key] for key in ("provider", "model", "reasoning", "run_on")}
            for surface, profile in snapshot["agent_profiles"].items()
        },
    }


def test_compute_probe_route_stores_and_updates_cached_project(compute_api, monkeypatch):
    app, client, url = compute_api
    before = client.get(url).json()
    assert before["machines"][0]["compute_probe"] is None
    probe = _result("laptop", "launchd", "ready", "Passed.")
    calls = []

    def run(manifest, machine, *, data_dir):
        calls.append((machine, data_dir))
        return probe

    monkeypatch.setattr("rcp.api.project_state.probe_compute_backend", run)
    response = client.post(f"{url}/machines/laptop/compute/probe")
    assert response.status_code == 200
    assert response.json() == probe.model_dump(mode="json")
    assert calls == [("laptop", app.state.catalog.data_dir)]
    assert (
        app.state.services.store.compute_backend_probe(app.state.default_project_id, "laptop")
        == probe
    )
    for suffix in ("", "/cached"):
        assert client.get(url + suffix).json()["machines"][0]["compute_probe"] == response.json()
    assert client.post(f"{url}/machines/missing/compute/probe").status_code == 422
    assert len(calls) == 1


def test_machine_compute_settings_write_invalidate_and_preserve_omitted(compute_api, manifest):
    app, client, url = compute_api
    store = app.state.services.store
    project_id = app.state.default_project_id
    body = settings_body(client.get(url).json())
    probe = _result("laptop", "launchd", "ready", "Passed.")
    store.record_compute_backend_probe(project_id, probe)
    compute = {
        "backend": "slurm",
        "jobs_root": "/shared/jobs",
        "slurm_account": "research",
        "slurm_partition": "batch",
        "slurm_submit_args": ["--qos=normal"],
    }
    response = client.put(f"{url}/settings", json={**body, "machine_compute": {"laptop": compute}})
    assert response.status_code == 200, response.text
    assert response.json()["machines"][0]["compute"] == compute
    assert response.json()["machines"][0]["compute_probe"] is None
    assert load_manifest(manifest.path).machine_map["laptop"].compute.model_dump() == compute
    assert store.compute_backend_probe(project_id, "laptop") is None
    store.record_compute_backend_probe(project_id, probe)
    for extra in ({}, {"machine_compute": {"laptop": compute}}):
        assert client.put(f"{url}/settings", json={**body, **extra}).status_code == 200
        assert store.compute_backend_probe(project_id, "laptop") == probe
    response = client.put(f"{url}/settings", json={**body, "machine_compute": {"laptop": None}})
    assert response.status_code == 200
    assert load_manifest(manifest.path).machine_map["laptop"].compute is None
    assert store.compute_backend_probe(project_id, "laptop") is None


@pytest.mark.parametrize(
    "compute", [{"backend": "subprocess"}, {"jobs_root": "relative"}, {"slurm_account": "account"}]
)
def test_invalid_machine_compute_settings_do_not_write(compute_api, manifest, compute):
    _, client, url = compute_api
    body = settings_body(client.get(url).json())
    before = manifest.path.read_text()
    response = client.put(f"{url}/settings", json={**body, "machine_compute": {"laptop": compute}})
    assert response.status_code == 422
    assert manifest.path.read_text() == before


def test_compute_job_list_refreshes_running_project_rows_and_orders(compute_api, monkeypatch):
    app, client, url = compute_api
    store = app.state.services.store
    project_id = app.state.default_project_id
    store.create_compute_job(job_record("older", project_id=project_id, label="Training"))
    store.create_compute_job(
        job_record(
            "newer", project_id=project_id, status="exited", created_at="2026-09-06T00:00:01Z"
        )
    )
    store.create_compute_job(job_record("other", project_id="another-project"))
    store.create_compute_job(
        job_record("live", project_id=project_id, created_at="2026-09-06T00:00:02Z")
    )
    calls = []

    def refresh(store, manifest, job_id, *, data_dir, **_reconcile_kwargs):
        calls.append(job_id)
        if job_id == "live":
            return store.compute_job(job_id)
        return store.record_compute_job_refresh(
            job_id, status="exited", exit_status=0, ended_at=store.now()
        )

    monkeypatch.setattr("rcp.compute_jobs.reconcile.refresh_compute_job", refresh)
    response = client.get(f"{url}/compute-jobs")
    assert response.status_code == 200
    assert sorted(calls) == ["live", "older"]
    assert [row["job_id"] for row in response.json()] == ["live", "newer", "older"]
    # The control decision is backend-owned: only the running row may be cancelled.
    assert [row["can_cancel"] for row in response.json()] == [True, False, False]
    assert response.json()[2]["status"] == "exited"
    assert response.json()[2]["exit_status"] == 0
    assert response.json()[2]["label"] == "Training"


def test_human_compute_cancel_is_attributed_idempotent_and_project_scoped(compute_api, monkeypatch):
    app, client, url = compute_api
    store = app.state.services.store
    store.create_compute_job(
        job_record(project_id=app.state.default_project_id, execution_machine="laptop")
    )
    store.create_compute_job(job_record("other", project_id="another-project"))
    cancellations = []

    class Backend:
        def cancel(self, handle, context):
            cancellations.append(handle)

        def alive(self, handle, context):
            return False

    monkeypatch.setitem(jobs.COMPUTE_BACKENDS, "systemd_user", Backend())
    monkeypatch.setattr(jobs, "read_job_file", lambda *args: None)
    first = client.post(f"{url}/compute-jobs/job-1/cancel")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "cancelled"
    assert first.json()["can_cancel"] is False
    assert first.json()["cancel_requested_by"] == store.local_owner.user_id
    assert first.json()["cancel_requested_at"]
    assert client.post(f"{url}/compute-jobs/job-1/cancel").json() == first.json()
    assert cancellations == ["rcp-job-job-1"]
    for job_id in ("other", "missing"):
        assert client.post(f"{url}/compute-jobs/{job_id}/cancel").status_code == 404

    def refuse(project_id):
        raise ValueError("Project is being removed.")

    monkeypatch.setattr(store, "require_project_accepts_new_work", refuse)
    assert client.post(f"{url}/compute-jobs/job-1/cancel").status_code == 409
    assert cancellations == ["rcp-job-job-1"]
