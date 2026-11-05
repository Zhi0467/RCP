from __future__ import annotations

import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.agents import AgentLauncher
from rcp.agents import launcher as launcher_module
from rcp.agents.launcher import ProviderExecutionAccount
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
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.providers import PROVIDER_IDS
from rcp.runs.provider_sign_in import (
    ProviderLoginRefused,
    ProviderSignInRunner,
)
from rcp.storage import AppStore
from rcp.storage.models import MACHINE_LOGIN_ID


@pytest.mark.parametrize("success", [True, False])
def test_verify_real_probe_result_updates_login_and_attributes_member(
    tmp_path, monkeypatch, success
):
    store = AppStore(tmp_path / "login.sqlite3")
    store.mark_provider_login_failed(
        LoginKey("codex", BACKEND_MACHINE_ID, MACHINE_LOGIN_ID),
        generation=0,
        detail="refresh_token_reused",
        source="turn",
    )
    launcher = AgentLauncher(accounts=ProviderAccounts.for_store(store))
    monkeypatch.setattr(launcher_module, "_discover_local_provider", lambda _: "/test/codex")
    probes = []
    holds = []
    monkeypatch.setattr(
        launcher.credential_gate,
        "hold_blocking",
        lambda key: holds.append((key.provider, key.machine_id)) or nullcontext(),
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
    response = TestClient(app).post(
        "/api/providers/codex/logins/verify", json={"machine_id": BACKEND_MACHINE_ID}
    )
    assert response.status_code == (200 if success else 409)
    assert holds == [("codex", BACKEND_MACHINE_ID)]
    assert len(probes) == 1
    assert probes[0][1][1] == "exec"
    state = store.provider_login_state(LoginKey("codex", BACKEND_MACHINE_ID, MACHINE_LOGIN_ID))
    assert state.state == ("signed_in" if success else "signed_out")
    assert state.generation == (1 if success else 0)
    assert state.changed_by == ("acting-member" if success else None)
    assert len(resumed) == int(success)
    if not success:
        assert "verification request" in response.json()["detail"]


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
    response = TestClient(app).post(
        "/api/providers/codex/logins/verify", json={"machine_id": BACKEND_MACHINE_ID}
    )
    assert response.status_code == 409
    assert (
        store.provider_login_state(LoginKey("codex", BACKEND_MACHINE_ID, MACHINE_LOGIN_ID)).state
        == "signed_in"
    )
    assert store.provider_login_states() == []


def test_login_list_shows_configured_accounts_and_drops_hosts_no_project_names(
    tmp_path, monkeypatch
):
    store = AppStore(tmp_path / "login.sqlite3")
    store.mark_provider_login_failed(
        LoginKey("codex", BACKEND_MACHINE_ID, MACHINE_LOGIN_ID),
        generation=0,
        detail="expired",
        source="turn",
    )
    store.mark_provider_login_failed(
        LoginKey("codex", "gone.example", MACHINE_LOGIN_ID),
        generation=0,
        detail="expired",
        source="turn",
    )
    credentials = ProviderCredentialStore(tmp_path / "providers")
    credentials.store_token(
        "claude",
        BACKEND_MACHINE_ID,
        "sk-ant-oat01-list-test",
        member_id="member",
        now=store.now(),
        login_id=MACHINE_LOGIN_ID,
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
        provider_targets=lambda: [
            ("claude", BACKEND_MACHINE_ID, None),
            ("codex", BACKEND_MACHINE_ID, None),
        ]
    )
    app.dependency_overrides[get_provider_credentials] = lambda: credentials
    app.dependency_overrides[get_provider_sign_ins] = lambda: ProviderSignInRunner(
        store, launcher, accounts
    )
    response = TestClient(app).get("/api/providers/logins")
    assert response.status_code == 200, response.text
    accounts = [a for a in response.json() if a["provider"] in {"claude", "codex"}]
    assert [(a["provider"], a["machine_id"], a["state"]) for a in accounts] == [
        ("claude", BACKEND_MACHINE_ID, "signed_in"),
        ("codex", BACKEND_MACHINE_ID, "signed_out"),
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
        LoginKey(ThirdProvider.id, BACKEND_MACHINE_ID, MACHINE_LOGIN_ID),
        generation=0,
        detail="expired",
        source="turn",
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
        provider_targets=lambda: [(ThirdProvider.id, BACKEND_MACHINE_ID, None)]
    )
    client = TestClient(app)
    (account,) = (
        a for a in client.get("/api/providers/logins").json() if a["provider"] == ThirdProvider.id
    )
    assert account["label"] == ThirdProvider.label
    assert account["sign_in_methods"] == ["device_code"]
    assert account["token"] is None
    route = f"/api/providers/{ThirdProvider.id}/logins"
    started = client.post(f"{route}/sign-in", json={"machine_id": BACKEND_MACHINE_ID})
    assert started.status_code == 200, started.text
    operation = started.json()["attempt_id"]
    assert (
        client.post(f"{route}/sign-in", json={"machine_id": BACKEND_MACHINE_ID}).status_code == 409
    )
    (tmp_path / "signed-in").write_text("")
    wait_until(lambda: runner.sign_in_status(operation).state == "succeeded" or None, timeout=10)
    runner.reconcile_recovery()
    assert completions == [(LoginKey(ThirdProvider.id, BACKEND_MACHINE_ID, ""),)]
    status = client.get(f"{route}/sign-in/{operation}")
    assert status.status_code == 200
    assert status.json()["state"] == "succeeded"
    assert status.json()["resumed"] == {"checked": 1}
    assert client.get(f"{route}/sign-in/{operation}").json() == status.json()
    assert completions == [(LoginKey(ThirdProvider.id, BACKEND_MACHINE_ID, ""),)]
    assert client.get(f"/api/providers/codex/logins/sign-in/{operation}").status_code == 404
    assert (
        client.post(
            f"{route}/token", json={"machine_id": BACKEND_MACHINE_ID, "token": "private-value"}
        ).status_code
        == 422
    )
    invalid = client.post(
        f"{route}/sign-in", json={"machine_id": BACKEND_MACHINE_ID, "unexpected": True}
    )
    assert invalid.status_code == 422
    assert client.post("/api/providers/unknown/logins/sign-in", json={}).status_code == 422


def test_account_api_reports_missing_managed_credential_without_a_state_row(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "login.sqlite3")
    accounts = ProviderAccounts(store, ProviderCredentialStore(tmp_path / "providers"))
    runner = ProviderSignInRunner(store, AgentLauncher(accounts=accounts), accounts)
    accounts = provider_login.provider_login_accounts(
        store,
        SimpleNamespace(provider_targets=lambda: [("claude", BACKEND_MACHINE_ID, None)]),
        accounts.credentials,
        runner,
        set(),
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
        {"machine_id": ["private-value"], "token": "private-value"},
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


def test_a_space_machine_no_project_uses_can_be_signed_in(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "login.sqlite3")
    gpu = store.create_space_machine(name="GPU", host="gpu.example", os_account="alice").machine_id
    credentials = ProviderCredentialStore(tmp_path / "providers")
    accounts = ProviderAccounts(store, credentials)
    launcher = AgentLauncher(accounts=accounts)
    runner = ProviderSignInRunner(store, launcher, accounts)
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_catalog] = lambda: SimpleNamespace(provider_targets=lambda: [])
    app.dependency_overrides[get_provider_credentials] = lambda: credentials
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner

    listed = TestClient(app).get("/api/providers/logins").json()
    remote = [account for account in listed if account["machine_id"] == gpu]
    assert {account["provider"] for account in remote} == set(PROVIDER_IDS)
    assert all(account["machines"] == ["GPU"] for account in remote)
    assert all(account["provider_path"] is None for account in remote)
    reached = {"account": "alice"}
    monkeypatch.setattr(
        launcher,
        "execution_account",
        lambda *, host="": ProviderExecutionAccount(
            host=host, reachable=True, os_account=reached["account"]
        ),
    )
    monkeypatch.setattr(
        launcher,
        "discover_provider",
        lambda provider, *, host: f"/home/{reached['account']}/.local/bin/{provider}",
    )
    assert runner.provider_binary("codex", gpu) == (
        "/home/alice/.local/bin/codex",
        set(),
    )
    from rcp.agents.launcher import ProviderDiscoveryUnavailable

    def unanswered(provider, *, host):
        raise ProviderDiscoveryUnavailable(f"{host} did not answer the discovery probe.")

    monkeypatch.setattr(launcher, "discover_provider", unanswered)
    with pytest.raises(ProviderLoginRefused) as refused:
        runner.provider_binary("codex", gpu)
    assert refused.value.detail == "gpu.example did not answer the discovery probe."
    with pytest.raises(ProviderLoginRefused):
        runner.provider_binary("codex", "unknown")
    reached["account"] = "bob"
    with pytest.raises(ProviderLoginRefused):
        runner.provider_binary("codex", gpu)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "detail"),
    [
        (255, "", "ssh: Connection refused", "gpu.example is unreachable"),
        (127, "", "bash: python3: command not found", "python3: command not found"),
        (127, "", "", "exit status 127"),
        (0, "relative/provider", "", "returned no absolute executable path"),
        (1, "", "", "Codex executable was not found"),
    ],
)
@pytest.mark.parametrize("action", ["verify", "sign-in"])
def test_sign_in_preserves_remote_discovery_failure(
    tmp_path, monkeypatch, returncode, stdout, stderr, detail, action
):
    store = AppStore(tmp_path / "login.sqlite3")
    gpu = store.create_space_machine(name="GPU", host="gpu.example", os_account="").machine_id
    launcher = AgentLauncher(accounts=ProviderAccounts.for_store(store))
    probes = []

    def probe(host, command, **_kwargs):
        assert host == "gpu.example"
        assert command[:2] == ["python3", "-c"]
        probes.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(launcher, "_probe", probe)
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    runner = ProviderSignInRunner(store, launcher, launcher.accounts)
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    response = TestClient(app).post(
        f"/api/providers/codex/logins/{action}", json={"machine_id": gpu}
    )
    assert response.status_code == 409
    assert detail in response.json()["detail"]
    assert len(probes) == 1
    assert store.provider_login_states() == []


