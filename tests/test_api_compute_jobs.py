from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rcp.compute_jobs.probe import _result
from rcp.config import load_manifest

from .helpers import create_named_app


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
    probe = _result("laptop", "systemd_user", "ready", "Passed.")
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
    probe = _result("laptop", "systemd_user", "ready", "Passed.")
    store.record_compute_backend_probe(project_id, probe)
    compute = {
        "job_manager": "slurm",
        "jobs_root": "/shared/jobs",
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


@pytest.mark.parametrize("unknown_alias", [False, True])
def test_settings_publish_agent_and_machine_compute_in_one_write(
    compute_api, manifest, monkeypatch, unknown_alias
):
    import rcp.config as config

    _, client, url = compute_api
    with manifest.path.open("a") as stream:
        stream.write('\n[[machines]]\nalias = "cluster"\nhost = "cluster.example"\n')
    body = settings_body(client.get(url).json())
    body["default_run_truth_scope"] = ["repo-a", "repo-b"]
    updates = {"laptop": {"jobs_root": "/local/jobs"}, "cluster": {"job_manager": "slurm"}}
    if unknown_alias:
        updates["missing"] = {"job_manager": "slurm"}
    before = manifest.path.read_bytes()
    writes = []
    atomic_write = config._atomic_write

    def record_write(path, content):
        writes.append(path)
        atomic_write(path, content)

    monkeypatch.setattr(config, "_atomic_write", record_write)
    response = client.put(f"{url}/settings", json={**body, "machine_compute": updates})
    if unknown_alias:
        assert response.status_code == 422, response.text
        assert "unknown machine" in response.text
        assert writes == []
        assert manifest.path.read_bytes() == before
    else:
        assert response.status_code == 200, response.text
        assert writes == [manifest.path]
        updated = load_manifest(manifest.path)
        assert updated.agent.default_run_truth_scope == ["repo-a", "repo-b"]
        assert updated.machine_map["laptop"].compute.jobs_root == "/local/jobs"
        assert updated.machine_map["cluster"].compute.job_manager == "slurm"


@pytest.mark.parametrize(
    "compute",
    [{"job_manager": "subprocess"}, {"jobs_root": "relative"}, {"slurm_account": "account"}],
)
def test_invalid_machine_compute_settings_do_not_write(compute_api, manifest, compute):
    _, client, url = compute_api
    body = settings_body(client.get(url).json())
    before = manifest.path.read_text()
    response = client.put(f"{url}/settings", json={**body, "machine_compute": {"laptop": compute}})
    assert response.status_code == 422
    assert manifest.path.read_text() == before
