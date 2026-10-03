from __future__ import annotations

import logging
import socket
import threading

import httpx
import uvicorn

from .helpers import TASK_SETTLE_TIMEOUT, create_named_app, wait_until
from .test_consolidation_storage_api import _failure


def test_served_consolidation_schedule_inbox_notification_and_shutdown(manifest, tmp_path, caplog):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.services.store
    project = app.state.default_project_id
    run = _failure(store, project)
    store.delete_consolidation_schedule(project)
    device = store.register_notification_device(store.local_owner.user_id)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert port != 8421
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
        worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        worker.start()
        try:
            wait_until(lambda: server.started)
            wait_until(lambda: app.state.startup_effect_runtime_event.is_set())
            assert app.state.consolidation_poller.is_running()
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                base = f"/api/projects/{project}/consolidation"
                before = client.get(base)
                assert before.status_code == 200
                assert before.json()["inbox"][0]["run_id"] == run.run_id
                enabled = client.put(
                    base + "/schedule", json={"local_time": "02:30", "timezone": "UTC"}
                )
                assert enabled.status_code == 200
                assert enabled.json()["schedule"]["expired"] is False
                notices = client.get(f"/api/notifications/devices/{device['device_id']}/pending")
                assert notices.status_code == 200
                assert notices.json()[0]["reason"] == "consolidation"
                assert client.post(base + f"/runs/{run.run_id}/keep", json={}).status_code == 409
                dismissed = client.post(base + f"/runs/{run.run_id}/dismiss", json={})
                assert dismissed.status_code == 200
                assert dismissed.json()["item"]["state"] == "dismissed"
                assert client.get(base).json()["inbox"] == []
                assert client.delete(base + "/schedule").json() == {"schedule": None}
        finally:
            server.should_exit = True
            worker.join(timeout=TASK_SETTLE_TIMEOUT)
        assert not worker.is_alive()
    assert not app.state.consolidation_poller.is_running()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
