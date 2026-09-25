from __future__ import annotations

import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.agents import AgentLauncher
from rcp.agents import launcher as launcher_module
from rcp.agents.provider_accounts import ProviderAccounts
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.api import provider_login
from rcp.api.dependencies import (
    get_catalog,
    get_launcher,
    get_provider_credentials,
    get_provider_sign_ins,
    get_store,
)
from rcp.runs.provider_sign_in import ProviderSignInRunner
from rcp.storage import AppStore


@pytest.mark.parametrize("success", [True, False])
def test_verify_real_probe_result_updates_login_and_attributes_member(
    tmp_path, monkeypatch, success
):
    store = AppStore(tmp_path / "login.sqlite3")
    store.mark_provider_login_failed(
        "codex", "", generation=0, detail="refresh_token_reused", source="turn"
    )
    launcher = AgentLauncher(accounts=ProviderAccounts.for_store(store))
    monkeypatch.setattr(launcher_module, "_discover_local_provider", lambda _: "/test/codex")
    probes = []
    holds = []
    monkeypatch.setattr(
        launcher.credential_gate,
        "hold_blocking",
        lambda provider, host: holds.append((provider, host)) or nullcontext(),
    )

    def probe(host, command, *, timeout, **_):
        assert timeout == 60
        probes.append((host, command))
        return subprocess.CompletedProcess(
            command,
            0 if success else 1,
            "OK" if success else "",
            "" if success else "still signed out",
        )

    monkeypatch.setattr(launcher, "_probe", probe)
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="acting-member")),
    )
    resumed = []
    runner = ProviderSignInRunner(store, launcher, launcher.accounts)
    runner.resume_account = lambda *args: resumed.append(args) or {"checked": 1}
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    response = TestClient(app).post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == (200 if success else 409)
    assert holds == [("codex", "")]
    assert len(probes) == 1
    assert probes[0][1][1] == "exec"
    state = store.provider_login_state("codex", "")
    assert state.state == ("signed_in" if success else "signed_out")
    assert state.generation == (1 if success else 0)
    assert state.changed_by == ("acting-member" if success else None)
    assert len(resumed) == int(success)


def test_non_auth_verify_failure_preserves_signed_in(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "login.sqlite3")
    launcher = AgentLauncher(accounts=ProviderAccounts.for_store(store))
    monkeypatch.setattr(launcher_module, "_discover_local_provider", lambda _: "/test/codex")
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    monkeypatch.setattr(launcher.credential_gate, "hold_blocking", lambda *_: nullcontext())
    monkeypatch.setattr(
        launcher,
        "_probe",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[1], 255, "", "SSH transport unavailable"
        ),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
    app.dependency_overrides[get_provider_sign_ins] = lambda: ProviderSignInRunner(
        store, launcher, launcher.accounts
    )
    response = TestClient(app).post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 409
    assert store.provider_login_state("codex", "").state == "signed_in"
    assert store.provider_login_states() == []


def test_login_list_shows_configured_accounts_and_drops_hosts_no_project_names(
    tmp_path, monkeypatch
):
    store = AppStore(tmp_path / "login.sqlite3")
    store.mark_provider_login_failed("codex", "", generation=0, detail="expired", source="turn")
    store.mark_provider_login_failed(
        "codex", "gone.example", generation=0, detail="expired", source="turn"
    )
    credentials = ProviderCredentialStore(tmp_path / "providers")
    credentials.store_token(
        "claude", "", "sk-ant-oat01-list-test", member_id="member", now=store.now()
    )
    accounts = ProviderAccounts(store, credentials)
    launcher = AgentLauncher(accounts=accounts)
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
    app.dependency_overrides[get_catalog] = lambda: SimpleNamespace(
        provider_targets=lambda: [("claude", "", None), ("codex", "", None)]
    )
    app.dependency_overrides[get_provider_credentials] = lambda: credentials
    app.dependency_overrides[get_provider_sign_ins] = lambda: ProviderSignInRunner(
        store, launcher, accounts
    )
    response = TestClient(app).get("/api/providers/logins")
    assert response.status_code == 200, response.text
    accounts = response.json()
    # The removed host's row is history nobody can act on; Verify would refuse it.
    assert [(a["provider"], a["host"], a["state"]) for a in accounts] == [
        ("claude", "", "signed_in"),
        ("codex", "", "signed_out"),
    ]
    token = accounts[0]["token"]
    assert token["pasted_by"] == "member" and token["verified_at"] is None
    assert "sk-ant" not in response.text
    assert accounts[0]["sign_in"] is None


