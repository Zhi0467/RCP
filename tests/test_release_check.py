from __future__ import annotations

import json
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn

from rcp import limits, release_check
from rcp.api.app import create_app
from rcp.release_check import ReleaseCheck
from rcp.storage import AppStore
from tests.helpers import signed_in_client, wait_until

COMMIT = "a" * 40


def stable(tag="v0.4.10", **changes):
    return dict(tag_name=tag, target_commitish=COMMIT, draft=False, prerelease=False) | changes


def companion(**changes):
    return (
        stable("desktop-v0.4.10", prerelease=True)
        | {
            "assets": [
                {"name": name, "state": "uploaded"} for name in ("RCP.zip", "RCP.zip.sha256")
            ]
        }
        | changes
    )


@pytest.fixture
def github(monkeypatch):
    routes = {"/latest": (200, stable()), "/tags/desktop-v0.4.10": (200, companion())}
    requests = []
    stall = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            code, payload = routes.get(self.path, (404, {}))
            if payload == "stall":
                stall.wait(1)
                return
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            if code == 302:
                self.send_header("Location", "http://127.0.0.1:1/forbidden")
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.delenv("RCP_UPDATE_CHECK")
    monkeypatch.setattr(release_check, "API_BASE", f"http://127.0.0.1:{server.server_port}")
    yield routes, requests
    stall.set()
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize(
    ("version", "status"),
    [
        ("0.4.9", "update_available"),
        ("0.4.10", "current"),
        ("0.5.0", "current"),
        ("0.4.10+build.19.gabcdef0", "current"),
        ("garbage", "unknown"),
    ],
)
def test_numeric_version_comparison(github, version, status):
    notice = ReleaseCheck("personal", version, source_checkout=True).check()
    assert notice.status == status
    assert notice.latest_version == "0.4.10"
    assert notice.current_version == (None if status == "unknown" else version.partition("+")[0])
    assert notice.update_command == (
        "scripts/update-from-source v0.4.10" if status == "update_available" else None
    )
    assert len(github[1]) == (2 if status == "update_available" else 1)


@pytest.mark.parametrize(
    "changes",
    [{"prerelease": True}, {"draft": True}, {"tag_name": "vbad"}, {"target_commitish": "main"}],
)
def test_latest_requires_supervisor_stable_identity(github, changes):
    github[0]["/latest"] = (200, stable(**changes))
    expected = "unknown" if "tag_name" in changes else "failed"
    assert ReleaseCheck("personal", "0.4.9").check().status == expected
    assert github[1] == ["/latest"]


@pytest.mark.parametrize("code", [403, 429])
def test_failure_preserves_last_success_without_retry(github, code):
    checker = ReleaseCheck("personal", "0.4.9")
    successful = checker.check()
    github[0]["/latest"] = (code, {})
    failed = checker.check()
    assert failed.status == "failed"
    assert failed.last_success_at == successful.last_success_at
    assert failed.latest_version == successful.latest_version
    assert failed.checked_at >= successful.checked_at
    assert len(github[1]) == 3


@pytest.mark.parametrize("payload", [b"x" * 1025, "stall", b"not JSON"])
def test_bounded_invalid_transport(github, monkeypatch, payload):
    github[0]["/latest"] = (200, payload)
    monkeypatch.setattr(limits, "RELEASE_CHECK_MAX_BYTES", 1024)
    if payload == "stall":
        # Only the stall needs a short deadline; the others must reach the server.
        monkeypatch.setattr(limits, "RELEASE_CHECK_DEADLINE_SECONDS", 0.1)
    assert ReleaseCheck("personal", "0.4.9").check().status == "failed"
    assert github[1] == ["/latest"]


def test_redirect_cannot_leave_github(github):
    github[0]["/latest"] = (302, {})
    assert ReleaseCheck("personal", "0.4.9").check().status == "failed"
    assert github[1] == ["/latest"]


