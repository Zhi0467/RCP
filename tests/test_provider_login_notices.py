from __future__ import annotations

from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher
from rcp.agents.provider_accounts import ProviderAccounts
from rcp.api.episodes import serialize_episode
from rcp.service import ProjectService, agent_login_accounts
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.models import LoginKey

from .test_episode_api_serialization import _auto_episode, _begin_report, _branch_summary, _project


@pytest.mark.parametrize("queued", [True, False])
def test_notices_follow_viewer_setting_and_hide_bound_other_owner_details(
    manifest, tmp_path, queued
):
    store = AppStore(tmp_path / "app.sqlite3")
    _project(store)
    accounts = ProviderAccounts.for_store(store)
    own = store.add_provider_login("codex", "", member_id="owner", added_by="owner")
    key = LoginKey("codex", "", own.login_id)
    store.mark_provider_login_signed_out(
        key, member_id="owner", source="sign_out", detail="private@example.test"
    )
    store.set_provider_login_setting("viewer", "codex", "", "shared")
    now = store.now()
    task = AgentTaskRecord(
        operation_id="bound-task",
        project_id="project",
        kind="project_chat",
        status="queued",
        request={"provider": "codex", "run_on": "local", "chat_id": "bound-chat"},
        login_id=key.login_id,
        created_at=now,
        updated_at=now,
        status_message="",
    )
    store.create_agent_task(task)
    store.checkpoint_agent_task(
        task.operation_id, native_session_id="bound-session", stage_root=str(tmp_path / "stage")
    )
    store.record_agent_task_receipt(
        task.operation_id, tier="summary", category="provider_runtime_selected", payload={}
    )
    if not queued:
        store.fail_agent_task(
            task.operation_id, "authentication failed", failure_kind="provider_auth"
        )
    launcher = AgentLauncher(accounts=accounts)
    rows = ProjectService.provider_logins_for(manifest, launcher, "project", viewer_id="viewer")
    codex = {row["login_id"]: row for row in rows if row["provider"] == "codex"}
    assert set(codex) == {"", key.login_id}
    assert codex[key.login_id]["owner_user_id"] == "owner"
    assert codex[key.login_id]["detail"] is None
    assert codex[key.login_id]["source"] is None
    assert codex[key.login_id]["token"] is None
    owner_rows = ProjectService.provider_logins_for(
        manifest, launcher, "project", viewer_id="owner"
    )
    assert (
        next(row for row in owner_rows if row["login_id"] == key.login_id)["detail"]
        == accounts.account_state(key).detail
    )


