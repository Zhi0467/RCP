from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.api import provider_login
from rcp.api.dependencies import get_provider_sign_ins
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey

from .helpers import wait_until
from .test_provider_sign_in import _runner


@pytest.mark.parametrize("verify_ok", [True, False])
def test_paste_code_stdin_once_owner_and_no_persisted_secret(tmp_path, monkeypatch, verify_ok):
    machine_home = tmp_path / "machine-home"
    machine_home.mkdir()
    monkeypatch.setenv("HOME", str(machine_home))
    secret = "private-one-time-code"
    binary = tmp_path / "fake-agy"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys, select\n"
        "if sys.argv[1:] == ['acp', '--login']:\n"
        " print('https://accounts.google.com/o/oauth2/auth?state=test', flush=True)\n"
        " code = sys.stdin.readline()\n"
        f" assert code == {secret + chr(10)!r}\n"
        " print(code, flush=True)\n"
        " assert not select.select([sys.stdin], [], [], 0.2)[0]\n"
        "else:\n"
        ' print(\'{"status":"SUCCESS"}\')\n'
        f" sys.exit({0 if verify_ok else 1})\n"
    )
    binary.chmod(0o755)
    runner = _runner(tmp_path, monkeypatch, binary)
    added = runner.store.add_provider_login(
        "agy", BACKEND_MACHINE_ID, member_id="owner", added_by="owner"
    )
    viewer = ["owner"]
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id=viewer[0])),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    client = TestClient(app)
    started = client.post(
        "/api/providers/agy/logins/sign-in",
        json={"machine_id": BACKEND_MACHINE_ID, "login_id": added.login_id},
    )
    assert started.status_code == 200
    attempt = started.json()["attempt_id"]
    endpoint = f"/api/providers/agy/logins/sign-in/{attempt}/code"
    wait_until(
        lambda: runner.sign_in_status(attempt).awaiting_code,
        timeout=30,
        detail=lambda: runner.sign_in_status(attempt).model_dump_json(),
    )
    viewer[0] = "other"
    assert client.post(endpoint, json={"code": secret}).status_code == 404
    viewer[0] = "owner"
    invalid = client.post(endpoint, json={"code": [secret]})
    assert invalid.status_code == 422
    assert secret not in invalid.text
    accepted = client.post(endpoint, json={"code": secret})
    assert accepted.status_code == 200
    assert accepted.json()["awaiting_code"] is False
    assert secret not in accepted.text
    assert client.post(endpoint, json={"code": secret}).status_code == 409
    wait_until(lambda: runner.sign_in_status(attempt).state != "pending", timeout=30)
    status = runner.sign_in_status(attempt)
    assert status.state == ("succeeded" if verify_ok else "failed")
    assert not status.awaiting_code
    assert secret not in status.model_dump_json()
    state = runner.store.provider_login_state(LoginKey("agy", BACKEND_MACHINE_ID, added.login_id))
    assert state.state == ("signed_in" if verify_ok else "signed_out")
    assert secret not in state.model_dump_json()
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != binary:
            assert secret.encode() not in path.read_bytes()


@pytest.mark.parametrize("state", ["pending", "succeeded", "failed"])
def test_code_endpoint_refuses_attempt_not_awaiting_code(tmp_path, monkeypatch, state):
    from rcp.runs.provider_sign_in import ProviderSignInStatus

    runner = _runner(tmp_path, monkeypatch, tmp_path / "unused-provider")
    runner._sign_ins["attempt"] = ProviderSignInStatus(
        attempt_id="attempt",
        provider="agy",
        machine_id=BACKEND_MACHINE_ID,
        state=state,
        started_at=runner.store.now(),
        started_by="owner",
    )
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="owner")),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    response = TestClient(app).post(
        "/api/providers/agy/logins/sign-in/attempt/code", json={"code": "private-code"}
    )
    assert response.status_code == 409
    assert "private-code" not in response.text


def test_canceled_paste_login_never_republishes_echoed_url(tmp_path, monkeypatch):
    from rcp.agents.provider_environment import ProviderProcessEnvironment
    from rcp.providers.agy.auth import AgyPasteLogin
    from rcp.runs.provider_sign_in import ProviderSignInStatus, SignInCanceled

    runner = _runner(tmp_path, monkeypatch, tmp_path / "unused-provider")
    status = ProviderSignInStatus(
        attempt_id="attempt",
        provider="agy",
        machine_id=BACKEND_MACHINE_ID,
        state="pending",
        started_at=runner.store.now(),
        started_by="owner",
    )
    runner._sign_ins[status.attempt_id] = status

    def output():
        yield "https://accounts.google.com/sign-in\n"
        assert runner.sign_in_status("attempt").awaiting_code
        runner.cancel_sign_in("attempt", member_id="owner")
        yield "https://accounts.google.com/private-code-echo\n"

    process = SimpleNamespace(stdout=output(), stdin=None, poll=lambda: 0, wait=lambda: 1)
    runner._popen = lambda *args, **kwargs: process
    with pytest.raises(SignInCanceled):
        runner._device_login(
            status, AgyPasteLogin(), "unused-provider", ProviderProcessEnvironment()
        )
    current = runner.sign_in_status("attempt")
    assert not current.awaiting_code
    assert current.verification_url is None
    assert "private-code-echo" not in current.model_dump_json()
