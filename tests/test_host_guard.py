from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rcp.api.host_guard import HostGuard, HostGuardConfig


def _app(*, team_access_url: str | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(HostGuard, port=8421, team_access_url=team_access_url)

    @app.get("/api/health")
    @app.get("/api/private")
    def healthy() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.mark.parametrize("path", ["/api/health", "/api/private"])
@pytest.mark.parametrize(
    ("host", "accepted"),
    [
        ("127.0.0.1:8421", True),
        ("localhost:8421", True),
        ("[::1]:8421", True),
        ("rcp-" + "a" * 32 + ".rcp.localhost:19421", True),
        ("rebound.example:8421", False),
        ("localhost:8422", False),
        ("localhost", False),
        ("localhost:8421.evil.example", False),
        ("rcp-" + "a" * 32 + ".rcp.localhost:65536", False),
        ("rcp-" + "a" * 31 + ".rcp.localhost:19421", False),
        ("127.0.0.1:8421@evil.example", False),
    ],
)
def test_request_authority_is_checked_even_for_health(path, host, accepted) -> None:
    response = TestClient(_app()).get(
        path, headers={"host": host, "x-forwarded-host": "localhost:8421"}
    )
    assert response.status_code == (200 if accepted else 421)
    if accepted:
        assert response.json() == {"status": "ok"}
    else:
        assert response.json()["detail"]["code"] == "host_invalid"


@pytest.mark.parametrize(
    ("host", "accepted"),
    [("team.example", True), ("team.example:443", True), ("other.example", False)],
)
def test_team_authority_comes_from_operator_origin(host, accepted) -> None:
    response = TestClient(_app(team_access_url="https://team.example")).get(
        "/api/health", headers={"host": host}
    )
    assert response.status_code == (200 if accepted else 421)


@pytest.mark.parametrize("hosts", [[], [(b"host", b"localhost:8421")] * 2])
def test_missing_or_ambiguous_host_is_refused(hosts) -> None:
    client = TestClient(_app())
    request = client.build_request("GET", "/api/health")
    request.headers.clear()
    request.headers.update(hosts)
    response = client.send(request)
    assert response.status_code == 421


def test_rebound_websocket_cannot_bypass_host_guard() -> None:
    with (
        TestClient(_app()) as client,
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/socket", headers={"host": "rebound.example:8421"}),
    ):
        pass
    assert refused.value.code == 1008


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1:8421", 200),
        ("rcp-" + "a" * 32 + ".rcp.localhost:19421", 200),
        ("rebound.example:8421", 421),
    ],
)
def test_guard_is_wired_before_public_health(tmp_path, host, expected):
    from rcp.api.app import create_app

    app = create_app(data_dir=tmp_path, request_host_guard=HostGuardConfig(port=8421))
    with TestClient(app, base_url=f"http://{host}") as client:
        response = client.get("/api/health")
        assert response.status_code == expected
        if expected == 421:
            assert response.json()["detail"]["code"] == "host_invalid"


def test_serve_passes_bound_host_guard(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from rcp import __main__ as cli
    from rcp.server_runtime import ServerMetadata

    captured = {}
    monkeypatch.setattr(cli, "prepared_web_assets", lambda **kwargs: nullcontext())
    monkeypatch.setattr(cli, "team_access_url", lambda: "https://team.example")
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    args = cli.build_parser().parse_args(["serve", "--port", "9345"])
    metadata = ServerMetadata.create(tmp_path, host=args.host, port=args.port, owner_kind="cli")
    cli._run_server(args, metadata)
    assert captured["request_host_guard"] == HostGuardConfig(
        port=9345, team_access_url="https://team.example"
    )


def test_plain_app_has_no_listener_host_policy(tmp_path):
    from rcp.api.app import create_app

    with TestClient(create_app(data_dir=tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