def test_third_provider_device_login_completes_without_polling_and_status_is_read_only(
    tmp_path, monkeypatch
):
    """Registration and an existing interaction suffice; HTTP never owns completion."""
    from rcp.providers import PROVIDERS, CodexProfile

    from .helpers import wait_until
    from .test_provider_sign_in import _fake_codex, _runner

    class ThirdProvider(CodexProfile):
        id = "test-provider"
        label = "Test Research Provider"

    monkeypatch.setitem(PROVIDERS, ThirdProvider.id, ThirdProvider())
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    runner.store.mark_provider_login_failed(
        ThirdProvider.id, "", generation=0, detail="expired", source="turn"
    )
    completions = []
    runner.resume_account = lambda *args: completions.append(args) or {"checked": 1}
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: runner.store
    app.dependency_overrides[get_provider_credentials] = lambda: runner.credentials
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    app.dependency_overrides[get_catalog] = lambda: SimpleNamespace(
        provider_targets=lambda: [(ThirdProvider.id, "", None)]
    )
    client = TestClient(app)
    account = client.get("/api/providers/logins").json()[0]
    assert account["label"] == ThirdProvider.label
    assert account["sign_in_methods"] == ["device_code"]
    assert account["token"] is None
    route = f"/api/providers/{ThirdProvider.id}/logins"
    started = client.post(f"{route}/sign-in", json={"host": ""})
    assert started.status_code == 200, started.text
    operation = started.json()["login_id"]
    assert client.post(f"{route}/sign-in", json={}).json()["login_id"] == operation
    # The page is gone: no status GET or other HTTP request completes the work.
    (tmp_path / "signed-in").write_text("")
    wait_until(lambda: runner.sign_in_status(operation).state == "succeeded" or None, timeout=10)
    runner.reconcile_recovery()
    assert completions == [(ThirdProvider.id, "")]
    status = client.get(f"{route}/sign-in/{operation}")
    assert status.status_code == 200
    assert status.json()["state"] == "succeeded"
    assert status.json()["resumed"] == {"checked": 1}
    assert client.get(f"{route}/sign-in/{operation}").json() == status.json()
    assert completions == [(ThirdProvider.id, "")]
    assert client.get(f"/api/providers/codex/logins/sign-in/{operation}").status_code == 404
    assert client.post(f"{route}/token", json={"token": "private-value"}).status_code == 422
    invalid = client.post(f"{route}/sign-in", json={"host": "", "unexpected": True})
    assert invalid.status_code == 422
    assert client.post("/api/providers/unknown/logins/sign-in", json={}).status_code == 422


def test_account_api_reports_missing_managed_credential_without_a_state_row(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "login.sqlite3")
    accounts = ProviderAccounts(store, ProviderCredentialStore(tmp_path / "providers"))
    runner = ProviderSignInRunner(store, AgentLauncher(accounts=accounts), accounts)
    accounts = provider_login.provider_login_accounts(
        store,
        SimpleNamespace(provider_targets=lambda: [("claude", "", None)]),
        accounts.credentials,
        runner,
    )
    assert accounts[0].state == "signed_out"
    assert accounts[0].sign_in_methods == ("token_entry",)
    assert accounts[0].token is None
    assert store.provider_login_states() == []


@pytest.mark.parametrize(
    "body",
    [
        {"token": {"secret": "private-value"}},
        {"token": "private-value", "unexpected": "private-value"},
        {"host": ["private-value"], "token": "private-value"},
    ],
)
def test_invalid_account_requests_do_not_echo_credentials(tmp_path, body):
    store = AppStore(tmp_path / "login.sqlite3")
    accounts = ProviderAccounts(store, ProviderCredentialStore(tmp_path / "providers"))
    runner = ProviderSignInRunner(store, AgentLauncher(accounts=accounts), accounts)
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    response = TestClient(app).post("/api/providers/claude/logins/token", json=body)
    assert response.status_code == 422
    assert "private-value" not in response.text
