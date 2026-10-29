from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from rcp import __version__, limits
from rcp.api.client_events import ClientEventLimiter

from .helpers import create_named_app, signed_in_client

URL = "/api/client-events"


def _client_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "rcp.client"]


def test_client_event_logs_one_bounded_warning_line(manifest, tmp_path, caplog) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    owner = app.state.background_tasks.store.local_owner
    caplog.set_level(logging.WARNING, logger="rcp.client")

    response = client.post(
        URL, json={"kind": "notice", "message": "Sync failed: conflict", "view": "dag"}
    )
    assert response.status_code == 204
    [record] = _client_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.args == ("notice", "dag", __version__, owner.user_id, "Sync failed: conflict")

    caplog.clear()
    oversized = "line one\nforged second line\x1b[31m" + "x" * (
        limits.CLIENT_EVENT_MESSAGE_MAX_CHARS * 10
    )
    assert client.post(URL, json={"kind": "window_error", "message": oversized}).status_code == 204
    [record] = _client_records(caplog)
    message = record.args[-1]
    assert len(message) == limits.CLIENT_EVENT_MESSAGE_MAX_CHARS
    assert message.startswith("line one forged second line ")
    assert "\n" not in record.getMessage() and "\x1b" not in record.getMessage()

    caplog.clear()
    for refused in (
        {"kind": "notice", "message": "m", "view": "#/projects/p?owner-code=secret"},
        {"kind": "notice", "message": "m", "view": "v" * (limits.CLIENT_EVENT_VIEW_MAX_CHARS + 1)},
        {"kind": "toast", "message": "m"},
        {"kind": "notice", "message": "m", "body": "request body"},
    ):
        assert client.post(URL, json=refused).status_code == 422
    assert _client_records(caplog) == []


def test_client_events_are_rate_limited_per_member(manifest, tmp_path, caplog) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    now = [0.0]
    app.state.client_event_limiter = ClientEventLimiter(clock=lambda: now[0])
    caplog.set_level(logging.WARNING, logger="rcp.client")
    event = {"kind": "notice", "message": "loop"}

    statuses = [
        client.post(URL, json=event).status_code
        for _ in range(limits.CLIENT_EVENT_RATE_MAX_EVENTS + 5)
    ]
    assert statuses == [204] * limits.CLIENT_EVENT_RATE_MAX_EVENTS + [429] * 5
    records = _client_records(caplog)
    # The admitted burst, then one line saying the rest of the window is dropped.
    assert len(records) == limits.CLIENT_EVENT_RATE_MAX_EVENTS + 1
    owner = app.state.background_tasks.store.local_owner
    assert records[-1].args == (
        owner.user_id,
        limits.CLIENT_EVENT_RATE_MAX_EVENTS,
        limits.CLIENT_EVENT_RATE_WINDOW_SECONDS,
    )

    now[0] += limits.CLIENT_EVENT_RATE_WINDOW_SECONDS
    assert client.post(URL, json=event).status_code == 204


def test_client_events_require_a_session(manifest, tmp_path, caplog) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    caplog.set_level(logging.WARNING, logger="rcp.client")
    with TestClient(app) as client:
        response = client.post(URL, json={"kind": "notice", "message": "anonymous"})
    assert response.status_code == 401
    assert _client_records(caplog) == []
