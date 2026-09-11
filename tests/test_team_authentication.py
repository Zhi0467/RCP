from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.core.models import AuthorizedHuman
from rcp.limits import (
    TEAM_CODE_FAILED_ATTEMPT_LIMIT,
    TEAM_DEVICE_PAIRING_TTL_MINUTES,
    TEAM_SESSION_IDLE_DAYS,
    TEAM_SESSION_LABEL_MAX_LENGTH,
)
from rcp.server_ops.config import ServerTeamConfig, render_team_access_config
from rcp.server_runtime import ServerMetadata
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    ProjectRecord,
    TeamAuthenticationError,
    normalize_space_access_url,
)


def _claimed_team(tmp_path):
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    member, token = store.enroll_team_member(bootstrap, "Alice")
    return store, bootstrap, member, token


def _enroll_invited_member(store: AppStore, creator_id: str, name: str):
    invitation, code = store.create_team_invitation(creator_id)
    member, token = store.enroll_team_member(code, name)
    return invitation, code, member, token


def _sqlite_bytes(path) -> bytes:
    payload = b""
    for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        if candidate.exists():
            payload += candidate.read_bytes()
    return payload


def test_session_ids_migrate_independently_and_preserve_sessions(tmp_path) -> None:
    store, _, member, token = _claimed_team(tmp_path)
    secrets = [store.create_team_session(token)[0] for _ in range(3)]
    with store.connection() as connection:
        before = connection.execute(
            "SELECT session_hash, user_id, created_at, last_seen_at, expires_at "
            "FROM team_sessions ORDER BY session_hash"
        ).fetchall()
        connection.execute("DROP INDEX team_sessions_public_id")
        connection.execute("ALTER TABLE team_sessions DROP COLUMN session_id")
        connection.execute("ALTER TABLE team_sessions DROP COLUMN label")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 14")

    migrated = AppStore(store.path)
    sessions = migrated.team_sessions(member.user_id)
    assert all(session.label == "Unnamed device" for session in sessions)
    identifiers = {session.session_id for session in sessions}
    assert len(identifiers) == 3
    assert all(uuid.UUID(identifier).version == 4 for identifier in identifiers)
    assert identifiers.isdisjoint(hashlib.sha256(secret.encode()).hexdigest() for secret in secrets)
    with migrated.connection() as connection:
        after = connection.execute(
            "SELECT session_hash, user_id, created_at, last_seen_at, expires_at "
            "FROM team_sessions ORDER BY session_hash"
        ).fetchall()
        assert [tuple(row) for row in before] == [tuple(row) for row in after]
        assert (
            connection.execute(
                "SELECT migration_name FROM storage_schema_migrations WHERE migration_version = 14"
            ).fetchone()[0]
            == "team_session_ids_v1"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE team_sessions SET session_id = ?", (sessions[0].session_id,))
    reopened = AppStore(store.path)
    assert {session.session_id for session in reopened.team_sessions(member.user_id)} == identifiers
    assert all(reopened.resolve_team_session(secret) == member for secret in secrets)


def test_members_list_and_revoke_only_their_own_device_sessions(tmp_path) -> None:
    store, _, alice, alice_token = _claimed_team(tmp_path)
    _, _, bob, bob_token = _enroll_invited_member(store, alice.user_id, "Alice")
    app = create_app(data_dir=tmp_path)
    clients = [TestClient(app, base_url="https://testserver") for _ in range(4)]
    for client, token in zip(clients, [alice_token] * 3 + [bob_token], strict=True):
        assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200
    desktop, phone, other, bob_client = clients
    response = desktop.get("/api/team/sessions")
    assert response.status_code == 200
    sessions = response.json()
    assert len(sessions) == 3
    assert len({item["session_id"] for item in sessions}) == 3
    assert all(
        set(item)
        == {
            "session_id",
            "label",
            "created_at",
            "last_seen_at",
            "expires_at",
            "is_current",
            "can_revoke",
        }
        for item in sessions
    )
    for client in clients:
        secret = client.cookies.get("__Host-rcp_session")
        assert secret not in response.text
        assert hashlib.sha256(secret.encode()).hexdigest() not in response.text
    assert alice_token not in response.text
    current = next(item for item in sessions if item["is_current"])
    assert current["can_revoke"] is False
    assert all(item["can_revoke"] for item in sessions if not item["is_current"])
    refused_current = desktop.post(f"/api/team/sessions/{current['session_id']}/revoke", json={})
    assert refused_current.status_code == 409
    assert desktop.get("/api/identity").status_code == 200

    bob_sessions = bob_client.get("/api/team/sessions").json()
    assert len(bob_sessions) == 1
    assert {item["session_id"] for item in sessions}.isdisjoint(
        item["session_id"] for item in bob_sessions
    )
    foreign = desktop.post(f"/api/team/sessions/{bob_sessions[0]['session_id']}/revoke", json={})
    unknown = desktop.post(f"/api/team/sessions/{uuid.uuid4()}/revoke", json={})
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    phone_id = next(
        item["session_id"] for item in phone.get("/api/team/sessions").json() if item["is_current"]
    )
    revoked = desktop.post(f"/api/team/sessions/{phone_id}/revoke", json={})
    assert revoked.status_code == 200
    assert revoked.json() == {"ok": True}
    assert phone.get("/api/identity").status_code == 401
    assert desktop.get("/api/identity").status_code == 200
    assert other.get("/api/identity").status_code == 200
    assert bob_client.get("/api/identity").json()["user"]["user_id"] == bob.user_id
    assert len(desktop.get("/api/team/sessions").json()) == 2
    assert phone.post("/api/team/session/exchange", json={"token": alice_token}).status_code == 200


def test_session_listing_omits_expired_rows_without_refreshing_idle_expiry(tmp_path) -> None:
    store, _, member, token = _claimed_team(tmp_path)
    live_secret, _ = store.create_team_session(token)
    expired_secret, _ = store.create_team_session(token)
    with store.connection() as connection:
        connection.execute(
            "UPDATE team_sessions SET expires_at = ? WHERE session_hash = ?",
            (
                (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                hashlib.sha256(expired_secret.encode()).hexdigest(),
            ),
        )
    before = store.team_sessions(member.user_id, authenticating_session=live_secret)
    assert len(before) == 1
    assert before[0].is_current is True
    assert store.team_sessions(member.user_id, authenticating_session=live_secret) == before


@pytest.mark.parametrize(
    "label", [" My <script>alert(1)</script> phone ", "", "x" * TEAM_SESSION_LABEL_MAX_LENGTH]
)
def test_session_exchange_preserves_the_supplied_label(tmp_path, label) -> None:
    store, _, member, token = _claimed_team(tmp_path)
    client = TestClient(create_app(data_dir=tmp_path), base_url="https://testserver")
    response = client.post(
        "/api/team/session/exchange",
        json={"token": token, "label": label},
        headers={"User-Agent": "Never use this as a device label"},
    )
    assert response.status_code == 200
    assert client.get("/api/team/sessions").json()[0]["label"] == label
    assert AppStore(store.path).team_sessions(member.user_id)[0].label == label


def test_session_exchange_defaults_an_omitted_label_and_rejects_excess_length(tmp_path) -> None:
    store, _, member, token = _claimed_team(tmp_path)
    client = TestClient(create_app(data_dir=tmp_path), base_url="https://testserver")
    rejected = client.post(
        "/api/team/session/exchange",
        json={"token": token, "label": "x" * (TEAM_SESSION_LABEL_MAX_LENGTH + 1)},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"][0]["loc"] == ["body", "label"]
    assert store.team_sessions(member.user_id) == []
    exchanged = client.post(
        "/api/team/session/exchange",
        json={"token": token},
        headers={"User-Agent": "A fingerprint must not name this session"},
    )
    assert exchanged.status_code == 200
    assert client.get("/api/team/sessions").json()[0]["label"] == "Unnamed device"
    assert store.team_sessions(member.user_id)[0].label == "Unnamed device"


def test_bootstrap_is_not_issued_before_late_schema_work_succeeds(tmp_path, monkeypatch) -> None:
    def fail_late_schema_work(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("injected late schema failure")

    monkeypatch.setattr(AppStore, "_ensure_column", fail_late_schema_work)
    with pytest.raises(sqlite3.OperationalError, match="injected late schema failure"):
        AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")

    assert not (tmp_path / "rcp.sqlite3").exists()

    monkeypatch.undo()
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    assert store.space_kind == "team"
    assert bootstrap.startswith("rcp_bootstrap_")


def test_bootstrap_claim_is_atomic_and_single_use(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")

    def claim(name: str):
        try:
            return store.enroll_team_member(bootstrap, name)
        except TeamAuthenticationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("Alice", "Bob")))

    successes = [result for result in results if isinstance(result, tuple)]
    failures = [result for result in results if isinstance(result, str)]
    assert len(successes) == 1
    assert failures == ["enrollment_code_consumed"]
    assert len(store.space_users()) == 1
    with pytest.raises(TeamAuthenticationError) as reused:
        store.enroll_team_member(bootstrap, "Charlie")
    assert reused.value.code == "enrollment_code_consumed"


def test_invitations_are_creator_private_expiring_single_use_credentials(tmp_path) -> None:
    store, _bootstrap, alice, _alice_token = _claimed_team(tmp_path)
    alice_first, first_code = store.create_team_invitation(alice.user_id)
    _alice_second, second_code = store.create_team_invitation(alice.user_id)
    bob, _bob_token = store.enroll_team_member(first_code, "Same name")
    bob_invitation, _bob_code = store.create_team_invitation(bob.user_id)

    assert alice.user_id != bob.user_id
    assert bob.display_name == "Same name"
    assert {item.invitation_id for item in store.team_invitations(alice.user_id)} == {
        alice_first.invitation_id,
        _alice_second.invitation_id,
    }
    assert [item.invitation_id for item in store.team_invitations(bob.user_id)] == [
        bob_invitation.invitation_id
    ]
    with pytest.raises(TeamAuthenticationError) as reused:
        store.enroll_team_member(first_code, "Another person")
    assert reused.value.code == "enrollment_code_consumed"

    second_id = second_code.split(".", 1)[0].removeprefix("rcp_invite_")
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "UPDATE team_invitations SET expires_at = ? WHERE invitation_id = ?",
            (expired_at, second_id),
        )
    with pytest.raises(TeamAuthenticationError) as expired:
        store.enroll_team_member(second_code, "Expired")
    assert expired.value.code == "enrollment_code_expired"


def test_authenticated_team_roster_names_enrolled_members_without_credentials(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    client = TestClient(create_app(data_dir=tmp_path), base_url="https://team.test")
    alice_enrollment = client.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"}
    ).json()
    alice = alice_enrollment["identity"]["user"]
    assert (
        client.post(
            "/api/team/session/exchange", json={"token": alice_enrollment["token"]}
        ).status_code
        == 200
    )
    invitation = client.post("/api/team/invitations", json={}).json()
    bob, _bob_token = store.enroll_team_member(invitation["code"], "Bob")

    roster = client.get("/api/space/users")

    assert roster.status_code == 200
    assert roster.json() == [
        {"user_id": alice["user_id"], "display_name": "Alice"},
        {"user_id": bob.user_id, "display_name": "Bob"},
    ]
    assert "token" not in roster.text


def test_team_roster_excludes_member_pending_removal_but_invitation_keeps_name(
    tmp_path,
) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    app = create_app(data_dir=tmp_path)
    alice = TestClient(app, base_url="https://testserver")
    bob = TestClient(app, base_url="https://testserver")
    alice_enrollment = alice.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"}
    ).json()
    alice_user = alice_enrollment["identity"]["user"]
    assert (
        alice.post(
            "/api/team/session/exchange", json={"token": alice_enrollment["token"]}
        ).status_code
        == 200
    )
    invitation = alice.post("/api/team/invitations", json={}).json()
    bob_enrollment = bob.post(
        "/api/team/enroll",
        json={"code": invitation["code"], "display_name": "Bob Collaborator"},
    ).json()
    bob_user = bob_enrollment["identity"]["user"]
    preview = store.member_removal_preview(bob_user["user_id"])

    store.begin_member_removal(
        bob_user["user_id"],
        expected_boundary_sha256=preview.boundary_sha256,
    )

    roster = alice.get("/api/space/users")
    assert roster.status_code == 200
    assert roster.json() == [
        {"user_id": alice_user["user_id"], "display_name": "Alice"},
    ]
    assert [user.user_id for user in store.space_users()] == [
        alice_user["user_id"],
        bob_user["user_id"],
    ]
    invitations = alice.get("/api/team/invitations")
    assert invitations.status_code == 200
    assert invitations.json()[0]["invitation_id"] == invitation["invitation"]["invitation_id"]
    assert invitations.json()[0]["status_label"] == "Bob Collaborator joined"


def test_revoking_an_invitation_stops_the_code_without_touching_others(tmp_path) -> None:
    store, _bootstrap, alice, _alice_token = _claimed_team(tmp_path)
    leaked, leaked_code = store.create_team_invitation(alice.user_id)
    kept, kept_code = store.create_team_invitation(alice.user_id)

    revoked = store.revoke_team_invitation(leaked.invitation_id, alice.user_id)
    assert revoked.revoked_at is not None
    assert revoked.consumed_at is None

    with pytest.raises(TeamAuthenticationError) as refused:
        store.enroll_team_member(leaked_code, "Stranger")
    assert refused.value.code == "enrollment_code_invalid"

    still_valid, _token = store.enroll_team_member(kept_code, "Bob")
    assert still_valid.display_name == "Bob"
    assert kept.invitation_id != leaked.invitation_id


def test_revoking_is_idempotent_and_keeps_the_first_revocation_time(tmp_path) -> None:
    store, _bootstrap, alice, _alice_token = _claimed_team(tmp_path)
    invitation, _code = store.create_team_invitation(alice.user_id)

    first = store.revoke_team_invitation(invitation.invitation_id, alice.user_id)
    second = store.revoke_team_invitation(invitation.invitation_id, alice.user_id)
    assert first.revoked_at == second.revoked_at


def test_only_the_creator_may_revoke_and_a_used_invitation_is_not_revocable(tmp_path) -> None:
    store, _bootstrap, alice, _alice_token = _claimed_team(tmp_path)
    _invitation, _code, bob, _bob_token = _enroll_invited_member(store, alice.user_id, "Bob")
    bob_invitation, _bob_code = store.create_team_invitation(bob.user_id)

    # Equal members: Alice cannot reach an invitation she cannot see.
    with pytest.raises(KeyError):
        store.revoke_team_invitation(bob_invitation.invitation_id, alice.user_id)

    consumed, consumed_code = store.create_team_invitation(alice.user_id)
    store.enroll_team_member(consumed_code, "Carol")
    with pytest.raises(ValueError, match="already been used"):
        store.revoke_team_invitation(consumed.invitation_id, alice.user_id)


def test_wrong_attempts_lock_only_the_target_invitation_code(tmp_path) -> None:
    store, _bootstrap, alice, _alice_token = _claimed_team(tmp_path)
    _target, target_code = store.create_team_invitation(alice.user_id)
    _other, other_code = store.create_team_invitation(alice.user_id)
    public = target_code.split(".", 1)[0]
    wrong_code = f"{public}.{'x' * 43}"

    for attempt in range(TEAM_CODE_FAILED_ATTEMPT_LIMIT):
        with pytest.raises(TeamAuthenticationError) as rejected:
            store.enroll_team_member(wrong_code, f"Guess {attempt}")
        expected = (
            "enrollment_code_locked"
            if attempt == TEAM_CODE_FAILED_ATTEMPT_LIMIT - 1
            else "enrollment_code_invalid"
        )
        assert rejected.value.code == expected

    with pytest.raises(TeamAuthenticationError) as locked:
        store.enroll_team_member(target_code, "Locked out")
    assert locked.value.code == "enrollment_code_locked"
    enrolled, _token = store.enroll_team_member(other_code, "Unaffected")
    assert enrolled.display_name == "Unaffected"


def test_member_tokens_are_prefixed_sha256_indexed_and_compared_in_constant_time(
    tmp_path, monkeypatch
) -> None:
    store, _bootstrap, member, token = _claimed_team(tmp_path)
    assert re.fullmatch(r"rcp_[A-Za-z0-9_-]{43}", token)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    with store.connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(team_member_tokens)")}
        row = connection.execute(
            "SELECT token_hash FROM team_member_tokens WHERE user_id = ?", (member.user_id,)
        ).fetchone()
        indexed_columns = {
            column[2]
            for index in connection.execute("PRAGMA index_list(team_member_tokens)")
            for column in connection.execute(f"PRAGMA index_info({index[1]})")
        }
    assert columns == {"token_id", "user_id", "token_hash", "created_at", "revoked_at"}
    assert row["token_hash"] == token_hash
    assert re.fullmatch(r"[a-f0-9]{64}", row["token_hash"])
    assert "token_hash" in indexed_columns
    assert token.encode() not in _sqlite_bytes(store.path)

    comparisons: list[tuple[str, str]] = []
    real_compare = hmac.compare_digest

    def observed_compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return real_compare(left, right)

    # Token comparison lives in the space/team half of the store.
    monkeypatch.setattr("rcp.storage.spaces.hmac.compare_digest", observed_compare)
    _session, resolved = store.create_team_session(token)
    assert resolved == member
    assert comparisons == [(token_hash, token_hash)]


def test_team_sessions_are_hashed_server_rows_with_fourteen_day_sliding_expiry(tmp_path) -> None:
    store, _bootstrap, member, token = _claimed_team(tmp_path)
    session, resolved = store.create_team_session(token)
    assert resolved == member
    assert session.startswith("rcp_session_")
    session_hash = hashlib.sha256(session.encode()).hexdigest()
    assert AppStore(store.path).resolve_team_session(session) == member
    forced_expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with store.connection() as connection:
        row = connection.execute(
            "SELECT session_hash, expires_at FROM team_sessions WHERE user_id = ?",
            (member.user_id,),
        ).fetchone()
        assert row["session_hash"] == session_hash
        connection.execute(
            "UPDATE team_sessions SET expires_at = ? WHERE session_hash = ?",
            (forced_expiry, session_hash),
        )
    assert session.encode() not in _sqlite_bytes(store.path)

    before_resolution = datetime.now(UTC)
    assert store.resolve_team_session(session) == member
    with store.connection() as connection:
        slid = connection.execute(
            "SELECT expires_at FROM team_sessions WHERE session_hash = ?", (session_hash,)
        ).fetchone()[0]
    slid_expiry = datetime.fromisoformat(slid)
    assert slid_expiry > datetime.fromisoformat(forced_expiry)
    assert slid_expiry >= before_resolution + timedelta(days=TEAM_SESSION_IDLE_DAYS - 1)

    with store.connection() as connection:
        connection.execute(
            "UPDATE team_sessions SET expires_at = ? WHERE session_hash = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), session_hash),
        )
    assert store.resolve_team_session(session) is None
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM team_sessions WHERE session_hash = ?", (session_hash,)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("operations", [("rotate", "rotate"), ("revoke", "rotate")])
def test_credential_mutations_atomically_revalidate_the_presenting_session(
    tmp_path, operations
) -> None:
    store, _bootstrap, member, token = _claimed_team(tmp_path)
    _enroll_invited_member(store, member.user_id, "Other member")
    session, _resolved = store.create_team_session(token)
    barrier = threading.Barrier(2)

    def mutate(operation: str):
        barrier.wait()
        if operation == "rotate":
            return store.rotate_team_token(
                member.user_id,
                authenticating_session=session,
            )
        store.revoke_team_token(
            member.user_id,
            authenticating_session=session,
        )
        return None

    def capture(operation: str):
        try:
            return ("ok", mutate(operation))
        except TeamAuthenticationError as exc:
            return (exc.code, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(capture, operations))

    assert [status for status, _value in results].count("ok") == 1
    assert [status for status, _value in results].count("team_session_invalid") == 1
    assert store.resolve_team_session(session) is None
    with pytest.raises(TeamAuthenticationError):
        store.create_team_session(token)
    replacement = next((value for status, value in results if status == "ok" and value), None)
    if replacement is not None:
        _new_session, resolved = store.create_team_session(replacement)
        assert resolved == member


def test_rotation_and_revocation_are_member_scoped_and_preserve_authorized_work(tmp_path) -> None:
    store, _bootstrap, alice, alice_token = _claimed_team(tmp_path)
    _invite, _code, bob, bob_token = _enroll_invited_member(store, alice.user_id, "Bob")
    alice_session, _ = store.create_team_session(alice_token)
    bob_session, _ = store.create_team_session(bob_token)
    now = store.now()
    project_id = str(uuid.uuid4())
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            home_space_id=store.space_id,
            locator=f"/tmp/{project_id}/research.yaml",
            name="Authorized work",
            state_location=f"/tmp/{project_id}/.research",
            state_remote=False,
            added_at=now,
        )
    )
    operation_id = str(uuid.uuid4())
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="refresh",
            status="running",
            request={"safe": True},
            created_at=now,
            updated_at=now,
            status_message="Running.",
            authorized_by=AuthorizedHuman(
                space_id=store.space_id,
                user_id=bob.user_id,
                display_name="Bob",
            ),
        )
    )
    before = store.agent_task(operation_id)

    replacement = store.rotate_team_token(bob.user_id)
    assert replacement != bob_token
    assert store.resolve_team_session(bob_session) is None
    assert store.resolve_team_session(alice_session) == alice
    with pytest.raises(TeamAuthenticationError):
        store.create_team_session(bob_token)
    assert store.agent_task(operation_id) == before

    replacement_session, _ = store.create_team_session(replacement)
    store.revoke_team_token(bob.user_id)
    assert store.resolve_team_session(replacement_session) is None
    assert store.resolve_team_session(alice_session) == alice
    with pytest.raises(TeamAuthenticationError):
        store.create_team_session(replacement)
    assert store.agent_task(operation_id) == before


