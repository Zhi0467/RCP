from __future__ import annotations

import logging
import socket
import subprocess
import threading
from unittest.mock import Mock

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.background import StartupEffectFence
from rcp.machine_power import MachinePowerController
from rcp.machine_power_install import InstallStatus
from rcp.storage import AppStore
from tests.helpers import wait_until


def test_personal_power_status_and_preferences(tmp_path):
    app = create_app(data_dir=tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/machine-power")
        assert response.status_code == 200
        status = response.json()
        assert set(status) == {
            "platform",
            "supported",
            "installed",
            "install_problem",
            "idle_hold",
            "lid_mode",
            "demand",
            "demand_reasons",
            "latched",
            "last_release",
            "cleanup_failure",
            "external_owner",
        }
        assert status["platform"] == "linux"
        assert status["supported"] is False
        assert status["idle_hold"] == {"enabled": True, "active": False}
        assert status["lid_mode"] == {"enabled": False, "active": False}
        updated = client.put("/api/machine-power", json={"idle_hold": False})
        assert updated.status_code == 200
        assert updated.json()["idle_hold"] == {"enabled": False, "active": False}
        assert updated.json().keys() == status.keys()
        assert client.get("/api/machine-power").json() == updated.json()
        assert client.put("/api/machine-power", json={"lid_mode": "yes"}).status_code == 422


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/machine-power"),
        ("put", "/api/machine-power"),
        ("post", "/api/machine-power/install"),
        ("post", "/api/machine-power/uninstall"),
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
def mac_power(tmp_path, monkeypatch):
    installer = Mock()
    installer.status.return_value = InstallStatus(False, "not_installed")
    installer.cancelled = True
    installer.install.return_value = installer.status.return_value
    installer.uninstall.return_value = installer.status.return_value
    outputs = {
        ("/usr/bin/pmset", "-g", "batt"): "Now drawing from 'AC Power'\n",
        ("/usr/bin/pmset", "-g", "therm"): (
            "No thermal warning level has been recorded\n"
            "No performance warning level has been recorded\n"
        ),
        ("/usr/sbin/ioreg", "-r", "-k", "AppleClamshellState"): '"AppleClamshellState" = No',
        ("/usr/bin/pmset", "-g"): "SleepDisabled 0\n",
    }

    def run(argv, timeout):
        assert timeout > 0
        return subprocess.CompletedProcess(argv, 0, outputs[tuple(argv)], "")

    def spawn(argv):
        raise AssertionError(f"unexpected process: {argv}")

    def factory(store, **kwargs):
        return MachinePowerController(
            store,
            demand_reader=lambda: [],
            platform="darwin",
            installer=installer,
            directory=tmp_path / "machine",
            run=run,
            spawn=spawn,
            clock=lambda: 1000,
            process_identity=lambda: (123, "test start"),
        )

    monkeypatch.setattr("rcp.api.app.MachinePowerController", factory)
    return installer


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_cancelled_admin_prompt_preserves_status(tmp_path, mac_power, action):
    app = create_app(data_dir=tmp_path / "data")
    with TestClient(app, raise_server_exceptions=True) as client:
        assert client.put("/api/machine-power", json={"lid_mode": True}).status_code == 200
        before = client.get("/api/machine-power").json()
        # A cross-site HTML form cannot send JSON, so it never reaches the prompt.
        form = client.post("/api/machine-power/" + action, data={"x": "1"})
        assert form.status_code == 415
        getattr(mac_power, action).assert_not_called()
        response = client.post("/api/machine-power/" + action, json={})
        assert response.status_code == 200
        assert response.json() == before
        getattr(mac_power, action).assert_called_once()


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
                assert before.json()["platform"] == "macos"
                assert before.json()["supported"] is True
                changed = client.put("/api/machine-power", json={"idle_hold": False})
                assert changed.status_code == 200
                assert changed.json()["idle_hold"] == {"enabled": False, "active": False}
                assert changed.json()["lid_mode"] == {"enabled": False, "active": False}
                assert client.get("/api/machine-power").json() == changed.json()
        finally:
            server.should_exit = True
            worker.join(timeout=10)
        assert not worker.is_alive()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_power_follows_deferred_startup_and_precedes_worker_shutdown(tmp_path, monkeypatch):
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
    assert order == ["start", "off", "workers"]
