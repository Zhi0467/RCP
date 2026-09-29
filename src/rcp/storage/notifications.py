"""Operational notification state; canonical graph state is never stored here."""

from __future__ import annotations

import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from rcp.limits import TEAM_CODE_FAILED_ATTEMPT_LIMIT, TEAM_DEVICE_PAIRING_TTL_MINUTES
from rcp.storage.models import _new_device_pairing_code, _parse_device_pairing_code

NOTIFICATION_DEFAULTS = {
    "proposal": True,
    "decision": True,
    "blocker": True,
    "episode_needs_action": True,
    "episode_finished": False,
}


def migrate_notifications(connection: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE IF NOT EXISTS notification_preferences (project_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, kind TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), "
        "PRIMARY KEY(project_id,user_id,kind))",
        "CREATE TABLE IF NOT EXISTS notification_devices (device_id TEXT PRIMARY KEY, "
        "user_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('desktop','web_push')), "
        "session_id TEXT REFERENCES team_sessions(session_id) ON DELETE CASCADE, "
        "created_at TEXT NOT NULL, last_status TEXT NOT NULL DEFAULT 'on')",
        "CREATE UNIQUE INDEX IF NOT EXISTS notification_desktop_owner ON "
        "notification_devices(user_id,coalesce(session_id,'')) WHERE kind='desktop'",
        "CREATE TABLE IF NOT EXISTS notification_outbox (device_id TEXT NOT NULL REFERENCES "
        "notification_devices(device_id) ON DELETE CASCADE, notification_id TEXT NOT NULL, "
        "project_id TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL, "
        "reason TEXT NOT NULL, project_name TEXT NOT NULL, deep_link TEXT NOT NULL, "
        "created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, "
        "last_status TEXT NOT NULL DEFAULT 'pending', observed_health TEXT, "
        "observed_blocked_reason TEXT, PRIMARY KEY(device_id,notification_id))",
        "CREATE TABLE IF NOT EXISTS notification_graph_markers (project_id TEXT NOT NULL, "
        "target TEXT NOT NULL, revision INTEGER NOT NULL, transition_id TEXT, attention_json TEXT "
        "NOT NULL, baseline_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(project_id,target))",
        "CREATE TABLE IF NOT EXISTS notification_episode_observations (episode_id TEXT PRIMARY KEY, "
        "project_id TEXT NOT NULL, health TEXT NOT NULL, blocked_reason TEXT, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS notification_project_baselines (project_id TEXT PRIMARY KEY, "
        "baseline_at TEXT NOT NULL)",
        # One space-wide VAPID signing key. Living in SQLite makes it atomic,
        # under the data directory, and part of every backup snapshot.
        "CREATE TABLE IF NOT EXISTS notification_vapid_key (singleton INTEGER PRIMARY KEY "
        "CHECK(singleton=1), private_pem TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS notification_web_push_subscriptions (device_id TEXT PRIMARY KEY "
        "REFERENCES notification_devices(device_id) ON DELETE CASCADE, endpoint TEXT NOT NULL UNIQUE, "
        "p256dh TEXT NOT NULL, auth TEXT NOT NULL, origin TEXT NOT NULL, "
        "opens_links INTEGER NOT NULL CHECK(opens_links IN (0,1)))",
        # Personal-space phone codes: the team code format, expiry, and lockout,
        # but redeeming one only registers a notify-only phone.
        "CREATE TABLE IF NOT EXISTS notification_phone_pairings (pairing_id TEXT PRIMARY KEY, "
        "code_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "consumed_at TEXT, failed_attempts INTEGER NOT NULL DEFAULT 0, locked_at TEXT, "
        "revoked_at TEXT)",
    )
    for statement in statements:
        connection.execute(statement)