def test_self_service_revoke_refuses_to_strand_the_last_enrolled_member(tmp_path) -> None:
    store, _bootstrap, member, token = _claimed_team(tmp_path)
    app = create_app(data_dir=tmp_path)
    client = TestClient(app, base_url="https://team.test")
    assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200

    refused = client.post("/api/team/credential/revoke", json={})

    assert refused.status_code == 409
    assert "last enrolled member" in refused.json()["detail"]
    assert client.get("/api/identity").json()["user"]["user_id"] == member.user_id
    rotated = client.post("/api/team/credential/rotate", json={})
    assert rotated.status_code == 200
    assert rotated.json()["token"].startswith("rcp_")
    assert store.space_user(member.user_id) is not None


def test_raw_credentials_never_enter_sqlite_or_task_and_patch_fixtures(tmp_path) -> None:
    store, bootstrap, alice, alice_token = _claimed_team(tmp_path)
    _invitation, invite_code, bob, bob_token = _enroll_invited_member(store, alice.user_id, "Bob")
    session, _member = store.create_team_session(bob_token)
    rotated = store.rotate_team_token(bob.user_id)
    now = store.now()
    project_id = str(uuid.uuid4())
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            home_space_id=store.space_id,
            locator=f"/tmp/{project_id}/research.yaml",
            name="Redaction",
            state_location=f"/tmp/{project_id}/.research",
            state_remote=False,
            added_at=now,
        )
    )
    operation_id = str(uuid.uuid4())
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="refresh",
            status="running",
            request={"source": "team enrollment redaction test"},
            created_at=now,
            updated_at=now,
            status_message="Running.",
            authorized_by=AuthorizedHuman(
                space_id=store.space_id,
                user_id=alice.user_id,
                display_name="Alice",
            ),
        )
    )
    store.record_agent_task_event(operation_id, "Enrollment complete.")
    store.record_agent_task_receipt(operation_id, "test", {"status": "safe"})
    contract = "Use authenticated human attribution."
    store.record_agent_task_contract(
        operation_id,
        "test",
        contract,
        hashlib.sha256(contract.encode()).hexdigest(),
    )
    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"operations": []}), encoding="utf-8")

    durable_bytes = _sqlite_bytes(store.path)
    fixture_bytes = json.dumps(
        {
            "task": store.agent_task(operation_id).model_dump(mode="json"),
            "events": [
                item.model_dump(mode="json") for item in store.agent_task_events(operation_id)
            ],
            "receipts": [
                item.model_dump(mode="json") for item in store.agent_task_receipts(operation_id)
            ],
            "contracts": [
                item.model_dump(mode="json") for item in store.agent_task_contracts(operation_id)
            ],
            "patch": json.loads(patch.read_text(encoding="utf-8")),
        },
        sort_keys=True,
    ).encode()
    for secret in (bootstrap, invite_code, alice_token, bob_token, session, rotated):
        assert secret.encode() not in durable_bytes
        assert secret.encode() not in fixture_bytes


