from __future__ import annotations

import logging
import socket
import threading
from unittest.mock import Mock

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.background import StartupEffectFence
from rcp.machine_power import MachinePowerController
from rcp.storage import AppStore
from tests.helpers import wait_until


def test_personal_power_status_and_preferences(tmp_path):
    app = create_app(data_dir=tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/machine-power")
        assert response.status_code == 200
        status = response.json()
        assert set(status) == {"supported", "idle_hold", "demand_reasons"}
        assert status["supported"] is False
        assert status["idle_hold"] == {"enabled": True, "active": False}
        updated = client.put("/api/machine-power", json={"idle_hold": False})
        assert updated.status_code == 200
        assert updated.json()["idle_hold"] == {"enabled": False, "active": False}
        assert client.get("/api/machine-power").json() == updated.json()
        assert client.put("/api/machine-power", json={"idle_hold": "yes"}).status_code == 422


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/machine-power"),
        ("put", "/api/machine-power"),
    ],
)
def test_team_power_endpoints_are_absent(tmp_path, method, path):
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team")
    member, _ = store.enroll_team_member(bootstrap, "Member")
    app = create_app(
        data_dir=tmp_path,
        trusted_principal_resolver=lambda _request, current: current.space_user(member.user_id),
    )
    with TestClient(app) as client:
        response = client.request(method, path, json={})
        assert response.status_code == 404
    assert app.state.machine_power is None


@pytest.fixture
def mac_power(monkeypatch):
    def spawn(argv):
        raise AssertionError(f"unexpected process: {argv}")

    def factory(store, **kwargs):
        return MachinePowerController(
            store, demand_reader=lambda: [], platform="darwin", spawn=spawn
        )

    monkeypatch.setattr("rcp.api.app.MachinePowerController", factory)


def test_served_macos_power_preferences(tmp_path, mac_power, caplog):
    caplog.set_level(logging.INFO)
    app = create_app(data_dir=tmp_path / "data")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert port != 8421
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
        worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        worker.start()
        try:
            wait_until(lambda: server.started, timeout=10)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                before = client.get("/api/machine-power")
                assert before.status_code == 200
                assert before.json()["supported"] is True
                changed = client.put("/api/machine-power", json={"idle_hold": False})
                assert changed.status_code == 200
                assert changed.json()["idle_hold"] == {"enabled": False, "active": False}
                assert client.get("/api/machine-power").json() == changed.json()
        finally:
            server.should_exit = True
            worker.join(timeout=10)
        assert not worker.is_alive()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_power_follows_deferred_startup_and_outlasts_worker_shutdown(tmp_path, monkeypatch):
    controller = Mock()
    monkeypatch.setattr("rcp.api.app.MachinePowerController", lambda *a, **kw: controller)
    fence = StartupEffectFence("keep-awake startup ordering")
    app = create_app(data_dir=tmp_path, startup_effect_fence=fence)
    order = []
    controller.start.side_effect = lambda: order.append("start")
    controller.stop.side_effect = lambda: order.append("off")
    original_shutdown = app.state.background_tasks.shutdown

    def shutdown(*args, **kwargs):
        order.append("workers")
        return original_shutdown(*args, **kwargs)

    monkeypatch.setattr(app.state.background_tasks, "shutdown", shutdown)
    with TestClient(app):
        assert order == []
        fence.release()
        assert app.state.startup_effect_runtime_event.wait(timeout=5)
        assert order == ["start"]
    assert order == ["start", "workers", "off"]


def test_failed_startup_stops_power(tmp_path, monkeypatch):
    controller = Mock()
    monkeypatch.setattr("rcp.api.app.MachinePowerController", lambda *a, **kw: controller)
    app = create_app(data_dir=tmp_path)
    monkeypatch.setattr(
        app.state.background_tasks, "recover_at_startup", Mock(side_effect=RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError, match="boom"), TestClient(app):
        pass
    controller.start.assert_called_once()
    controller.stop.assert_called_once()