def test_an_unmanaged_login_note_names_the_machine_card_executable(tmp_path):
    store = AppStore(tmp_path / "login.sqlite3")
    credentials = ProviderCredentialStore(tmp_path / "providers")
    accounts = ProviderAccounts(store, credentials)
    catalog = SimpleNamespace(provider_targets=lambda: [("opencode", BACKEND_MACHINE_ID, None)])
    runner = ProviderSignInRunner(store, AgentLauncher(accounts=accounts), accounts)

    def note():
        (account,) = (
            account
            for account in provider_login.provider_login_accounts(
                store, catalog, credentials, runner, set()
            )
            if account.provider == "opencode"
        )
        assert not account.managed
        return account.login_command

    assert note() == "opencode providers login"
    card = store.backend_space_machine()
    store.update_space_machine(card.machine_id, provider_paths={"opencode": "/opt/oc bin/opencode"})
    assert note() == "'/opt/oc bin/opencode' providers login"


def test_only_accounts_an_agent_launches_on_are_marked_agent_used(tmp_path, monkeypatch, manifest):
    store = AppStore(tmp_path / "login.sqlite3")
    gpu = store.create_space_machine(name="GPU", host="gpu.example", os_account="").machine_id
    monkeypatch.setattr(
        store,
        "projects",
        lambda: [SimpleNamespace(project_id="p", locator=str(manifest.path))],
    )
    monkeypatch.setattr(store, "member_project_ids", lambda _member: {"p"})
    credentials = ProviderCredentialStore(tmp_path / "providers")
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
        provider_targets=lambda: [
            (provider, machine_id, None)
            for provider in PROVIDER_IDS
            for machine_id in (BACKEND_MACHINE_ID, gpu)
        ]
    )
    app.dependency_overrides[get_provider_credentials] = lambda: credentials
    app.dependency_overrides[get_provider_sign_ins] = lambda: ProviderSignInRunner(
        store, launcher, accounts
    )
    listed = TestClient(app).get("/api/providers/logins").json()
    used = {(a["provider"], a["machine_id"]) for a in listed if a["agent_used"]}
    assert {a["machine_id"] for a in listed} == {BACKEND_MACHINE_ID, gpu}
    assert used and all(machine_id == BACKEND_MACHINE_ID for _provider, machine_id in used)