@pytest.mark.parametrize("source", [True, False])
@pytest.mark.parametrize(
    "initial",
    [
        None,
        {"draft": True},
        {"target_commitish": "b" * 40},
        {"tag_name": "desktop-v0.4.11"},
        {"assets": [{"name": "RCP.zip", "state": "uploaded"}]},
        {"assets": [{"name": name, "state": "new"} for name in ("RCP.zip", "RCP.zip.sha256")]},
    ],
)
def test_companion_rechecks_until_published_then_stops(github, monkeypatch, source, initial):
    monkeypatch.setattr(sys, "frozen", not source, raising=False)
    routes, requests = github
    routes["/tags/desktop-v0.4.10"] = (404, {}) if initial is None else (200, companion(**initial))
    checker = ReleaseCheck("personal", "0.4.9", source_checkout=source)
    notice = checker.check()
    assert notice.status == "update_available"
    assert not notice.companion_ready and notice.download_url is None
    routes["/tags/desktop-v0.4.10"] = (200, companion())
    ready = checker.check()
    assert ready.companion_ready
    assert ready.download_url == "https://github.com/Zhi0467/RCP/releases/tag/desktop-v0.4.10"
    checker.check()
    assert requests.count("/tags/desktop-v0.4.10") == 2


def _age(checker: ReleaseCheck, seconds: float) -> None:
    checked = datetime.now(UTC) - timedelta(seconds=seconds)
    checker._notice = checker._notice.model_copy(update={"checked_at": checked})


def test_route_reads_look_up_again_once_past_the_bound(github):
    routes, requests = github
    checker = ReleaseCheck("personal", "0.4.10")
    bound = limits.RELEASE_CHECK_FRESH_SECONDS
    assert checker.fresh(bound).status == "current"
    _age(checker, bound - 5)
    checker.fresh(bound)
    assert requests == ["/latest"]

    # A stale cache found by several readers at once costs one lookup.
    routes["/latest"] = (200, stable("v0.4.11"))
    _age(checker, bound + 1)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: checker.fresh(bound), range(4)))
    assert requests.count("/latest") == 2
    assert checker.snapshot().status == "update_available"


@pytest.mark.parametrize(("head", "expected"), [(COMMIT, True), ("b" * 40, False), (None, False)])
def test_source_checkout_is_at_release_only_on_its_commit(github, monkeypatch, head, expected):
    monkeypatch.setattr(release_check, "_checkout_head", lambda: head)
    assert (
        ReleaseCheck("personal", "0.4.10", source_checkout=True).check().source_at_release
        is expected
    )
    assert not ReleaseCheck("personal", "0.4.10").check().source_at_release


def test_off_makes_no_calls_even_for_explicit_check(github, monkeypatch):
    monkeypatch.setenv("RCP_UPDATE_CHECK", "off")
    checker = ReleaseCheck("personal", "0.4.9")
    checker.start()
    assert checker.check().status == "off"
    checker.stop()
    assert github[1] == []


def test_team_pin_and_cache_only_authenticated_routes(github, tmp_path, monkeypatch):
    store, _ = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team")
    member = store.preprovision_team_member("Member")
    monkeypatch.setattr(
        "rcp.api.app.read_installed_release_identity", lambda _layout: ("0.4.9", True)
    )
    app = create_app(data_dir=tmp_path)
    checker = app.state.services.release_check
    assert checker.check().status == "pinned"
    assert checker.snapshot().update_command is None
    client = signed_in_client(app, base_url="https://team.test")
    assert client.get("/api/update-notice").status_code == 401
    app = create_app(
        data_dir=tmp_path,
        trusted_principal_resolver=lambda _request, opened: opened.space_user(member.user_id),
    )
    checker = app.state.services.release_check
    expected = checker.check().model_dump(mode="json")
    before = list(github[1])
    client = signed_in_client(app, base_url="https://team.test")
    assert client.get("/api/update-notice").json() == expected
    assert github[1] == before


