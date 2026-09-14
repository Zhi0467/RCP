from __future__ import annotations

import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.agents import AgentLauncher
from rcp.api import provider_login
from rcp.api.dependencies import get_launcher, get_store
from rcp.storage import AppStore


@pytest.mark.parametrize("success", [True, False])
def test_verify_real_probe_result_updates_login_and_attributes_member(
    tmp_path, monkeypatch, success
):
    store = AppStore(tmp_path / "login.sqlite3")
    store.mark_provider_login_failed(
        "codex", "", generation=0, detail="refresh_token_reused", source="turn"
    )
    launcher = AgentLauncher(login_state=store.provider_login_state)
    monkeypatch.setattr(provider_login, "_discover_local_provider", lambda _: "/test/codex")
    probes = []
    holds = []
    monkeypatch.setattr(
        launcher.credential_gate,
        "hold_blocking",
        lambda provider, host: holds.append((provider, host)) or nullcontext(),
    )

    def probe(host, command, *, timeout):
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
    monkeypatch.setattr(
        provider_login,
        "_resume_account",
        lambda *args: resumed.append(args[1:]) or {"recoveries": 1},
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
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
    if not success:
        assert response.json()["detail"] == "still signed out"


def test_non_auth_verify_failure_preserves_signed_in(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "login.sqlite3")
    launcher = AgentLauncher(login_state=store.provider_login_state)
    monkeypatch.setattr(provider_login, "_discover_local_provider", lambda _: "/test/codex")
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
    response = TestClient(app).post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 409
    assert store.provider_login_state("codex", "").state == "signed_in"
    assert store.provider_login_states() == []
