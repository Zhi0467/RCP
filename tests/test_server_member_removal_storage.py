from __future__ import annotations

import uuid

import pytest

from rcp.core.models import AuthorizedHuman
from rcp.limits import MEMBER_REMOVAL_PREVIEW_MAX_ITEMS
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    EpisodeRecord,
    ProjectRecord,
    SpaceUserRecord,
    TeamAuthenticationError,
)

from .storage_helpers import downgrade_artifacts


def _claimed_team(tmp_path):
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    alice, alice_token = store.enroll_team_member(bootstrap, "Alice")
    invitation, bob_code = store.create_team_invitation(alice.user_id)
    bob, bob_token = store.enroll_team_member(bob_code, "Bob")
    return store, alice, alice_token, invitation, bob, bob_token


def _project(store: AppStore, name: str = "Shared project") -> ProjectRecord:
    project_id = str(uuid.uuid4())
    now = store.now()
    return store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            home_space_id=store.space_id,
            locator=f"/tmp/{project_id}/research.yaml",
            name=name,
            state_location=f"/tmp/{project_id}/.research",
            state_remote=False,
            added_at=now,
        )
    )


def _authorized(store: AppStore, member: SpaceUserRecord) -> AuthorizedHuman:
    assert member.display_name is not None
    return AuthorizedHuman(
        space_id=store.space_id,
        user_id=member.user_id,
        display_name=member.display_name,
    )


def test_member_removal_fences_every_access_path_and_keeps_a_tombstone(tmp_path) -> None:
    store, alice, _alice_token, _alice_invite, bob, bob_token = _claimed_team(tmp_path)
    project = _project(store)
    store.seat_project_member(project.project_id, alice.user_id)
    store.seat_project_member(project.project_id, bob.user_id)
    session, _resolved = store.create_team_session(bob_token)
    space_invitation, _space_code = store.create_team_invitation(bob.user_id)
    charlie = store.preprovision_team_member("Charlie")
    project_invitation = store.invite_to_project(
        project.project_id,
        charlie.user_id,
        invited_by=bob.user_id,
    )

    preview = store.member_removal_preview(bob.user_id)

    assert preview.member == bob
    assert not preview.last_authenticating_member
    assert preview.project_ids == (project.project_id,)
    assert preview.orphaned_project_ids == ()
    assert preview.browser_session_count == 1
    assert len(preview.active_token_ids) == 1
    assert preview.space_invitation_ids == (space_invitation.invitation_id,)
    assert preview.project_invitation_ids == (project_invitation.invitation_id,)

    fenced = store.begin_member_removal(
        bob.user_id,
        expected_boundary_sha256=preview.boundary_sha256,
    )

    assert fenced.member.removal_started_at is not None
    assert fenced.member.removed_at is None
    assert fenced.project_ids == ()
    assert fenced.active_token_ids == ()
    assert fenced.browser_session_count == 0
    assert fenced.space_invitation_ids == ()
    assert fenced.project_invitation_ids == ()
    assert store.resolve_team_session(session) is None
    with pytest.raises(TeamAuthenticationError) as revoked:
        store.create_team_session(bob_token)
    assert revoked.value.code == "team_token_invalid"
    assert not store.is_project_member(project.project_id, bob.user_id)
    assert store.member_removal_preview(bob.user_id) == fenced
    assert (
        store.begin_member_removal(
            bob.user_id,
            expected_boundary_sha256=preview.boundary_sha256,
        )
        == fenced
    )

    with store.connection() as connection:
        revoked_space = connection.execute(
            "SELECT revoked_at FROM team_invitations WHERE invitation_id = ?",
            (space_invitation.invitation_id,),
        ).fetchone()
        revoked_project = connection.execute(
            "SELECT response, responded_at FROM project_invitations WHERE invitation_id = ?",
            (project_invitation.invitation_id,),
        ).fetchone()
    assert revoked_space["revoked_at"] is not None
    assert tuple(revoked_project) == ("revoked", fenced.member.removal_started_at)

    completed = store.complete_member_removal(bob.user_id)
    assert completed.removed_at is not None
    assert store.complete_member_removal(bob.user_id) == completed
    assert store.space_user(bob.user_id) == completed
    assert completed.display_name == "Bob"