def test_poller_lifespan_and_personal_endpoint(github, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "rcp.api.app.build_identity", lambda: type("Identity", (), {"base_version": "0.4.9"})()
    )
    monkeypatch.setattr(limits, "RELEASE_CHECK_START_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(limits, "RELEASE_CHECK_INTERVAL_SECONDS", 0.05)
    app = create_app(data_dir=tmp_path)
    checker = app.state.services.release_check
    assert checker.snapshot().status == "unchecked"
    with signed_in_client(app) as client:
        wait_until(lambda: checker.snapshot().status == "update_available")
        notice = client.get("/api/update-notice").json()
        assert set(notice) == {
            "space",
            "status",
            "current_version",
            "latest_version",
            "checked_at",
            "last_success_at",
            "failure_reason",
            "companion_ready",
            "download_url",
            "source_checkout",
            "source_at_release",
            "update_command",
        }
        assert notice["space"] == "personal"
        wait_until(lambda: github[1].count("/latest") >= 2)
        worker = checker._thread
    assert not worker.is_alive()


@pytest.mark.parametrize("team", [False, True])
def test_served_notice_and_server_settings_share_cache(github, tmp_path, monkeypatch, caplog, team):
    from tests.test_api_server_status import _report

    monkeypatch.setattr("rcp.__version__", "0.4.9")
    monkeypatch.setattr(
        "rcp.api.app.read_installed_release_identity", lambda _layout: ("0.4.9", False)
    )
    monkeypatch.setattr(limits, "RELEASE_CHECK_START_DELAY_SECONDS", 0.01)
    resolver = None
    if team:
        store, _ = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team")
        member = store.preprovision_team_member("Member")

        def resolver(_request, opened):
            return opened.space_user(member.user_id)

    report = _report(source_state="aligned", overall_state="healthy", problems=())
    app = create_app(
        data_dir=tmp_path,
        trusted_principal_resolver=resolver,
        server_doctor_reader=lambda: report,
        server_protected_backup_reader=lambda _report: None,
    )
    checker = app.state.services.release_check
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
        worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        worker.start()
        try:
            wait_until(lambda: server.started)
            wait_until(lambda: checker.snapshot().status == "update_available")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                before = list(github[1])
                response = client.get("/api/update-notice")
                assert response.status_code == 200
                notice = response.json()
                assert notice["status"] == "update_available"
                assert notice["update_command"] == (
                    "sudo rcp server update" if team else "scripts/update-from-source v0.4.10"
                )
                if team:
                    status = client.get("/api/server-status").json()
                    assert status["release_check"] == notice
                    assert status["releases"]["update_available"]
                    github[0]["/latest"] = (503, {})
                    assert checker.check().status == "failed"
                    status = client.get("/api/server-status").json()
                    assert not status["releases"]["update_available"]
                    assert "503" in status["release_check"]["failure_reason"]
                    assert status["overall"]["tone"] == "good"
                    assert report.source_state == "aligned"
                    # Refresh looks up again sooner than an ordinary read.
                    _age(checker, limits.RELEASE_CHECK_REFRESH_SECONDS + 1)
                    looked_up = github[1].count("/latest")
                    client.get("/api/server-status")
                    assert github[1].count("/latest") == looked_up
                    github[0]["/latest"] = (200, stable())
                    status = client.get("/api/server-status", params={"refresh": "true"}).json()
                    assert github[1].count("/latest") == looked_up + 1
                    assert status["release_check"]["status"] == "update_available"
                    assert status["release_check"]["failure_reason"] is None
                    assert status["releases"]["running_version"] == "0.4.9"
                    assert status["releases"]["latest_version"] == "0.4.10"
                else:
                    assert github[1] == before
        finally:
            server.should_exit = True
            worker.join(timeout=10)
        assert not worker.is_alive()
    assert not [record for record in caplog.records if record.levelno >= 40]