def test_new_launch_notices_resolve_own_instead_of_machine(manifest, tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    _project(store)
    own = store.add_provider_login("codex", "", member_id="viewer", added_by="viewer")
    key = LoginKey("codex", "", own.login_id)
    store.mark_provider_login_verified(key, member_id="viewer", detail="verified")
    store.set_provider_login_setting("viewer", "codex", "", "own")
    selected = agent_login_accounts(manifest, store, "project", viewer_id="viewer")
    assert key in selected
    assert LoginKey("codex", "", "") not in selected


def test_readiness_follows_the_viewer_login(manifest, tmp_path, monkeypatch):
    store = AppStore(tmp_path / "app.sqlite3")
    launcher = AgentLauncher(accounts=ProviderAccounts.for_store(store))
    cached = {"provider_readiness": {"laptop": {"codex": {"authenticated": False, "models": []}}}}
    store.set_provider_login_setting("other", "codex", "", "shared")
    # The machine login resolves, but the project's probe said it is signed out.
    other = ProjectService.viewer_readiness(cached, manifest, launcher, "other")
    assert other["provider_readiness"]["laptop"]["codex"]["authenticated"] is False
    store.mark_provider_login_signed_out(
        LoginKey("codex", "", ""), member_id=None, source="sign_out", detail="machine"
    )
    own = store.add_provider_login("codex", "", member_id="viewer", added_by="viewer")
    store.mark_provider_login_verified(
        LoginKey("codex", "", own.login_id), member_id="viewer", detail="verified"
    )
    store.set_provider_login_setting("viewer", "codex", "", "own")
    probed = []

    def readiness(provider, *, host, binary, refresh, login_id):
        probed.append((provider, login_id, refresh))
        return SimpleNamespace(
            model_dump=lambda **_: {"authenticated": True, "models": [{"id": login_id}]}
        )

    monkeypatch.setattr(launcher, "readiness", readiness)
    monkeypatch.setattr(launcher, "provider_binary", lambda *_: "codex")
    snapshot = ProjectService.viewer_readiness(cached, manifest, launcher, "viewer", refresh=True)
    assert snapshot["provider_readiness"]["laptop"]["codex"]["models"] == [{"id": own.login_id}]
    assert probed == [("codex", own.login_id, True)]
    # Shared resolves only the signed-out machine sign-in: refused, nothing probed.
    other = ProjectService.viewer_readiness(cached, manifest, launcher, "other")
    assert other["provider_readiness"]["laptop"]["codex"]["authenticated"] is False
    assert len(probed) == 1


def test_report_admission_uses_episode_authorizer_login(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    _project(store)
    episode, root = _auto_episode(store, "own-report")
    assert episode.authorized_by is not None
    member = episode.authorized_by.user_id
    own = store.add_provider_login("codex", "", member_id=member, added_by=member)
    key = LoginKey("codex", "", own.login_id)
    store.set_provider_login_setting(member, "codex", "", "own")
    _begin_report(store, episode, root, ending="completed")
    episode = store.episode(episode.episode_id)
    assert episode is not None
    blocked = serialize_episode(store, "project", episode, branch_summary=_branch_summary)
    assert blocked.blocked_reason == "sign_in"
    store.mark_provider_login_verified(key, member_id=member, detail="verified")
    ready = serialize_episode(store, "project", episode, branch_summary=_branch_summary)
    assert ready.blocked_reason != "sign_in"


@pytest.mark.parametrize("suffix", ["", "/cached"])
def test_project_response_refreshes_viewer_logins_without_rewriting_shared_cache(
    manifest, tmp_path, suffix
):
    from .helpers import create_named_app, signed_in_client

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    store = app.state.services.store
    project_id = app.state.default_project_id
    owner = store.local_owner
    assert owner is not None
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    before = app.state.catalog.cached_snapshot(project_id)
    own = store.add_provider_login("codex", "", member_id=owner.user_id, added_by=owner.user_id)
    key = LoginKey("codex", "", own.login_id)
    store.mark_provider_login_verified(key, member_id=owner.user_id, detail="verified")
    store.set_provider_login_setting(owner.user_id, "codex", "", "own")
    response = client.get(f"/api/projects/{project_id}{suffix}")
    assert response.status_code == 200
    assert {
        row["login_id"] for row in response.json()["provider_logins"] if row["provider"] == "codex"
    } == {key.login_id}
    after = app.state.catalog.cached_snapshot(project_id)
    assert before["provider_logins"] == after["provider_logins"]


@pytest.mark.parametrize("existing", [False, True])
def test_missing_own_login_notice_has_no_fabricated_login(manifest, tmp_path, existing):
    store = AppStore(tmp_path / "app.sqlite3", space_kind="team")
    accounts = ProviderAccounts.for_store(store)
    own = (
        store.add_provider_login("codex", "", member_id="new-member", added_by="new-member")
        if existing
        else None
    )
    rows = ProjectService.provider_logins_for(
        manifest, AgentLauncher(accounts=accounts), None, viewer_id="new-member"
    )
    codex = [row for row in rows if row["provider"] == "codex"]
    assert len(codex) == 1
    row = codex[0]
    assert row["login_id"] == (own.login_id if own else None)
    assert row["kind"] == "own"
    assert row["owner_user_id"] == "new-member"
    assert row["state"] == "signed_out"
    assert row["host"] == ""
    assert row["label"]
    if existing:
        return
    assert all(
        value in (None, [], 0)
        for field, value in row.items()
        if field not in {"provider", "host", "kind", "owner_user_id", "state", "label"}
    )
    assert not any(row.member_id == "new-member" for row in store.provider_logins("codex", ""))
