from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from rcp.compute_jobs.models import ComputeBackendProbe

from .helpers import append_fixture_patch, create_named_app, seed_patch
from .test_api import _experiment_fixture_patch


def _probe(*, ready: bool) -> ComputeBackendProbe:
    return ComputeBackendProbe(
        execution_machine="laptop",
        backend_id="systemd_user",
        state="ready" if ready else "failed",
        ready=ready,
        diagnostic="Probe passed." if ready else "The user manager is unavailable.",
        required_action=None if ready else "Enable linger and probe again.",
        containment="cooperative",
        status_label="Ready" if ready else "Failed",
        status_tone="ready" if ready else "error",
    )


@pytest.mark.parametrize("episode_kind", ["experiment", "auto_research"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("stored", [False, True])
def test_episode_start_requires_resolved_machine_probe(
    manifest, tmp_path, monkeypatch, episode_kind, ready, stored
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data", compute_ready=False)
    project_id = app.state.default_project_id
    service = app.state.service
    tasks = app.state.background_tasks
    store = tasks.store
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_fixture_patch())
    expected = _probe(ready=ready)
    calls = []

    def probe(current_manifest, machine_alias, *, data_dir):
        calls.append((current_manifest.path, machine_alias, data_dir))
        return expected

    monkeypatch.setattr("rcp.compute_jobs.admission.probe_compute_backend", probe)
    if stored:
        store.record_compute_backend_probe(project_id, expected)
    monkeypatch.setattr(tasks, "_spawn_record", lambda record, _request, **_kwargs: record)
    if episode_kind == "experiment":
        url = f"/api/projects/{project_id}/experiments/exp%2Fbounded-loop/run"
        body = {"chat_id": str(uuid.uuid4()), "run_on": "untrusted-client-machine"}
    else:
        url = f"/api/projects/{project_id}/episodes"
        body = {"mode": "auto_research", "invocation_ceiling": 2}
    response = TestClient(app).post(url, json=body)

    assert response.status_code == (202 if ready else 422), response.text
    assert calls == ([] if stored else [(manifest.path, "laptop", tmp_path / "data")])
    assert store.compute_backend_probe(project_id, "laptop") == expected
    if ready:
        assert len(store.episodes(project_id)) == 1
        assert len(store.agent_tasks(project_id)) == 1
    else:
        detail = response.json()["detail"]
        assert "laptop" in detail
        assert expected.diagnostic in detail
        assert expected.required_action in detail
        assert store.episodes(project_id) == []
        assert store.agent_tasks(project_id) == []
        assert not (manifest.research_dir / "branches").exists()


@pytest.mark.parametrize("stored", [False, True])
def test_ordinary_work_does_not_require_compute_probe(manifest, tmp_path, monkeypatch, stored):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data", compute_ready=False)
    project_id = app.state.default_project_id
    tasks = app.state.background_tasks
    if stored:
        tasks.store.record_compute_backend_probe(project_id, _probe(ready=False))

    def unexpected_probe(*_args, **_kwargs):
        pytest.fail("Ordinary Work must not probe compute during admission.")

    monkeypatch.setattr("rcp.compute_jobs.admission.probe_compute_backend", unexpected_probe)
    monkeypatch.setattr(tasks, "_spawn_record", lambda record, _request, **_kwargs: record)
    response = TestClient(app).post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={"chat_id": str(uuid.uuid4()), "message": "Inspect the code.", "mode": "work"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["request"]["patch_kind"] == "work"
    assert tasks.store.episodes(project_id) == []