def test_member_removal_preview_is_an_exact_confirmation_boundary(tmp_path) -> None:
    store, alice, _alice_token, _alice_invite, bob, _bob_token = _claimed_team(tmp_path)
    preview = store.member_removal_preview(bob.user_id)
    project = _project(store, "Added after preview")
    store.seat_project_member(project.project_id, alice.user_id)
    store.seat_project_member(project.project_id, bob.user_id)

    with pytest.raises(ValueError, match="changed after preview"):
        store.begin_member_removal(
            bob.user_id,
            expected_boundary_sha256=preview.boundary_sha256,
        )

    assert store.space_user(bob.user_id).removal_started_at is None
    assert store.is_project_member(project.project_id, bob.user_id)
    assert store.create_team_invitation(bob.user_id)


def test_member_removal_does_not_confuse_browser_login_count_with_display_bound(
    tmp_path,
) -> None:
    store, _alice, _alice_token, _alice_invite, bob, bob_token = _claimed_team(tmp_path)
    sessions = [
        store.create_team_session(bob_token)[0] for _ in range(MEMBER_REMOVAL_PREVIEW_MAX_ITEMS + 1)
    ]

    preview = store.member_removal_preview(bob.user_id)
    assert preview.browser_session_count == len(sessions)

    fenced = store.begin_member_removal(
        bob.user_id,
        expected_boundary_sha256=preview.boundary_sha256,
    )
    assert fenced.browser_session_count == 0
    assert all(store.resolve_team_session(session) is None for session in sessions)


def test_member_removal_refuses_the_last_authenticating_member(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    alice, _token = store.enroll_team_member(bootstrap, "Alice")
    store.preprovision_team_member("Not enrolled")
    store.create_team_invitation(alice.user_id)
    preview = store.member_removal_preview(alice.user_id)
    assert preview.last_authenticating_member

    with pytest.raises(ValueError, match="no other enrolled member"):
        store.begin_member_removal(
            alice.user_id,
            expected_boundary_sha256=preview.boundary_sha256,
        )

    with pytest.raises(ValueError, match="last enrolled member"):
        store.revoke_team_token(alice.user_id)
    assert store.rotate_team_token(alice.user_id).startswith("rcp_")


def test_member_removal_and_self_revoke_refuse_to_orphan_a_project(tmp_path) -> None:
    store, alice, _alice_token, _alice_invite, bob, _bob_token = _claimed_team(tmp_path)
    project = _project(store, "Only Alice has access")
    store.seat_project_member(project.project_id, alice.user_id)
    preview = store.member_removal_preview(alice.user_id)

    with pytest.raises(ValueError, match="Only Alice has access"):
        store.begin_member_removal(
            alice.user_id,
            expected_boundary_sha256=preview.boundary_sha256,
        )
    with pytest.raises(ValueError, match="Only Alice has access"):
        store.revoke_team_token(alice.user_id)

    store.seat_project_member(project.project_id, bob.user_id)
    store.revoke_team_token(alice.user_id)


def test_member_tombstone_waits_for_authorized_tasks_and_episodes(tmp_path) -> None:
    store, alice, _alice_token, _alice_invite, bob, _bob_token = _claimed_team(tmp_path)
    project = _project(store)
    store.seat_project_member(project.project_id, alice.user_id)
    store.seat_project_member(project.project_id, bob.user_id)
    now = store.now()
    operation_id = str(uuid.uuid4())
    episode_id = str(uuid.uuid4())
    authorized = _authorized(store, bob)
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project.project_id,
            kind="refresh",
            status="queued",
            request={},
            created_at=now,
            updated_at=now,
            status_message="Queued",
            authorized_by=authorized,
        )
    )
    store.create_episode(
        EpisodeRecord(
            episode_id=episode_id,
            project_id=project.project_id,
            mode="experiment_loop",
            control_node_id="experiment-node",
            status="queued",
            invocation_ceiling=1,
            authorized_by=authorized,
            created_at=now,
            updated_at=now,
        )
    )
    preview = store.member_removal_preview(bob.user_id)
    assert preview.active_task_ids == (operation_id,)
    assert preview.active_episode_ids == (episode_id,)

    store.begin_member_removal(
        bob.user_id,
        expected_boundary_sha256=preview.boundary_sha256,
    )
    with pytest.raises(RuntimeError, match="live authorized work"):
        store.complete_member_removal(bob.user_id)

    store.request_agent_task_pause(operation_id)
    store.pause_agent_task(operation_id, detail="Membership removed")
    store.request_episode_stop(episode_id)
    store.mark_episode_stop_skipped(episode_id, diagnostic="Authorizing member removed")
    completed = store.complete_member_removal(bob.user_id)
    assert completed.removed_at is not None
    assert store.members_pending_removal() == []


