from __future__ import annotations

import pytest

from rcp.compute_jobs.probe import _result
from rcp.config import load_manifest
from tests.helpers import resource_watermark_tokens, signed_in_client

from .helpers import create_named_app


@pytest.fixture
def compute_api(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    return app, signed_in_client(app), f"/api/projects/{app.state.default_project_id}"


def settings_body(snapshot):
    return {
        "default_run_truth_scope": snapshot["default_run_truth_scope"],
        "agent_profiles": {
            surface: {key: profile[key] for key in ("provider", "model", "reasoning", "run_on")}
            for surface, profile in snapshot["agent_profiles"].items()
        },
    }


def _laptop_card(client):
    return next(
        card for card in client.get("/api/space/machines").json()["machines"] if not card["host"]
    )


def test_card_compute_change_rechecks_the_routes_of_every_project_on_it(compute_api, monkeypatch):
    from rcp.compute_jobs import probe as probe_module

    app, client, url = compute_api
    store = app.state.services.store
    project_id = app.state.default_project_id
    calls = []

    def run(manifest, machine, route, *, data_dir, compute):
        calls.append((machine, route, compute.job_manager if compute else None))
        return _result(
            machine, {"helper": "systemd_user", "scheduler": "slurm"}[route], "ready", ""
        )

    monkeypatch.setattr(probe_module, "probe_compute_backend", run)
    card = _laptop_card(client)
    for job_manager, routes in (("slurm", ["scheduler", "helper"]), ("none", ["helper"])):
        calls.clear()
        before = resource_watermark_tokens(client, project_id, {"name": "settings"})
        response = client.patch(
            f"/api/space/machines/{card['machine_id']}", json={"job_manager": job_manager}
        )
        assert response.status_code == 200, response.text
        assert calls == [
            ("laptop", route, "slurm" if job_manager == "slurm" else None) for route in routes
        ]
        # The returned card already carries the new checks.
        assert [route for route, probe in response.json()["compute_probes"].items() if probe] == (
            sorted(routes, key=["scheduler", "helper"].index)
        )
        project = client.get(url).json()
        probes = project["machines"][0]["compute_probes"]
        assert [route for route, probe in probes.items() if probe] == sorted(
            routes, key=["scheduler", "helper"].index
        )
        assert (project["machines"][0]["compute"] or {}).get("job_manager") == (
            "slurm" if job_manager == "slurm" else None
        )
        assert resource_watermark_tokens(client, project_id, {"name": "settings"}) != before
    # A setting that changes no route keeps the recorded checks.
    calls.clear()
    hinted = client.patch(
        f"/api/space/machines/{card['machine_id']}", json={"access_hint": "Use /shared"}
    )
    assert hinted.status_code == 200 and hinted.json()["access_hint"] == "Use /shared"
    assert calls == []
    assert store.compute_backend_probe(project_id, "laptop", "helper") is not None


@pytest.mark.parametrize("found", [True, False, None])
def test_an_undecided_card_turns_slurm_on_only_when_the_machine_has_it(
    compute_api, monkeypatch, found
):
    from rcp.compute_jobs import probe as probe_module

    app, client, _ = compute_api
    store = app.state.services.store
    monkeypatch.setattr(probe_module, "detect_slurm", lambda host: found)
    monkeypatch.setattr(
        probe_module,
        "probe_compute_backend",
        lambda manifest, machine, route, **_: _result(machine, "launchd", "ready", ""),
    )
    monkeypatch.setattr("rcp.api.space_machines.probe_machine_reach", lambda *_a, **_k: [])
    assert store.space_machine_for("").job_manager is None
    probe_module.refresh_compute_probes(
        store,
        load_manifest(store.project(app.state.default_project_id).locator),
        app.state.default_project_id,
        data_dir=app.state.catalog.data_dir,
    )
    expected = {True: "slurm", False: "none"}.get(found)
    assert store.space_machine_for("").job_manager == expected
    # A card no project uses yet settles the same way when it is checked.
    unused = store.create_space_machine(name="spare", host="spare.example", os_account="")
    checked = client.post(f"/api/space/machines/{unused.machine_id}/compute/check", json={})
    assert checked.status_code == 200, checked.text
    assert checked.json()["job_manager"] == expected


def test_compute_check_rechecks_one_machine_on_request(compute_api, monkeypatch):
    from rcp.compute_jobs import probe as probe_module

    app, client, url = compute_api
    calls = []

    def run(manifest, machine, route, *, data_dir, compute):
        calls.append((machine, route))
        return _result(machine, "launchd", "ready", "")

    monkeypatch.setattr(probe_module, "probe_compute_backend", run)
    monkeypatch.setattr(
        "rcp.api.project_state.refresh_compute_probes", probe_module.refresh_compute_probes
    )
    before = resource_watermark_tokens(client, app.state.default_project_id, {"name": "settings"})
    response = client.post(f"{url}/machines/laptop/compute/check", json={})
    assert (
        resource_watermark_tokens(client, app.state.default_project_id, {"name": "settings"})
        != before
    )
    assert response.status_code == 200, response.text
    assert calls == [("laptop", "helper")]
    assert response.json()["scheduler"] is None and response.json()["helper"]["ready"]
    assert client.post(f"{url}/machines/missing/compute/check", json={}).status_code == 422


def test_project_machine_browser_reads_that_machines_account(compute_api, manifest, monkeypatch):
    from rcp.browser import BrowserReadiness

    app, client, url = compute_api
    laptop = load_manifest(manifest.path).machine_map["laptop"]
    calls = []

    def readiness(*, host, os_account, data_dir):
        calls.append((host, os_account, data_dir))
        return BrowserReadiness(status="not_installed")

    monkeypatch.setattr("rcp.api.project_state.browser_readiness", readiness)
    response = client.get(f"{url}/machines/laptop/browser")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "not_installed"
    assert calls == [(laptop.host, laptop.os_account, app.state.catalog.data_dir)]
    assert client.get(f"{url}/machines/missing/browser").status_code == 422


@pytest.mark.parametrize(
    "compute",
    [{"job_manager": "subprocess"}, {"jobs_root": "relative"}, {"slurm_account": "account"}],
)
def test_invalid_machine_compute_settings_do_not_write(compute_api, compute):
    _, client, _ = compute_api
    card = _laptop_card(client)
    response = client.patch(f"/api/space/machines/{card['machine_id']}", json=compute)
    assert response.status_code == 422
    after = _laptop_card(client)
    assert (after["job_manager"], after["jobs_root"]) == (card["job_manager"], card["jobs_root"])
