from __future__ import annotations

import threading

import pytest

from rcp.runs.provider_update import ProviderUpdateRunner
from rcp.server_ops.provider_update import ProviderUpdateRefused, ProviderVersionChange
from tests.helpers import create_named_app, signed_in_client, wait_until


@pytest.fixture
def app(manifest, tmp_path):
    return create_named_app(str(manifest.path), data_dir=tmp_path / "data")


def test_update_job_api_admission_and_result(app, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def update(store, launcher, provider, machine_id, *, target, stop, guard):
        with launcher.provider_update(provider, machine_id), guard():
            entered.set()
            assert release.wait(5)
            return ProviderVersionChange(before="1", after="2")

    monkeypatch.setattr("rcp.runs.provider_update.update_provider", update)
    monkeypatch.setattr(ProviderUpdateRunner, "_target", lambda self, machine: object())
    client = signed_in_client(app)
    path = "/api/space/machines/backend/providers/codex/update"
    assert client.get(path).status_code == 404
    try:
        response = client.post(path, json={})
        assert response.status_code == 202
        assert entered.is_set()
        assert response.json()["state"] == "running"
        assert set(response.json()) == {
            "provider",
            "machine_id",
            "state",
            "detail",
            "before",
            "after",
            "started_at",
            "finished_at",
        }
        assert client.post(path, json={}).status_code == 409
    finally:
        release.set()
    result = wait_until(
        lambda: job if (job := client.get(path).json())["state"] != "running" else None
    )
    assert result["state"] == "succeeded"
    assert (result["before"], result["after"]) == ("1", "2")
    assert result["finished_at"]


def test_update_job_refuses_busy_before_accepting(app, monkeypatch):
    def update(*args, **kwargs):
        raise ProviderUpdateRefused("2 turns are using Codex")

    monkeypatch.setattr("rcp.runs.provider_update.update_provider", update)
    monkeypatch.setattr(ProviderUpdateRunner, "_target", lambda self, machine: object())
    response = signed_in_client(app).post(
        "/api/space/machines/backend/providers/codex/update", json={}
    )
    assert response.status_code == 409
    assert "2" in response.json()["detail"]


def test_update_job_failure_is_pollable(app, monkeypatch):
    def update(*args, guard, **kwargs):
        with guard():
            raise OSError("installer failed")

    monkeypatch.setattr("rcp.runs.provider_update.update_provider", update)
    monkeypatch.setattr(ProviderUpdateRunner, "_target", lambda self, machine: object())
    client = signed_in_client(app)
    path = "/api/space/machines/backend/providers/claude/update"
    assert client.post(path, json={}).status_code == 202
    job = wait_until(lambda: job if (job := client.get(path).json())["state"] == "failed" else None)
    assert job["detail"]
    assert job["finished_at"]


def test_update_job_requires_machine_edit_identity(tmp_path):
    from tests.test_project_membership import _team_app

    _, client, _, _, acting = _team_app(tmp_path)
    acting[0] = "not-a-member"
    response = client.post("/api/space/machines/backend/providers/codex/update", json={})
    assert response.status_code == 403
    assert client.get("/api/space/machines/backend/providers/codex/update").status_code == 403


def test_daily_and_on_demand_jobs_share_the_update_hold(tmp_path, monkeypatch):
    from rcp.runs.provider_auto_update import auto_update_providers
    from rcp.server_ops.provider_update import LocalUpdateTarget
    from rcp.storage import AppStore
    from tests.test_provider_auto_update import _Backend

    backend = _Backend(tmp_path, monkeypatch)
    backend.layout.data_dir.mkdir(parents=True, exist_ok=True)
    store = AppStore(backend.layout.data_dir / "rcp.sqlite3")
    entered = threading.Event()
    release = threading.Event()

    def runner(account, argv, timeout):
        if argv[-1] == "upgrade":
            entered.set()
            assert release.wait(5)
        return backend.runner(account, argv, timeout)

    jobs = ProviderUpdateRunner(store, backend.launcher)
    monkeypatch.setattr(jobs, "_target", lambda machine: LocalUpdateTarget(backend.account, runner))
    try:
        jobs.start("opencode", "backend")
        assert entered.wait(5)
        auto_update_providers(
            store, backend.launcher, account=backend.account, runner=runner, stop=threading.Event()
        )
        assert backend.upgrades == 0
    finally:
        release.set()
        wait_until(
            lambda: (
                status
                if (status := jobs.status("opencode", "backend")) and status.state != "running"
                else None
            )
        )
        store.close()
    assert backend.upgrades == 1


def test_job_shutdown_drains_the_active_update(tmp_path, monkeypatch):
    from rcp.agents import AgentLauncher
    from rcp.storage import AppStore

    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    store = AppStore(tmp_path / "jobs.sqlite3")
    jobs = ProviderUpdateRunner(store, AgentLauncher())

    def update(*args, guard, **kwargs):
        with guard():
            entered.set()
            assert release.wait(5)
            return ProviderVersionChange(before="1", after="2")

    monkeypatch.setattr("rcp.runs.provider_update.update_provider", update)
    monkeypatch.setattr(jobs, "_target", lambda machine: object())
    jobs.start("codex", "backend")
    assert entered.wait(5)

    def stop():
        jobs.stop()
        stopped.set()

    worker = threading.Thread(target=stop)
    worker.start()
    try:
        assert not stopped.wait(0.05)
    finally:
        release.set()
        worker.join(5)
    assert stopped.is_set()
    with pytest.raises(ProviderUpdateRefused):
        jobs.start("codex", "backend")
    store.close()