class NotificationStoreMixin:
    def notification_preferences(self, project_id: str, user_id: str) -> dict[str, bool]:
        with self.connection() as connection:
            return self._notification_preferences(connection, project_id, user_id)

    @staticmethod
    def _notification_preferences(
        connection: sqlite3.Connection, project_id: str, user_id: str
    ) -> dict[str, bool]:
        return NOTIFICATION_DEFAULTS | {
            row["kind"]: bool(row["enabled"])
            for row in connection.execute(
                "SELECT kind,enabled FROM notification_preferences WHERE project_id=? AND user_id=?",
                (project_id, user_id),
            )
        }

    def set_notification_preferences(
        self, project_id: str, user_id: str, preferences: dict[str, bool]
    ) -> dict[str, bool]:
        if set(preferences) - NOTIFICATION_DEFAULTS.keys() or any(
            type(value) is not bool for value in preferences.values()
        ):
            raise ValueError("invalid notification preferences")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Member-owned, so a project work fence never blocks an opt-out.
            member = connection.execute(
                "SELECT 1 FROM project_members m JOIN projects p ON p.project_id=m.project_id "
                "JOIN space_users u ON u.user_id=m.user_id WHERE m.project_id=? AND m.user_id=? "
                "AND p.retired_at IS NULL AND u.removal_started_at IS NULL AND u.removed_at IS NULL",
                (project_id, user_id),
            ).fetchone()
            if member is None:
                raise ValueError("notification preferences require project membership")
            connection.executemany(
                "INSERT INTO notification_preferences VALUES (?,?,?,?) ON CONFLICT "
                "(project_id,user_id,kind) DO UPDATE SET enabled=excluded.enabled",
                [(project_id, user_id, kind, enabled) for kind, enabled in preferences.items()],
            )
            return self._notification_preferences(connection, project_id, user_id)

    def register_notification_device(
        self, user_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT * FROM space_users WHERE user_id=?", (user_id,)
            ).fetchone()
            if (
                user is None
                or user["removal_started_at"] is not None
                or user["removed_at"] is not None
            ):
                raise ValueError("notification member is inactive")
            if user["identity_kind"] == "team_member":
                session = connection.execute(
                    "SELECT 1 FROM team_sessions WHERE session_id=? AND user_id=? AND expires_at>?",
                    (session_id, user_id, self.now()),
                ).fetchone()
                if session is None:
                    raise ValueError("notification device requires an active session")
            elif session_id is not None:
                raise ValueError("personal notification device cannot own a team session")
            existing = connection.execute(
                "SELECT * FROM notification_devices WHERE user_id=? AND session_id IS ? "
                "AND kind='desktop'",
                (user_id, session_id),
            ).fetchone()
            if existing:
                return dict(existing)
            device_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO notification_devices(device_id,user_id,kind,session_id,created_at) "
                "VALUES (?,?,'desktop',?,?)",
                (device_id, user_id, session_id, self.now()),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM notification_devices WHERE device_id=?",
                    (device_id,),
                ).fetchone()
            )

    def notification_vapid_key(self) -> str:
        """Return the VAPID key PEM, creating it only while no phone depends on one."""
        from rcp.web_push import VapidKey

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT private_pem FROM notification_vapid_key").fetchone()
            if row is not None:
                return row[0]
            if connection.execute(
                "SELECT 1 FROM notification_devices WHERE kind='web_push'"
            ).fetchone():
                # Devices signed against a lost key must subscribe again.
                raise RuntimeError("the VAPID key is missing while phone subscriptions exist")
            pem = VapidKey.generate().to_pem()
            connection.execute(
                "INSERT INTO notification_vapid_key VALUES (1,?,?)", (pem, self.now())
            )
            return pem

    def register_web_push_device(
        self,
        user_id: str,
        *,
        session_id: str | None,
        endpoint: str,
        p256dh: str,
        auth: str,
        origin: str,
        opens_links: bool,
    ) -> dict[str, Any]:
        """Replace this session's phone, or add a notify-only phone, for one endpoint."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM notification_vapid_key").fetchone() is None:
                raise RuntimeError("the VAPID key must exist before a phone subscribes")
            user = connection.execute(
                "SELECT * FROM space_users WHERE user_id=?", (user_id,)
            ).fetchone()
            if user is None or user["removal_started_at"] or user["removed_at"]:
                raise ValueError("notification member is inactive")
            if user["identity_kind"] == "team_member":
                if (
                    session_id is None
                    or connection.execute(
                        "SELECT 1 FROM team_sessions WHERE session_id=? AND user_id=? "
                        "AND expires_at>?",
                        (session_id, user_id, self.now()),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("a phone subscription requires an active session")
                same = connection.execute(
                    "SELECT d.device_id FROM notification_devices d JOIN "
                    "notification_web_push_subscriptions w ON w.device_id=d.device_id "
                    "WHERE d.session_id=? AND w.endpoint=?",
                    (session_id, endpoint),
                ).fetchone()
                if same is not None:
                    # A revisit re-registers; keep the device so queued items survive.
                    connection.execute(
                        "UPDATE notification_web_push_subscriptions SET p256dh=?,auth=?,origin=? "
                        "WHERE device_id=?",
                        (p256dh, auth, origin, same["device_id"]),
                    )
                    return dict(
                        connection.execute(
                            "SELECT * FROM notification_devices WHERE device_id=?",
                            (same["device_id"],),
                        ).fetchone()
                    )
                connection.execute(
                    "DELETE FROM notification_devices WHERE kind='web_push' AND session_id=?",
                    (session_id,),
                )
            elif session_id is not None:
                raise ValueError("personal notification device cannot own a team session")
            return self._insert_web_push_device(
                connection, user_id, session_id, endpoint, p256dh, auth, origin, opens_links
            )

    def _insert_web_push_device(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        session_id: str | None,
        endpoint: str,
        p256dh: str,
        auth: str,
        origin: str,
        opens_links: bool,
    ) -> dict[str, Any]:
        connection.execute(
            "DELETE FROM notification_devices WHERE device_id IN (SELECT device_id FROM "
            "notification_web_push_subscriptions WHERE endpoint=?)",
            (endpoint,),
        )
        device_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO notification_devices(device_id,user_id,kind,session_id,created_at) "
            "VALUES (?,?,'web_push',?,?)",
            (device_id, user_id, session_id, self.now()),
        )
        connection.execute(
            "INSERT INTO notification_web_push_subscriptions VALUES (?,?,?,?,?,?)",
            (device_id, endpoint, p256dh, auth, origin, int(opens_links)),
        )
        return dict(
            connection.execute(
                "SELECT * FROM notification_devices WHERE device_id=?", (device_id,)
            ).fetchone()
        )

    def create_notification_phone_pairing(self) -> tuple[str, str]:
        """Issue the personal space's one live phone code; return it and its expiry."""
        now = self.now()
        expires_at = (
            datetime.fromisoformat(now) + timedelta(minutes=TEAM_DEVICE_PAIRING_TTL_MINUTES)
        ).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._space_kind_from_connection(connection) != "personal":
                raise ValueError("Only a personal space pairs notify-only phones.")
            connection.execute(
                "UPDATE notification_phone_pairings SET revoked_at=? "
                "WHERE consumed_at IS NULL AND revoked_at IS NULL",
                (now,),
            )
            for _ in range(8):
                code, pairing_id, code_hash = _new_device_pairing_code()
                if (
                    connection.execute(
                        "SELECT 1 FROM notification_phone_pairings WHERE pairing_id=?",
                        (pairing_id,),
                    ).fetchone()
                    is None
                ):
                    break
            else:  # pragma: no cover - eight collisions in a million-slot space
                raise RuntimeError("RCP could not allocate a phone pairing code.")
            connection.execute(
                "INSERT INTO notification_phone_pairings(pairing_id,code_hash,created_at,expires_at) "
                "VALUES (?,?,?,?)",
                (pairing_id, code_hash, now, expires_at),
            )
        return code, expires_at

    def notification_phone_pairing_live(self) -> bool:
        with self.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM notification_phone_pairings WHERE consumed_at IS NULL AND "
                    "revoked_at IS NULL AND locked_at IS NULL AND expires_at>?",
                    (self.now(),),
                ).fetchone()
                is not None
            )

    def redeem_notification_phone_pairing(
        self, code: str, *, endpoint: str, p256dh: str, auth: str, origin: str
    ) -> dict[str, Any]:
        """Consume a code and register its notify-only phone in one transaction.

        Raises ``ValueError`` with ``invalid``, ``consumed``, ``locked``, or
        ``expired``; a wrong secret still counts toward the lockout.
        """
        parsed = _parse_device_pairing_code(code)
        if parsed is None:
            raise ValueError("invalid")
        pairing_id, supplied_hash = parsed
        now = self.now()
        error = None
        device = None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_phone_pairings WHERE pairing_id=?", (pairing_id,)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                error = "invalid"
            elif row["consumed_at"] is not None:
                error = "consumed"
            elif row["locked_at"] is not None:
                error = "locked"
            elif row["expires_at"] <= now:
                error = "expired"
            elif not hmac.compare_digest(row["code_hash"], supplied_hash):
                failed = int(row["failed_attempts"]) + 1
                locked_at = now if failed >= TEAM_CODE_FAILED_ATTEMPT_LIMIT else None
                connection.execute(
                    "UPDATE notification_phone_pairings SET failed_attempts=?,locked_at=? "
                    "WHERE pairing_id=?",
                    (failed, locked_at, pairing_id),
                )
                error = "locked" if locked_at else "invalid"
            elif connection.execute("SELECT 1 FROM notification_vapid_key").fetchone() is None:
                raise RuntimeError("the VAPID key must exist before a phone subscribes")
            else:
                connection.execute(
                    "UPDATE notification_phone_pairings SET consumed_at=? WHERE pairing_id=?",
                    (now, pairing_id),
                )
                device = self._insert_web_push_device(
                    connection,
                    self.local_owner.user_id,
                    None,
                    endpoint,
                    p256dh,
                    auth,
                    origin,
                    False,
                )
        if error is not None:
            raise ValueError(error)
        assert device is not None
        return device

    def web_push_subscription(self, device_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM notification_web_push_subscriptions WHERE device_id=?",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def defer_notification(
        self, device_id: str, notification_id: str, next_attempt_at: str
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE notification_outbox SET next_attempt_at=max(next_attempt_at,?) "
                "WHERE device_id=? AND notification_id=?",
                (next_attempt_at, device_id, notification_id),
            )

    def set_notification_device_status(self, device_id: str, status: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE notification_devices SET last_status=? WHERE device_id=?",
                (status, device_id),
            )

    def fail_notification(self, device_id: str, notification_id: str) -> None:
        """Drop an item the push service refused for good; the device shows it."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM notification_outbox WHERE device_id=? AND notification_id=?",
                (device_id, notification_id),
            )
            connection.execute(
                "UPDATE notification_devices SET last_status='delivery_failed' WHERE device_id=?",
                (device_id,),
            )

    def notification_device(self, device_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM notification_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            return dict(row) if row else None

    def notification_devices(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM notification_devices")]

    def notification_outbox(self, device_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM notification_outbox WHERE (? IS NULL OR device_id=?) ORDER BY created_at,notification_id",
                    (device_id, device_id),
                )
            ]

    def delete_notification_device(self, device_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM notification_devices WHERE device_id=?", (device_id,))

    def drop_notification(self, device_id: str, notification_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM notification_outbox WHERE device_id=? AND notification_id=?",
                (device_id, notification_id),
            )

    def _notification_device_active(
        self, connection: sqlite3.Connection, device: sqlite3.Row
    ) -> bool:
        user = connection.execute(
            "SELECT * FROM space_users WHERE user_id=?", (device["user_id"],)
        ).fetchone()
        active = (
            user is not None and user["removal_started_at"] is None and user["removed_at"] is None
        )
        if active and user["identity_kind"] == "team_member":
            active = (
                connection.execute(
                    "SELECT 1 FROM team_sessions WHERE session_id=? AND user_id=? AND expires_at>?",
                    (device["session_id"], device["user_id"], self.now()),
                ).fetchone()
                is not None
            )
        if not active:
            connection.execute(
                "DELETE FROM notification_devices WHERE device_id=?", (device["device_id"],)
            )
        return active

    def _notification_allowed(
        self, connection: sqlite3.Connection, device: sqlite3.Row, project_id: str, kind: str
    ) -> bool:
        member = connection.execute(
            """
            SELECT 1 FROM project_members m JOIN projects p ON p.project_id=m.project_id WHERE
            m.project_id=? AND m.user_id=? AND p.retired_at IS NULL
            """,
            (project_id, device["user_id"]),
        ).fetchone()
        return (
            member is not None
            and self._notification_project_active(connection, project_id)
            and self._notification_preferences(connection, project_id, device["user_id"])[kind]
        )

    def enqueue_notification(
        self,
        connection: sqlite3.Connection,
        *,
        notification_id: str,
        project_id: str,
        target: str,
        kind: str,
        item_id: str,
        reason: str,
        project_name: str,
        deep_link: str,
        created_at: str | None = None,
        observed_health: str | None = None,
        observed_blocked_reason: str | None = None,
    ) -> None:
        if kind not in NOTIFICATION_DEFAULTS:
            raise ValueError("invalid notification kind")
        if not self._notification_project_active(connection, project_id):
            return
        now = created_at or self.now()
        for device in connection.execute("SELECT * FROM notification_devices").fetchall():
            if self._notification_device_active(connection, device) and self._notification_allowed(
                connection, device, project_id, kind
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox(
                        device_id, notification_id, project_id, target, kind, item_id,
                        reason, project_name, deep_link, created_at, next_attempt_at,
                        observed_health, observed_blocked_reason
                    ) VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        device["device_id"],
                        notification_id,
                        project_id,
                        target,
                        kind,
                        item_id,
                        reason,
                        project_name,
                        deep_link,
                        now,
                        now,
                        observed_health,
                        observed_blocked_reason,
                    ),
                )

    def _guard_notification_delivery(
        self,
        connection: sqlite3.Connection,
        device_id: str,
        notification_id: str,
        *,
        allow_posted: bool = False,
    ) -> bool:
        device = connection.execute(
            "SELECT * FROM notification_devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if device is None or not self._notification_device_active(connection, device):
            return False
        row = connection.execute(
            "SELECT * FROM notification_outbox WHERE device_id=? AND notification_id=?",
            (device_id, notification_id),
        ).fetchone()
        if row is None or (row["last_status"] == "posted" and not allow_posted):
            return False
        if not self._notification_allowed(connection, device, row["project_id"], row["kind"]):
            connection.execute(
                "DELETE FROM notification_outbox WHERE device_id=? AND notification_id=?",
                (device_id, notification_id),
            )
            return False
        return True

    def guard_notification_delivery(self, device_id: str, notification_id: str) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._guard_notification_delivery(connection, device_id, notification_id)

    def begin_notification_attempt(
        self, device_id: str, notification_id: str, next_attempt_at: str
    ) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._guard_notification_delivery(connection, device_id, notification_id):
                return False
            # The due check sits in the update, so two concurrent pulls cannot
            # lease the same row.
            return (
                connection.execute(
                    "UPDATE notification_outbox SET attempts=attempts+1,next_attempt_at=? "
                    "WHERE device_id=? AND notification_id=? AND next_attempt_at<=?",
                    (next_attempt_at, device_id, notification_id, self.now()),
                ).rowcount
                == 1
            )

    def acknowledge_notification(
        self,
        device_id: str,
        notification_id: str,
        *,
        posted: bool,
        next_attempt_at: str | None = None,
    ) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._guard_notification_delivery(
                connection, device_id, notification_id, allow_posted=posted
            ):
                return False
            connection.execute(
                """
                UPDATE notification_outbox SET last_status=?,next_attempt_at=coalesce(?,next_attempt_at)
                WHERE device_id=? AND notification_id=?
                """,
                ("posted" if posted else "failed", next_attempt_at, device_id, notification_id),
            )
            connection.execute(
                "UPDATE notification_devices SET last_status=? WHERE device_id=?",
                ("on" if posted else "delivery_failed", device_id),
            )
            return True

    def notification_project_baseline(self, project_id: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT baseline_at FROM notification_project_baselines WHERE project_id=?",
                (project_id,),
            ).fetchone()
            return row[0] if row else None

    def baseline_notification_project(
        self,
        project_id: str,
        observations: list[tuple[str, str, str | None]],
        *,
        baseline_at: str | None = None,
    ) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._notification_project_active(connection, project_id):
                return False
            now = baseline_at or self.now()
            inserted = connection.execute(
                "INSERT OR IGNORE INTO notification_project_baselines VALUES (?,?)",
                (project_id, now),
            ).rowcount
            if inserted:
                connection.executemany(
                    "INSERT OR IGNORE INTO notification_episode_observations VALUES (?,?,?,?,?)",
                    [
                        (episode_id, project_id, health, blocked_reason, now)
                        for episode_id, health, blocked_reason in observations
                    ],
                )
            return bool(inserted)

    def notification_graph_marker(
        self, project_id: str, target: str = "main"
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM notification_graph_markers WHERE project_id=? AND target=?",
                (project_id, target),
            ).fetchone()
            return dict(row) if row else None

    def consume_notification_graph_boundary(
        self,
        project_id: str,
        target: str,
        revision: int,
        transition_id: str | None,
        attention: dict[str, list[str]],
        notifications: list[dict[str, Any]],
    ) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._notification_project_active(connection, project_id):
                return False
            previous = connection.execute(
                "SELECT revision,baseline_at FROM notification_graph_markers WHERE project_id=? AND target=?",
                (project_id, target),
            ).fetchone()
            if previous and previous["revision"] >= revision:
                return False
            now = self.now()
            if previous:
                for notification in notifications:
                    self.enqueue_notification(connection, **notification)
            connection.execute(
                """
                INSERT INTO notification_graph_markers VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id,target)
                DO UPDATE SET revision=excluded.revision,transition_id=excluded.transition_id,attention_json
                =excluded.attention_json,updated_at=excluded.updated_at
                """,
                (
                    project_id,
                    target,
                    revision,
                    transition_id,
                    json.dumps(attention),
                    previous["baseline_at"] if previous else now,
                    now,
                ),
            )
            return True

    def rewind_notification_graph_marker(
        self,
        project_id: str,
        target: str,
        revision: int,
        attention: dict[str, list[str]],
    ) -> bool:
        """Move a marker back to `revision`; only a silent first baseline, which sent nothing, may rewind."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._notification_project_active(connection, project_id):
                return False
            updated = connection.execute(
                """
                UPDATE notification_graph_markers SET revision=?,transition_id=NULL,
                attention_json=?,updated_at=? WHERE project_id=? AND target=? AND revision>?
                """,
                (revision, json.dumps(attention), self.now(), project_id, target, revision),
            ).rowcount
            return bool(updated)

    def notification_episode_observations(self, project_id: str) -> dict[str, dict[str, Any]]:
        with self.connection() as connection:
            return {
                row["episode_id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM notification_episode_observations WHERE project_id=?",
                    (project_id,),
                )
            }

    def observe_notification_episode(
        self,
        project_id: str,
        episode_id: str,
        health: str,
        blocked_reason: str | None,
        notification: dict[str, Any] | None,
    ) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._notification_project_active(connection, project_id):
                return False
            previous = connection.execute(
                "SELECT health,blocked_reason FROM notification_episode_observations WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            if previous and tuple(previous) == (health, blocked_reason):
                return False
            if notification:
                self.enqueue_notification(connection, **notification)
            connection.execute(
                """
                INSERT INTO notification_episode_observations VALUES (?,?,?,?,?) ON CONFLICT(episode_id) DO
                UPDATE SET
                health=excluded.health,blocked_reason=excluded.blocked_reason,updated_at=excluded.updated_at
                """,
                (episode_id, project_id, health, blocked_reason, self.now()),
            )
            return True

    def pending_notification_rows(self, device_id: str | None = None) -> list[dict[str, Any]]:
        now = self.now()
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM notification_outbox
                    WHERE (? IS NULL OR device_id=?) AND last_status!='posted'
                    ORDER BY created_at,notification_id
                    """,
                    (device_id, device_id),
                )
                if row["next_attempt_at"] <= now
            ]

    def expire_notification_receipts(self, before: str) -> None:
        """Delete posted rows once their delivery window has passed."""
        cutoff = datetime.fromisoformat(before)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = [
                (row["device_id"], row["notification_id"])
                for row in connection.execute(
                    "SELECT device_id,notification_id,created_at FROM notification_outbox WHERE last_status='posted'"
                )
                if datetime.fromisoformat(row["created_at"]) < cutoff
            ]
            connection.executemany(
                "DELETE FROM notification_outbox WHERE device_id=? AND notification_id=?", expired
            )

    def prune_notification_devices(self) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for device in connection.execute("SELECT * FROM notification_devices").fetchall():
                self._notification_device_active(connection, device)

    def _notification_project_active(self, connection: sqlite3.Connection, project_id: str) -> bool:
        if (
            connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND retired_at IS NULL",
                (project_id,),
            ).fetchone()
            is None
        ):
            return False
        try:
            self._require_project_accepts_new_work(connection, project_id)
        except ValueError:
            return False
        return True

    @staticmethod
    def _rewrite_notification_project_links(
        connection: sqlite3.Connection,
        old_project_id: str,
        project_id: str,
    ) -> None:
        old_prefix = f"#/projects/{quote(old_project_id, safe='')}/"
        prefix = f"#/projects/{quote(project_id, safe='')}/"
        connection.execute(
            """
            UPDATE notification_outbox SET deep_link = ? || substr(deep_link, length(?) + 1)
            WHERE project_id = ? AND substr(deep_link, 1, length(?)) = ?
            """,
            (prefix, old_prefix, project_id, old_prefix, old_prefix),
        )
