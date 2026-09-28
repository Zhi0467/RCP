from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rcp.compute_jobs.probe import _result
from rcp.config import load_manifest

from .helpers import create_named_app


@pytest.fixture
def compute_api(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    return app, TestClient(app), f"/api/projects/{app.state.default_project_id}"


def settings_body(snapshot):
    return {
        "default_run_truth_scope": snapshot["default_run_truth_scope"],
        "agent_profiles": {
            surface: {key: profile[key] for key in ("provider", "model", "reasoning", "run_on")}
            for surface, profile in snapshot["agent_profiles"].items()
        },
    }


def test_compute_settings_save_checks_every_route_of_the_saved_machine(compute_api, monkeypatch):
    from rcp.compute_jobs import probe as probe_module

    app, client, url = compute_api
    calls = []

    def run(manifest, machine, route, *, data_dir):
        calls.append((machine, route, data_dir))
        return _result(
            machine, {"helper": "systemd_user", "scheduler": "slurm"}[route], "ready", ""
        )

    monkeypatch.setattr(probe_module, "probe_compute_backend", run)
    monkeypatch.setattr(
        "rcp.api.project_state.refresh_compute_probes", probe_module.refresh_compute_probes
    )
    body = settings_body(client.get(url).json())
    for compute, routes in (
        ({"job_manager": "slurm"}, ["scheduler", "helper"]),
        (None, ["helper"]),
    ):
        calls.clear()
        response = client.put(
            f"{url}/settings", json={**body, "machine_compute": {"laptop": compute}}
        )
        assert response.status_code == 200, response.text
        assert calls == [("laptop", route, app.state.catalog.data_dir) for route in routes]
        probes = client.get(url).json()["machines"][0]["compute_probes"]
        assert [route for route, probe in probes.items() if probe] == sorted(
            routes, key=["scheduler", "helper"].index
        )
        # The heartbeat carries the probe time so an open page reloads the results.
        heartbeat = client.get(f"{url}/cached/revision").json()
        assert heartbeat["compute_probes_probed_at"] is not None
        assert (
            heartbeat["compute_probes_probed_at"]
            == client.get(url).json()["compute_probes_probed_at"]
        )


def test_compute_check_rechecks_one_machine_on_request(compute_api, monkeypatch):
    from rcp.compute_jobs import probe as probe_module

    app, client, url = compute_api
    calls = []

    def run(manifest, machine, route, *, data_dir):
        calls.append((machine, route))
        return _result(machine, "launchd", "ready", "")

    monkeypatch.setattr(probe_module, "probe_compute_backend", run)
    monkeypatch.setattr(
        "rcp.api.project_state.refresh_compute_probes", probe_module.refresh_compute_probes
    )
    response = client.post(f"{url}/machines/laptop/compute/check")
    assert response.status_code == 200, response.text
    assert calls == [("laptop", "helper")]
    assert response.json()["scheduler"] is None and response.json()["helper"]["ready"]
    assert client.post(f"{url}/machines/missing/compute/check").status_code == 422


def test_machine_compute_settings_write_invalidate_and_preserve_omitted(compute_api, manifest):
    app, client, url = compute_api
    store = app.state.services.store
    project_id = app.state.default_project_id
    body = settings_body(client.get(url).json())
    probe = _result("laptop", "systemd_user", "ready", "Passed.")
    scheduler = _result("laptop", "slurm", "ready", "Passed.")
    store.record_compute_backend_probe(project_id, probe, "helper")
    store.record_compute_backend_probe(project_id, scheduler, "scheduler")
    compute = {
        "job_manager": "slurm",
        "jobs_root": "/shared/jobs",
    }
    response = client.put(f"{url}/settings", json={**body, "machine_compute": {"laptop": compute}})
    assert response.status_code == 200, response.text
    assert response.json()["machines"][0]["compute"] == compute
    assert response.json()["machines"][0]["compute_probes"] == {"helper": None, "scheduler": None}
    assert load_manifest(manifest.path).machine_map["laptop"].compute.model_dump() == compute
    assert store.compute_backend_probe(project_id, "laptop", "helper") is None
    assert store.compute_backend_probe(project_id, "laptop", "scheduler") is None
    store.record_compute_backend_probe(project_id, probe, "helper")
    store.record_compute_backend_probe(project_id, scheduler, "scheduler")
    for extra in ({}, {"machine_compute": {"laptop": compute}}):
        assert client.put(f"{url}/settings", json={**body, **extra}).status_code == 200
        assert store.compute_backend_probe(project_id, "laptop", "helper") == probe
        assert store.compute_backend_probe(project_id, "laptop", "scheduler") == scheduler
    response = client.put(f"{url}/settings", json={**body, "machine_compute": {"laptop": None}})
    assert response.status_code == 200
    assert load_manifest(manifest.path).machine_map["laptop"].compute is None
    assert store.compute_backend_probe(project_id, "laptop", "helper") is None
    assert store.compute_backend_probe(project_id, "laptop", "scheduler") is None


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