@pytest.fixture
def member_login_api(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "member-logins.sqlite3", space_kind="team")
    accounts = ProviderAccounts(store, ProviderCredentialStore(tmp_path / "credentials"))
    runner = ProviderSignInRunner(store, AgentLauncher(accounts=accounts), accounts)
    viewer = {"id": "alice"}
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id=viewer["id"])),
    )
    monkeypatch.setattr(
        runner,
        "resume_setting",
        lambda *args: {"checked": 2},
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    app.dependency_overrides[get_provider_credentials] = lambda: accounts.credentials
    app.dependency_overrides[get_catalog] = lambda: SimpleNamespace(
        provider_targets=lambda: [
            ("claude", BACKEND_MACHINE_ID, None),
            ("codex", BACKEND_MACHINE_ID, None),
        ]
    )
    return TestClient(app), store, runner, viewer


def test_member_rows_filter_private_evidence_and_attempt_codes(member_login_api):
    from rcp.runs.provider_sign_in import ProviderSignInStatus

    client, store, runner, viewer = member_login_api
    own = store.add_provider_login(
        "claude", BACKEND_MACHINE_ID, member_id="alice", added_by="alice"
    )
    key = LoginKey("claude", BACKEND_MACHINE_ID, own.login_id)
    runner.credentials.store_token(
        "claude",
        BACKEND_MACHINE_ID,
        "secret",
        member_id="alice",
        now=store.now(),
        login_id=own.login_id,
    )
    store.mark_provider_login_failed(
        key, generation=0, detail="private@example.invalid", source="probe"
    )
    shared = store.add_provider_login("codex", BACKEND_MACHINE_ID, added_by="alice")
    runner._sign_ins["attempt"] = ProviderSignInStatus(
        attempt_id="attempt",
        login_id=shared.login_id,
        provider="codex",
        machine_id=BACKEND_MACHINE_ID,
        state="pending",
        user_code="CODE",
        verification_url="https://example.invalid/verify",
        started_at=store.now(),
        started_by="alice",
    )
    rows = {
        row["login_id"]: row
        for row in client.get("/api/providers/logins").json()
        if row["login_id"]
    }
    assert rows[own.login_id]["detail"] == "private@example.invalid"
    assert rows[own.login_id]["token"] is not None
    assert rows[shared.login_id]["sign_in"]["user_code"] == "CODE"
    viewer["id"] = "bob"
    rows = {
        row["login_id"]: row
        for row in client.get("/api/providers/logins").json()
        if row["login_id"]
    }
    assert all(rows[own.login_id][field] is None for field in ("detail", "source", "token"))
    assert rows[shared.login_id]["sign_in"]["user_code"] is None
    assert rows[shared.login_id]["sign_in"]["verification_url"] is None
    assert client.get("/api/providers/codex/logins/sign-in/attempt").status_code == 404
    assert client.post("/api/providers/codex/logins/sign-in/attempt/cancel").status_code == 404


def test_add_own_conflict_owner_actions_and_remove_rules(member_login_api):
    client, store, runner, viewer = member_login_api
    route = "/api/providers/claude/logins"
    added = client.post(f"{route}/add", json={"machine_id": BACKEND_MACHINE_ID, "kind": "own"})
    assert added.status_code == 200
    login_id = added.json()["login_id"]
    assert added.json()["sign_in"] is None
    assert (
        client.post(
            f"{route}/add", json={"machine_id": BACKEND_MACHINE_ID, "kind": "own"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{route}/remove", json={"machine_id": BACKEND_MACHINE_ID, "login_id": ""}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{route}/remove", json={"machine_id": BACKEND_MACHINE_ID, "login_id": login_id}
        ).status_code
        == 403
    )
    viewer["id"] = "bob"
    for action in ("verify", "sign-in", "sign-out", "remove", "token"):
        body = {"machine_id": BACKEND_MACHINE_ID, "login_id": login_id}
        if action == "token":
            body["token"] = "private-token"
        assert client.post(f"{route}/{action}", json=body).status_code == 403
    viewer["id"] = "alice"
    store.finish_provider_login_removal(LoginKey("claude", BACKEND_MACHINE_ID, login_id))
    assert (
        client.post(
            f"{route}/verify", json={"machine_id": BACKEND_MACHINE_ID, "login_id": login_id}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{route}/token",
            json={"machine_id": BACKEND_MACHINE_ID, "login_id": login_id, "token": "private-token"},
        ).status_code
        == 409
    )
    assert runner.credentials.token("claude", BACKEND_MACHINE_ID, login_id) is None


@pytest.mark.parametrize(("provider", "action"), [("claude", "sign-in"), ("codex", "token")])
def test_authorization_precedes_unsupported_method(member_login_api, provider, action):
    client, store, _, viewer = member_login_api
    row = store.add_provider_login(
        provider, BACKEND_MACHINE_ID, member_id="alice", added_by="alice"
    )
    body = (
        {"machine_id": BACKEND_MACHINE_ID, "login_id": row.login_id, "token": "private-token"}
        if action == "token"
        else {"machine_id": BACKEND_MACHINE_ID, "login_id": row.login_id}
    )
    route = f"/api/providers/{provider}/logins/{action}"
    assert client.post(route, json=body).status_code == 422
    store.finish_provider_login_removal(LoginKey(provider, BACKEND_MACHINE_ID, row.login_id))
    assert client.post(route, json=body).status_code == 409
    viewer["id"] = "bob"
    response = client.post(route, json=body)
    assert response.status_code == 403
    assert "alice" not in response.text and row.login_id not in response.text
    body["login_id"] = "00000000-0000-4000-8000-000000000001"
    assert client.post(route, json=body).status_code == 404


@pytest.mark.parametrize("setting", ["own", "own_then_shared", "shared_then_own", "shared"])
def test_setting_round_trip(member_login_api, setting):
    client, store, runner, viewer = member_login_api
    saved = client.put(
        "/api/providers/claude/logins/setting",
        json={"machine_id": BACKEND_MACHINE_ID, "setting": setting},
    )
    assert saved.status_code == 200
    assert saved.json()["resumed"] == {"checked": 2}
    item = next(
        item
        for item in client.get("/api/providers/logins/settings").json()["settings"]
        if item["provider"] == "claude"
    )
    assert item["setting"] == setting and item["stored"] is True


@pytest.mark.parametrize("setting", ["", "both_off", "OWN", None, False, [], {}])
def test_setting_invalid_value_is_422(member_login_api, setting):
    client, *_ = member_login_api
    assert (
        client.put(
            "/api/providers/claude/logins/setting",
            json={"machine_id": BACKEND_MACHINE_ID, "setting": setting},
        ).status_code
        == 422
    )


def test_settings_get_precedence(member_login_api):
    client, store, runner, viewer = member_login_api

    def current():
        return next(
            item
            for item in client.get("/api/providers/logins/settings").json()["settings"]
            if item["provider"] == "claude"
        )

    assert current()["setting"] == "own" and current()["stored"] is False
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO provider_login_settings VALUES(?,?,?,?,?)",
            ("alice", "claude", "*", "own_then_shared", store.now()),
        )
    assert current()["setting"] == "own_then_shared" and current()["stored"] is True
    store.set_provider_login_setting("alice", "claude", BACKEND_MACHINE_ID, "shared_then_own")
    assert current()["setting"] == "shared_then_own"


def test_personal_login_rules(tmp_path, monkeypatch):
    client_store = AppStore(tmp_path / "personal.sqlite3")
    accounts = ProviderAccounts.for_store(client_store)
    runner = ProviderSignInRunner(client_store, AgentLauncher(accounts=accounts), accounts)
    runner.resume_setting = lambda *_: {"checked": 0}
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    client = TestClient(app)
    assert (
        client_store.provider_login_setting("member", "claude", BACKEND_MACHINE_ID).setting
        == "shared"
    )
    assert (
        client.post(
            "/api/providers/claude/logins/add",
            json={"machine_id": BACKEND_MACHINE_ID, "kind": "own"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/providers/claude/logins/setting",
            json={"machine_id": BACKEND_MACHINE_ID, "setting": "own"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/providers/opencode/logins/add",
            json={"machine_id": BACKEND_MACHINE_ID, "kind": "shared"},
        ).status_code
        == 422
    )


def test_shared_remove_calls_keyed_lifecycle_and_reports_drain(member_login_api, monkeypatch):
    client, store, runner, _viewer = member_login_api
    added = client.post(
        "/api/providers/claude/logins/add",
        json={"machine_id": BACKEND_MACHINE_ID, "kind": "shared"},
    ).json()
    key = LoginKey("claude", BACKEND_MACHINE_ID, added["login_id"])
    calls = []

    def remove(login_store, launcher, login, *, cancel_sign_in, member_id):
        assert login_store is store
        assert launcher is runner.launcher
        assert cancel_sign_in == runner.cancel_login_sign_in
        calls.append((login, member_id))
        return store.set_provider_login_draining(login)

    monkeypatch.setattr(provider_login.provider_login, "remove_shared_login", remove)
    monkeypatch.setattr(
        provider_login.provider_login,
        "bound_running_count",
        lambda _store, login: 2 if login == key else 0,
    )
    response = client.post(
        "/api/providers/claude/logins/remove",
        json={"machine_id": BACKEND_MACHINE_ID, "login_id": key.login_id},
    )
    assert response.status_code == 200
    assert calls == [(key, "alice")]
    assert response.json()["bound_running"] == 2
    assert response.json()["draining_since"] and response.json()["removed_at"] is None
    row = next(
        row for row in client.get("/api/providers/logins").json() if row["login_id"] == key.login_id
    )
    assert row["bound_running"] == 2
