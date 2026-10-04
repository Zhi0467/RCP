from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rcp.api import app as app_module
from rcp.server_runtime import ServerMetadata
from tests.helpers import create_named_app, signed_in_client


@pytest.mark.parametrize("owner", ["startup", "watcher"])
def test_shutdown_drains_active_live_capture(manifest, tmp_path, monkeypatch, owner):
    # Drive the actual lifespan, with watcher callbacks controlled by this test.
    monkeypatch.setattr(app_module.WatcherPoller, "start", lambda self: None)
    monkeypatch.setattr(app_module.WatcherPoller, "stop", lambda self: None)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    armed = threading.Event()
    observed = threading.Event()
    if owner == "startup":
        armed.set()

    def capture(store, catalog, project_id):
        observed.set()
        if armed.is_set():
            entered.set()
            assert release.wait(10)
            finished.set()

    monkeypatch.setattr(app_module, "reconcile_artifact_live_snapshots", capture)
    data_dir = tmp_path / "data"
    metadata = ServerMetadata.create(data_dir, host="127.0.0.1", port=18423, owner_kind="embedded")
    app = create_named_app(str(manifest.path), data_dir=data_dir, instance_metadata=metadata)
    client = signed_in_client(app, base_url="http://testserver:18423")
    client.__enter__()
    with ThreadPoolExecutor(max_workers=2) as executor:
        poll = None
        if owner == "watcher":
            assert observed.wait(5)
            armed.set()
            poll = executor.submit(app.state.watcher_poller.on_poll_completed)
        assert entered.wait(5)
        shutdown = executor.submit(client.__exit__, None, None, None)
        try:
            assert not finished.wait(0.1)
            assert not shutdown.done()
        finally:
            release.set()
        shutdown.result(timeout=5)
        if poll is not None:
            poll.result(timeout=5)
    assert finished.is_set()
    # A callback arriving after drain cannot start another write.
    finished.clear()
    app.state.watcher_poller.on_poll_completed()
    assert not finished.is_set()


def test_closed_maintenance_gate_refuses_watcher_live_capture(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module.WatcherPoller, "start", lambda self: None)
    calls = []
    monkeypatch.setattr(
        app_module, "reconcile_artifact_live_snapshots", lambda *args: calls.append(args)
    )
    data_dir = tmp_path / "data"
    metadata = ServerMetadata.create(data_dir, host="127.0.0.1", port=18423, owner_kind="embedded")
    app = create_named_app(str(manifest.path), data_dir=data_dir, instance_metadata=metadata)
    app.state.background_admission_gate.close_and_wait(timeout=1)
    app.state.watcher_poller.on_poll_completed()
    assert not calls