def test_team_authentication_middleware_keeps_only_bootstrap_boundaries_public(tmp_path) -> None:
    store, _bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    app = create_app(data_dir=tmp_path)
    client = TestClient(app, base_url="https://testserver")

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["space_name"] == "Team Lab"
    assert client.get("/").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert (
        client.post(
            "/api/team/enroll", json={"code": "invalid", "display_name": "Alice"}
        ).status_code
        == 401
    )
    assert client.post("/api/team/session/exchange", json={"token": "invalid"}).status_code == 401
    assert (
        client.post(
            "/api/team/devices/pair", json={"code": "AAAA-AAAAAA", "label": "Phone"}
        ).status_code
        == 401
    )
    oversized = "x" * 5000
    for path, body in (
        ("/api/team/enroll", {"code": oversized, "display_name": "Alice"}),
        ("/api/team/session/exchange", {"token": oversized}),
        ("/api/team/devices/pair", {"code": oversized, "label": "Phone"}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "team_auth_request_too_large"

    for path in (
        "/api",
        "/api/identity",
        "/api/projects",
        "/api/team/invitations",
        "/api/team/sessions",
    ):
        response = client.get(path)
        assert response.status_code == 401, (path, response.text)
        assert response.json()["detail"]["code"] == "team_identity_required"
    issued = client.post("/api/team/devices/pairings", json={})
    assert issued.status_code == 401
    assert issued.json()["detail"]["code"] == "team_identity_required"
    assert store.space_users() == []


def test_device_pairing_codes_are_short_lived_single_use_and_bound_to_their_member(
    tmp_path,
) -> None:
    store, _, alice, alice_token = _claimed_team(tmp_path)
    _, _, bob, _bob_token = _enroll_invited_member(store, alice.user_id, "Bob")

    pairing, code = store.create_team_device_pairing(alice.user_id)
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{6}", code)
    assert pairing.created_by == alice.user_id
    assert datetime.fromisoformat(pairing.expires_at) - datetime.fromisoformat(
        pairing.created_at
    ) == timedelta(minutes=TEAM_DEVICE_PAIRING_TTL_MINUTES)
    secret = code.split("-", 1)[1]
    assert secret.encode() not in _sqlite_bytes(store.path)

    with pytest.raises(ValueError):
        store.pair_team_device(code, "   ")
    with pytest.raises(TeamAuthenticationError) as invalid:
        store.pair_team_device(code[:-1] + ("2" if code[-1] != "2" else "3"), "Ada's phone")
    assert invalid.value.code == "device_pairing_code_invalid"

    # A person types the code as they see it: case and separators are forgiven.
    session, member = store.pair_team_device(f" {code.lower().replace('-', ' ')} ", "Ada's phone")
    assert member == alice
    assert store.resolve_team_session(session) == alice
    labels = {item.label for item in store.team_sessions(alice.user_id)}
    assert labels == {"Ada's phone"}
    assert store.team_sessions(bob.user_id) == []

    with pytest.raises(TeamAuthenticationError) as consumed:
        store.pair_team_device(code, "Second phone")
    assert consumed.value.code == "device_pairing_code_consumed"

    # Issuing a new code withdraws the previous unused one, so a member holds one.
    _, first = store.create_team_device_pairing(alice.user_id)
    _, second = store.create_team_device_pairing(alice.user_id)
    with pytest.raises(TeamAuthenticationError) as withdrawn:
        store.pair_team_device(first, "Tablet")
    assert withdrawn.value.code == "device_pairing_code_invalid"

    # Wrong secrets lock only the targeted code.
    wrong = second[:-1] + ("2" if second[-1] != "2" else "3")
    for _ in range(TEAM_CODE_FAILED_ATTEMPT_LIMIT - 1):
        with pytest.raises(TeamAuthenticationError) as attempt:
            store.pair_team_device(wrong, "Tablet")
        assert attempt.value.code == "device_pairing_code_invalid"
    with pytest.raises(TeamAuthenticationError) as locked:
        store.pair_team_device(wrong, "Tablet")
    assert locked.value.code == "device_pairing_code_locked"
    with pytest.raises(TeamAuthenticationError) as still_locked:
        store.pair_team_device(second, "Tablet")
    assert still_locked.value.code == "device_pairing_code_locked"

    _, expiring = store.create_team_device_pairing(alice.user_id)
    with store.connection() as connection:
        connection.execute(
            "UPDATE team_device_pairings SET expires_at = ? WHERE pairing_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), expiring.split("-")[0]),
        )
    with pytest.raises(TeamAuthenticationError) as expired:
        store.pair_team_device(expiring, "Tablet")
    assert expired.value.code == "device_pairing_code_expired"
    assert {item.label for item in store.team_sessions(alice.user_id)} == {"Ada's phone"}

    # A code lives only as long as the session that issued it: revoking that
    # device, or rotating the credential, kills the code with it.
    desktop_session, _ = store.create_team_session(alice_token, label="Desktop")
    pairing, bound = store.create_team_device_pairing(
        alice.user_id, issuing_session=desktop_session
    )
    assert store.team_device_pairing(pairing.pairing_id, alice.user_id).revoked_at is None
    with pytest.raises(KeyError):
        store.team_device_pairing(pairing.pairing_id, bob.user_id)
    store.delete_team_session(desktop_session)
    assert store.team_device_pairing(pairing.pairing_id, alice.user_id).revoked_at is not None
    with pytest.raises(TeamAuthenticationError) as orphaned:
        store.pair_team_device(bound, "Tablet")
    assert orphaned.value.code == "device_pairing_code_invalid"

    rotating_session, _ = store.create_team_session(alice_token, label="Desktop")
    _, rotated_away = store.create_team_device_pairing(
        alice.user_id, issuing_session=rotating_session
    )
    store.rotate_team_token(alice.user_id)
    with pytest.raises(TeamAuthenticationError) as after_rotation:
        store.pair_team_device(rotated_away, "Tablet")
    assert after_rotation.value.code == "device_pairing_code_invalid"


def test_a_device_pairs_with_a_code_and_a_name_and_never_sees_the_member_token(
    tmp_path,
) -> None:
    store, _, alice, alice_token = _claimed_team(tmp_path)
    app = create_app(data_dir=tmp_path)
    desktop = TestClient(app, base_url="https://testserver")
    phone = TestClient(app, base_url="https://testserver")
    assert (
        desktop.post("/api/team/session/exchange", json={"token": alice_token}).status_code == 200
    )

    issued = desktop.post("/api/team/devices/pairings", json={})
    assert issued.status_code == 200
    assert set(issued.json()) == {"pairing_id", "code", "expires_at", "connect_url"}
    code = issued.json()["code"]
    pairing_id = issued.json()["pairing_id"]
    assert alice_token not in issued.text
    waiting = desktop.get(f"/api/team/devices/pairings/{pairing_id}")
    assert waiting.status_code == 200
    assert waiting.json() == {
        "pairing_id": pairing_id,
        "expires_at": issued.json()["expires_at"],
        "status": "waiting",
    }
    assert desktop.get("/api/team/devices/pairings/ZZZZ").status_code == 404

    assert phone.get("/api/identity").status_code == 401
    unnamed = phone.post("/api/team/devices/pair", json={"code": code})
    assert unnamed.status_code == 422
    blank = phone.post("/api/team/devices/pair", json={"code": code, "label": "  "})
    assert blank.status_code == 422
    paired = phone.post("/api/team/devices/pair", json={"code": code, "label": "Ada's iPhone"})
    assert paired.status_code == 200
    assert paired.json()["user"]["user_id"] == alice.user_id
    assert alice_token not in paired.text
    cookie = paired.headers["set-cookie"].lower()
    assert cookie.startswith("__host-rcp_session=rcp_session_")
    assert "httponly" in cookie and "secure" in cookie
    assert phone.get("/api/identity").status_code == 200

    replay = TestClient(app, base_url="https://testserver").post(
        "/api/team/devices/pair", json={"code": code, "label": "Someone else"}
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "device_pairing_code_consumed"
    assert desktop.get(f"/api/team/devices/pairings/{pairing_id}").json()["status"] == "consumed"

    # A code issued by the phone dies with the phone's session.
    phone_issued = phone.post("/api/team/devices/pairings", json={}).json()

    listed = desktop.get("/api/team/sessions")
    assert listed.status_code == 200
    by_label = {item["label"]: item for item in listed.json()}
    assert set(by_label) == {"Unnamed device", "Ada's iPhone"}
    assert by_label["Unnamed device"]["is_current"] is True
    assert by_label["Ada's iPhone"]["can_revoke"] is True
    assert code not in listed.text

    phone_session_id = by_label["Ada's iPhone"]["session_id"]
    revoked = desktop.post(f"/api/team/sessions/{phone_session_id}/revoke", json={})
    assert revoked.status_code == 200
    assert phone.get("/api/identity").status_code == 401
    assert store.team_sessions(alice.user_id)[0].label == "Unnamed device"
    orphaned = desktop.get(f"/api/team/devices/pairings/{phone_issued['pairing_id']}")
    assert orphaned.json()["status"] == "revoked"
    redeemed = TestClient(app, base_url="https://testserver").post(
        "/api/team/devices/pair", json={"code": phone_issued["code"], "label": "Intruder"}
    )
    assert redeemed.status_code == 401


def test_authenticated_team_mutations_reject_forms_and_cross_origin_json(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    client = TestClient(create_app(data_dir=tmp_path), base_url="https://team.test")
    enrollment = client.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"}
    ).json()
    token = enrollment["token"]
    assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200

    mutation_paths = (
        f"/api/team/sessions/{uuid.uuid4()}/revoke",
        "/api/team/session/logout",
        "/api/team/invitations",
        "/api/team/credential/rotate",
        "/api/team/credential/revoke",
    )
    for path in mutation_paths:
        forged_form = client.post(
            path,
            data={"forged": "true"},
            headers={"Origin": "https://team.test"},
        )
        assert forged_form.status_code == 415
        assert forged_form.json()["detail"]["code"] == "team_json_required"

        forged_json = client.post(
            path,
            json={},
            headers={"Origin": "https://attacker.test"},
        )
        assert forged_json.status_code == 403
        assert forged_json.json()["detail"]["code"] == "team_origin_invalid"
        assert "access-control-allow-origin" not in forged_json.headers

        cors_allowed_but_team_invalid = client.post(
            path,
            json={},
            headers={"Origin": "http://localhost:5173"},
        )
        assert cors_allowed_but_team_invalid.status_code == 403
        assert cors_allowed_but_team_invalid.json()["detail"]["code"] == ("team_origin_invalid")
        assert cors_allowed_but_team_invalid.headers["access-control-allow-origin"] == (
            "http://localhost:5173"
        )

    assert client.get("/api/identity").status_code == 200
    assert client.get("/api/team/invitations").json() == []
    fresh_client = TestClient(create_app(data_dir=tmp_path), base_url="https://team.test")
    assert fresh_client.post("/api/team/session/exchange", json={"token": token}).status_code == 200
    assert store.space_kind == "team"
    assert len(store.space_users()) == 1


def test_authenticated_team_mutation_accepts_the_desktop_https_origin_over_its_tunnel(
    tmp_path,
) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    host = "rcp-11111111111141118111111111111111.rcp.localhost:57276"
    client = TestClient(create_app(data_dir=tmp_path), base_url=f"http://{host}")
    token = client.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"}
    ).json()["token"]
    exchange = client.post("/api/team/session/exchange", json={"token": token})
    cookie = exchange.headers["set-cookie"].partition(";")[0]

    invitation = client.post(
        "/api/team/invitations",
        json={},
        headers={"Cookie": cookie, "Origin": f"https://{host}"},
    )

    assert invitation.status_code == 200
    assert invitation.json()["space_name"] == "Team Lab"
    assert len(store.team_invitations(invitation.json()["invitation"]["created_by"])) == 1


def test_authenticated_team_attachment_upload_keeps_its_bounded_multipart_contract(
    manifest, tmp_path
) -> None:
    data_dir = tmp_path / "data"
    store, bootstrap = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Team Lab")
    app = create_app(str(manifest.path), data_dir=data_dir)
    client = TestClient(app, base_url="https://team.test")
    token = client.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"}
    ).json()["token"]
    assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200
    project_id = app.state.default_project_id
    chat_id = str(uuid.uuid4())
    client_id = str(uuid.uuid4())
    path = f"/api/projects/{project_id}/chats/{chat_id}/attachments"

    forged = client.post(
        path,
        data={"client_id": client_id},
        files={"file": ("notes.txt", b"temporary input", "text/plain")},
        headers={"Origin": "https://attacker.test"},
    )
    assert forged.status_code == 403
    assert forged.json()["detail"]["code"] == "team_origin_invalid"

    uploaded = client.post(
        path,
        data={"client_id": client_id},
        files={"file": ("notes.txt", b"temporary input", "text/plain")},
        headers={"Origin": "https://team.test"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["attachment"]["name"] == "notes.txt"
    assert store.space_kind == "team"


def test_enrollment_exchange_and_session_cookie_make_the_team_api_usable(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    app = create_app(data_dir=tmp_path)
    client = TestClient(app, base_url="https://testserver")

    enrolled = client.post("/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"})
    assert enrolled.status_code == 200
    enrollment_payload = enrolled.json()
    token = enrollment_payload.pop("token")
    member = enrollment_payload["identity"]["user"]
    assert member["display_name"] == "Alice"
    assert bootstrap not in enrolled.text
    assert client.get("/api/identity").status_code == 401

    exchanged = client.post("/api/team/session/exchange", json={"token": token})
    assert exchanged.status_code == 200
    assert token not in exchanged.text
    cookie = exchanged.headers["set-cookie"].lower()
    assert cookie.startswith("__host-rcp_session=rcp_session_")
    for attribute in (
        "httponly",
        "secure",
        "samesite=lax",
        "path=/",
        "max-age=1209600",
    ):
        assert attribute in cookie
    # The __Host- prefix is only honoured when the cookie carries no Domain,
    # which is what keeps a team session from being scoped to a sibling host.
    assert "domain=" not in cookie

    identity = client.get("/api/identity")
    assert identity.status_code == 200
    assert identity.json()["user"]["user_id"] == member["user_id"]
    assert "max-age=1209600" in identity.headers["set-cookie"].lower()
    assert client.get("/api/projects").status_code == 200

    restarted = TestClient(create_app(data_dir=tmp_path), base_url="https://testserver")
    restarted_identity = restarted.get(
        "/api/identity",
        headers={"Cookie": f"__Host-rcp_session={client.cookies.get('__Host-rcp_session')}"},
    )
    assert restarted_identity.status_code == 200
    assert restarted_identity.json()["user"]["user_id"] == member["user_id"]
    invitation = client.post("/api/team/invitations", json={})
    assert invitation.status_code == 200
    assert invitation.json()["space_name"] == "Team Lab"
    assert invitation.json()["invitation"]["expires_at"]
    assert invitation.json()["code"].startswith("rcp_invite_")
    listed = client.get("/api/team/invitations")
    assert listed.status_code == 200
    assert [item["invitation_id"] for item in listed.json()] == [
        invitation.json()["invitation"]["invitation_id"]
    ]
    assert invitation.json()["code"] not in listed.text
    assert AppStore(store.path).space_user(member["user_id"]) is not None


@pytest.mark.parametrize("selected", ["1", "2", "3", "4"])
def test_native_team_handshake_echoes_one_protocol_and_rejects_another(tmp_path, selected) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    metadata = ServerMetadata.create(
        tmp_path,
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        running_commit="a" * 40,
        web_build_id=f"sha256:{'b' * 64}",
    )
    client = TestClient(
        create_app(data_dir=tmp_path, instance_metadata=metadata),
        base_url="https://testserver",
    )
    header = "RCP-Team-Shell-Protocol"

    missing = client.post("/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"})
    assert missing.status_code == 426
    assert missing.json()["detail"]["action"].startswith("Update and rebuild RCP desktop")

    mismatch = client.post(
        "/api/team/enroll",
        json={"code": bootstrap, "display_name": "Alice"},
        headers={header: "5"},
    )
    assert mismatch.status_code == 426
    assert mismatch.json()["detail"] == {
        "code": "team_shell_protocol_mismatch",
        "message": "The selected team-shell protocol is not supported by this server.",
        "server_protocol": {"minimum": 1, "maximum": 4},
        "action": (
            "Update and rebuild RCP desktop from merged main, or have the server operator "
            "install a compatible promoted RCP release."
        ),
    }

    enrolled = client.post(
        "/api/team/enroll",
        json={"code": bootstrap, "display_name": "Alice"},
        headers={header: selected},
    )
    assert enrolled.status_code == 200
    assert enrolled.headers[header] == selected
    token = enrolled.json()["token"]

    exchanged = client.post(
        "/api/team/session/exchange",
        json={"token": token},
        headers={header: selected},
    )
    assert exchanged.status_code == 200
    assert exchanged.headers[header] == selected

    projects = client.get("/api/projects", headers={header: selected})
    assert projects.status_code == 200
    assert projects.headers[header] == selected
    assert len(store.space_users()) == 1


def test_revoking_an_invitation_over_http_blocks_enrollment(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    app = create_app(data_dir=tmp_path)
    alice = TestClient(app, base_url="https://testserver")
    bob = TestClient(app, base_url="https://testserver")
    alice_token = alice.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"}
    ).json()["token"]
    assert alice.post("/api/team/session/exchange", json={"token": alice_token}).status_code == 200
    issued = alice.post("/api/team/invitations", json={}).json()
    invitation_id = issued["invitation"]["invitation_id"]

    revoked = alice.post(f"/api/team/invitations/{invitation_id}/revoke", json={})
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["status_label"] == "Revoked"
    assert revoked.json()["can_revoke"] is False

    refused = bob.post(
        "/api/team/enroll", json={"code": issued["code"], "display_name": "Stranger"}
    )
    assert refused.status_code == 401
    assert (
        store.team_invitations(alice.get("/api/identity").json()["user"]["user_id"])[0].revoked_at
        is not None
    )

    missing = alice.post(f"/api/team/invitations/{uuid.uuid4()}/revoke", json={})
    assert missing.status_code == 404


def test_team_invitation_projection_names_members_and_human_states(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    app = create_app(data_dir=tmp_path)
    alice = TestClient(app, base_url="https://testserver")
    bob = TestClient(app, base_url="https://testserver")
    alice_token = alice.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Alice"}
    ).json()["token"]
    assert alice.post("/api/team/session/exchange", json={"token": alice_token}).status_code == 200

    joined = alice.post("/api/team/invitations", json={}).json()
    waiting = alice.post("/api/team/invitations", json={}).json()
    expired = alice.post("/api/team/invitations", json={}).json()
    locked = alice.post("/api/team/invitations", json={}).json()
    assert waiting["invitation"]["status"] == "waiting"
    assert waiting["invitation"]["status_label"] == "Waiting for someone to join"
    assert waiting["invitation"]["can_revoke"] is True

    enrolled = bob.post(
        "/api/team/enroll",
        json={"code": joined["code"], "display_name": "Bob Collaborator"},
    )
    assert enrolled.status_code == 200
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "UPDATE team_invitations SET expires_at = ? WHERE invitation_id = ?",
            (expired_at, expired["invitation"]["invitation_id"]),
        )
        connection.execute(
            "UPDATE team_invitations SET locked_at = ? WHERE invitation_id = ?",
            (store.now(), locked["invitation"]["invitation_id"]),
        )

    projected = {item["invitation_id"]: item for item in alice.get("/api/team/invitations").json()}
    assert projected[joined["invitation"]["invitation_id"]]["status_label"] == (
        "Bob Collaborator joined"
    )
    assert projected[joined["invitation"]["invitation_id"]]["can_revoke"] is False
    assert projected[waiting["invitation"]["invitation_id"]]["status_label"] == (
        "Waiting for someone to join"
    )
    expired_projection = projected[expired["invitation"]["invitation_id"]]
    assert expired_projection["status"] == "expired"
    assert expired_projection["status_label"] == "Expired"
    assert expired_projection["can_revoke"] is False
    assert projected[locked["invitation"]["invitation_id"]]["status_label"] == (
        "Locked after failed attempts"
    )
    assert projected[locked["invitation"]["invitation_id"]]["can_revoke"] is True


def test_team_members_see_only_their_invitations_and_cannot_target_credentials(
    tmp_path,
) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    app = create_app(data_dir=tmp_path)
    alice_client = TestClient(app, base_url="https://testserver")
    bob_client = TestClient(app, base_url="https://testserver")
    alice_enrollment = alice_client.post(
        "/api/team/enroll", json={"code": bootstrap, "display_name": "Same name"}
    ).json()
    alice_id = alice_enrollment["identity"]["user"]["user_id"]
    alice_token = alice_enrollment["token"]
    assert (
        alice_client.post("/api/team/session/exchange", json={"token": alice_token}).status_code
        == 200
    )
    alice_invitation = alice_client.post("/api/team/invitations", json={}).json()
    bob_enrollment = bob_client.post(
        "/api/team/enroll",
        json={"code": alice_invitation["code"], "display_name": "Same name"},
    ).json()
    bob_id = bob_enrollment["identity"]["user"]["user_id"]
    bob_token = bob_enrollment["token"]
    assert alice_id != bob_id
    assert (
        bob_client.post("/api/team/session/exchange", json={"token": bob_token}).status_code == 200
    )

    assert bob_client.get("/api/team/invitations").json() == []
    bob_invitation = bob_client.post("/api/team/invitations", json={}).json()
    assert {item["invitation_id"] for item in alice_client.get("/api/team/invitations").json()} == {
        alice_invitation["invitation"]["invitation_id"]
    }
    assert {item["invitation_id"] for item in bob_client.get("/api/team/invitations").json()} == {
        bob_invitation["invitation"]["invitation_id"]
    }
    renamed = bob_client.patch("/api/team/space", json={"name": "Renamed by Bob"})
    assert renamed.json() == {"space_name": "Renamed by Bob", "access_url": None}
    assert alice_client.get("/api/identity").json()["space_name"] == "Renamed by Bob"

    rotated = alice_client.post("/api/team/credential/rotate", json={})
    assert rotated.status_code == 200
    alice_replacement = rotated.json()["token"]
    assert alice_client.get("/api/identity").status_code == 401
    assert bob_client.get("/api/identity").json()["user"]["user_id"] == bob_id
    assert (
        TestClient(app, base_url="https://testserver")
        .post("/api/team/session/exchange", json={"token": bob_token})
        .status_code
        == 200
    )
    assert (
        TestClient(app, base_url="https://testserver")
        .post("/api/team/session/exchange", json={"token": alice_token})
        .status_code
        == 401
    )

    assert (
        alice_client.post(
            "/api/team/session/exchange", json={"token": alice_replacement}
        ).status_code
        == 200
    )
    revoked = alice_client.post("/api/team/credential/revoke", json={})
    assert revoked.status_code == 200
    assert alice_client.get("/api/identity").status_code == 401
    assert bob_client.get("/api/identity").json()["user"]["user_id"] == bob_id
    assert store.space_user(alice_id).identity_kind == "team_member"
    assert store.space_user(bob_id).identity_kind == "team_member"


def test_trusted_principal_resolver_remains_a_supported_team_authentication_path(
    tmp_path,
) -> None:
    store, _bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team Lab")
    first = store.preprovision_team_member("Resolver member")
    second = store.preprovision_team_member("Other member")
    selected = [first.user_id]
    app = create_app(
        data_dir=tmp_path,
        trusted_principal_resolver=lambda _request, _store: selected[0],
    )
    client = TestClient(app)

    assert client.get("/api/identity").json()["user"]["user_id"] == first.user_id
    selected[0] = second.user_id
    assert client.get("/api/identity").json()["user"]["user_id"] == second.user_id
    assert client.post("/api/team/invitations").status_code == 200


def test_personal_space_keeps_its_local_owner_without_team_authentication(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    owner = app.state.background_tasks.store.local_owner
    assert owner is not None

    identity = client.get("/api/identity")
    assert identity.status_code == 200
    assert identity.json()["space_kind"] == "personal"
    assert identity.json()["space_name"] is None
    assert identity.json()["user"]["user_id"] == owner.user_id
    assert client.get("/api/projects").status_code == 200
    for path, body in (
        ("/api/team/enroll", {"code": "unused", "display_name": "Person"}),
        ("/api/team/session/exchange", {"token": "rcp_unused"}),
        (f"/api/team/sessions/{uuid.uuid4()}/revoke", {}),
    ):
        assert client.post(path, json=body).status_code == 404
    assert client.get("/api/team/sessions").status_code == 404
    assert app.state.background_tasks.store.local_owner == owner


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Lab-Server.tail1234.ts.net", "https://lab-server.tail1234.ts.net"),
        (" https://lab.example.org/ ", "https://lab.example.org"),
        ("https://lab.example.org:8443", "https://lab.example.org:8443"),
        ("https://[2001:DB8::1]:8443/", "https://[2001:db8::1]:8443"),
        ("https://[fd7a:115c:a1e0::c201:27cb]", "https://[fd7a:115c:a1e0::c201:27cb]"),
    ],
)
def test_access_addresses_normalize_to_one_https_origin(raw, expected) -> None:
    assert normalize_space_access_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://lab.example.org",
        "https://user:secret@lab.example.org",
        "https://lab.example.org/team",
        "https://lab.example.org/?x=1",
        "https://lab.example.org/#pair",
        "lab.example.org",
        "https://" + "a" * 200 + ".org",
        "https://lab example.org",
        "https://lab.example.org\\extra",
        "https://lab_server.example.org",
        "https://-lab.example.org",
        "https://999.1.1.1",
        "https://[2001:db8::zz]",
        "https://lab.example.org:99999",
    ],
)
def test_access_addresses_reject_anything_but_an_https_origin(raw) -> None:
    with pytest.raises(ValueError):
        normalize_space_access_url(raw)