def test_old_project_invitation_table_is_migrated_to_accept_revocation(tmp_path) -> None:
    path = tmp_path / "rcp.sqlite3"
    store, alice, _alice_token, _alice_invite, bob, _bob_token = _claimed_team(tmp_path)
    project = _project(store)
    store.seat_project_member(project.project_id, alice.user_id)
    pending = store.invite_to_project(
        project.project_id,
        bob.user_id,
        invited_by=alice.user_id,
    )
    with store.connection() as connection:
        connection.execute("DROP INDEX project_invitations_invitee")
        connection.execute("DROP INDEX project_invitations_pending")
        connection.execute("ALTER TABLE project_invitations RENAME TO current_project_invitations")
        connection.execute(
            """
            CREATE TABLE project_invitations (
                invitation_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                invited_user_id TEXT NOT NULL,
                invited_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                response TEXT CHECK(response IN ('accepted', 'declined')),
                responded_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO project_invitations SELECT * FROM current_project_invitations"
        )
        connection.execute("DROP TABLE current_project_invitations")
        downgrade_artifacts(connection)
        connection.execute(
            "DELETE FROM storage_schema_migrations WHERE migration_version IN (5, 6)"
        )

    reopened = AppStore(path)
    with reopened.connection() as connection:
        connection.execute(
            "UPDATE project_invitations SET response = 'revoked' WHERE invitation_id = ?",
            (pending.invitation_id,),
        )
        row = connection.execute(
            "SELECT response FROM project_invitations WHERE invitation_id = ?",
            (pending.invitation_id,),
        ).fetchone()
        indexes = {item[1] for item in connection.execute("PRAGMA index_list(project_invitations)")}
    assert row["response"] == "revoked"
    assert {"project_invitations_invitee", "project_invitations_pending"} <= indexes


def test_provider_cleanup_fences_own_logins_and_preserves_shared_and_sessions(
    tmp_path, monkeypatch
) -> None:
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.server_ops.provider_cleanup import cleanup_member_provider_logins
    from rcp.storage.models import LoginKey

    store, _alice, _, _, bob, _ = _claimed_team(tmp_path)
    credentials = ProviderCredentialStore.for_data_dir(tmp_path)
    own = store.add_provider_login("claude", "", member_id=bob.user_id, added_by=bob.user_id)
    shared = store.add_provider_login("claude", "", added_by=bob.user_id)
    codex = store.add_provider_login("codex", "", member_id=bob.user_id, added_by=bob.user_id)
    for row in (own, shared, codex):
        store.mark_provider_login_verified(
            LoginKey(row.provider, row.host, row.login_id), member_id=bob.user_id, detail="ready"
        )
    for row in (own, shared):
        credentials.store_token(
            "claude", "", "secret", member_id=bob.user_id, now=store.now(), login_id=row.login_id
        )
    home = credentials.account_dir("codex", "", codex.login_id) / "codex-home"
    home.mkdir(parents=True)
    (home / "auth.json").write_text("secret")
    (home / "sessions").mkdir()
    (home / "sessions" / "session.jsonl").write_text("history")
    store.set_provider_login_setting(bob.user_id, "claude", "", "shared")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO provider_login_settings VALUES (?, ?, ?, ?, ?)",
            (bob.user_id, "codex", "*", "own_then_shared", store.now()),
        )

    assert cleanup_member_provider_logins(store, bob.user_id) == ()
    for row in (own, codex):
        removed = store.provider_login_state(LoginKey(row.provider, row.host, row.login_id))
        assert removed.state == "signed_out"
        assert removed.removed_at is not None
        assert removed.generation == 2
    assert not credentials.token_path("claude", "", own.login_id).exists()
    assert credentials.token("claude", "", shared.login_id) == "secret"
    assert store.provider_login_state(LoginKey("claude", "", shared.login_id)).state == "signed_in"
    assert not (home / "auth.json").exists()
    assert (home / "sessions" / "session.jsonl").read_text() == "history"
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM provider_login_settings WHERE user_id = ?", (bob.user_id,)
            ).fetchone()[0]
            == 0
        )
    assert cleanup_member_provider_logins(store, bob.user_id) == ()
    assert store.provider_login_state(LoginKey("claude", "", own.login_id)).generation == 2


def test_member_advance_reports_unreached_cleanup_even_on_completion(tmp_path, monkeypatch) -> None:
    import subprocess

    from rcp.server_ops import provider_cleanup
    from rcp.storage.models import LoginKey
    from tests.test_server_member_removal import _team

    store, _alice, bob, _, _, coordinator = _team(tmp_path)
    own = store.add_provider_login(
        "codex", "worker.example", member_id=bob.user_id, added_by=bob.user_id
    )
    key = LoginKey("codex", "worker.example", own.login_id)
    store.mark_provider_login_verified(key, member_id=bob.user_id, detail="ready")
    attempts = []

    def unreachable(arguments, **kwargs):
        attempts.append(arguments)
        return subprocess.CompletedProcess(arguments, 255)

    monkeypatch.setattr(provider_cleanup.subprocess, "run", unreachable)
    boundary = coordinator.plan(bob.user_id).snapshot.boundary_sha256
    result = coordinator.advance(bob.user_id, boundary_sha256=boundary)
    assert result.provider_cleanup_unreached == ("worker.example",)
    assert result.snapshot.removed_at is not None
    assert store.provider_login_state(key).removed_at is not None
    assert len(attempts) == 1
    assert coordinator.reconcile_pending() == ()
    coordinator.advance(bob.user_id, boundary_sha256=boundary)
    assert len(attempts) == 1
    # After completion the removal reconciler, which runs at startup, retries it.
    from rcp.agents import AgentLauncher
    from rcp.agents.provider_accounts import ProviderAccounts
    from rcp.runs.provider_login import reconcile_login_removals

    launcher = AgentLauncher(accounts=ProviderAccounts.for_store(store))
    reconcile_login_removals(store, launcher)
    assert len(attempts) == 2


def test_pending_member_cleanup_retries_removed_login_credentials(tmp_path, monkeypatch) -> None:
    import subprocess

    from rcp.server_ops import provider_cleanup
    from rcp.storage.models import LoginKey

    store, _alice, _, _, bob, _ = _claimed_team(tmp_path)
    own = store.add_provider_login(
        "claude", "worker.example", member_id=bob.user_id, added_by=bob.user_id
    )
    attempts = []

    def remote(arguments, **kwargs):
        attempts.append(arguments)
        return subprocess.CompletedProcess(arguments, 255 if len(attempts) == 1 else 0)

    monkeypatch.setattr(provider_cleanup.subprocess, "run", remote)
    assert provider_cleanup.cleanup_member_provider_logins(store, bob.user_id) == (
        "worker.example",
    )
    assert provider_cleanup.cleanup_member_provider_logins(store, bob.user_id) == ()
    assert len(attempts) == 2
    assert (
        store.provider_login_state(LoginKey("claude", "worker.example", own.login_id)).generation
        == 1
    )


def test_member_cleanup_keeps_a_credential_until_its_running_turn_ends(tmp_path) -> None:
    import json

    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.server_ops import provider_cleanup

    store, _alice, _, _, bob, _ = _claimed_team(tmp_path)
    own = store.add_provider_login("claude", "", member_id=bob.user_id, added_by=bob.user_id)
    credentials = ProviderCredentialStore.for_data_dir(store.path.parent)
    credentials.store_token(
        "claude", "", "token", member_id=bob.user_id, now=store.now(), login_id=own.login_id
    )
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO graph_runs(operation_id,project_id,kind,status,request_json,created_at,"
            "updated_at,status_message,login_id) VALUES('turn','project','refresh','running',?,?,?,"
            "'running',?)",
            (json.dumps({"provider": "claude"}), store.now(), store.now(), own.login_id),
        )
    assert provider_cleanup.cleanup_member_provider_logins(store, bob.user_id) == ()
    assert credentials.token("claude", "", own.login_id) == "token"
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status = 'completed'")
    assert provider_cleanup.cleanup_member_provider_logins(store, bob.user_id) == ()
    assert credentials.token("claude", "", own.login_id) is None


@pytest.mark.parametrize(
    ("provider", "relative"),
    [
        ("claude", ".config/rcp/logins/{}/claude-setup-token"),
        ("codex", ".rcp/provider-homes/codex/{}/auth.json"),
    ],
)
def test_remote_cleanup_helper_preserves_other_files(tmp_path, monkeypatch, provider, relative):
    from pathlib import Path

    from rcp.server_ops.provider_cleanup import remove_remote_credential

    login_id = str(uuid.uuid4())
    credential = tmp_path / relative.format(login_id)
    credential.parent.mkdir(parents=True)
    credential.write_text("secret")
    retained = credential.with_name("session.jsonl")
    retained.write_text("history")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    remove_remote_credential(provider, login_id)
    assert not credential.exists()
    assert retained.read_text() == "history"