def test_the_access_address_is_operator_set_read_only_and_links_codes(
    tmp_path, monkeypatch
) -> None:
    store, _, _alice, alice_token = _claimed_team(tmp_path)
    app = create_app(data_dir=tmp_path)
    desktop = TestClient(app, base_url="https://testserver")
    assert (
        desktop.post("/api/team/session/exchange", json={"token": alice_token}).status_code == 200
    )

    monkeypatch.delenv("RCP_TEAM_ACCESS_URL", raising=False)
    monkeypatch.setattr("rcp.team_access.INSTALLED_TEAM_CONFIG_PATH", tmp_path / "team.toml")
    assert desktop.get("/api/team/space").json() == {"space_name": "Team Lab", "access_url": None}
    assert desktop.post("/api/team/devices/pairings", json={}).json()["connect_url"] is None
    # Members cannot set it; the operator does, in the server configuration.
    refused = desktop.patch("/api/team/space", json={"access_url": "https://lab.ts.net"})
    assert refused.status_code == 422

    monkeypatch.setenv("RCP_TEAM_ACCESS_URL", "https://Lab.tail1234.ts.net/")
    assert desktop.get("/api/team/space").json() == {
        "space_name": "Team Lab",
        "access_url": "https://lab.tail1234.ts.net",
    }
    issued = desktop.post("/api/team/devices/pairings", json={}).json()
    assert issued["connect_url"] == f"https://lab.tail1234.ts.net/#pair={issued['code']}"
    renamed = desktop.patch("/api/team/space", json={"name": "Renamed Lab"})
    assert renamed.json() == {
        "space_name": "Renamed Lab",
        "access_url": "https://lab.tail1234.ts.net",
    }

    monkeypatch.setenv("RCP_TEAM_ACCESS_URL", "http://not-https.example")
    assert desktop.get("/api/team/space").json()["access_url"] is None
    assert store.space_name == "Renamed Lab"

    # An installed server reads the operator's team.toml; a broken file is no address.
    monkeypatch.delenv("RCP_TEAM_ACCESS_URL")
    monkeypatch.setattr(
        "rcp.server_ops.config._expected_config_ownership", lambda: (os.getuid(), os.getgid())
    )
    (tmp_path / "team.toml").write_text(
        render_team_access_config(
            ServerTeamConfig(access_url="https://WTH-gpu-01.tail1234.ts.net/")
        )
    )
    (tmp_path / "team.toml").chmod(0o640)
    assert desktop.get("/api/team/space").json()["access_url"] == (
        "https://wth-gpu-01.tail1234.ts.net"
    )
    (tmp_path / "team.toml").write_text("access_url = 'ftp://nope'\n")
    assert desktop.get("/api/team/space").json()["access_url"] is None
