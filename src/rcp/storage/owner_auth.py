"""Personal owner admission; session lifetime and revocation use the shared store."""

from __future__ import annotations

import fcntl
import hmac
import os
import re
import sqlite3
from datetime import datetime, timedelta
from typing import IO

from rcp.limits import OWNER_SIGN_IN_TTL_MINUTES, TEAM_CODE_FAILED_ATTEMPT_LIMIT
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import (
    SpaceUserRecord,
    TeamAuthenticationError,
    _new_device_pairing_code,
    _parse_device_pairing_code,
    _sha256,
)


def migrate_owner_auth(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS owner_credentials (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "secret_hash TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS owner_sign_in_codes (code_id TEXT PRIMARY KEY, code_hash TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, consumed_at TEXT, failed_attempts INTEGER NOT NULL DEFAULT 0, "
        "locked_at TEXT)"
    )


def _secret_hash(secret: str) -> str:
    if not isinstance(secret, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", secret) is None:
        raise TeamAuthenticationError("owner_secret_invalid", "Invalid owner credential.")
    return _sha256(secret)


class OwnerAuthStoreMixin(StoreMixinBase):
    def _require_owner(self, connection: sqlite3.Connection) -> SpaceUserRecord:
        if self._space_kind_from_connection(connection) != "personal":
            raise ValueError("Owner authentication requires a personal space.")
        row = connection.execute(
            "SELECT * FROM space_users WHERE identity_kind='local_owner'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Personal owner identity is missing.")
        return self._space_user_record(row)

    def enroll_owner_secret(self, secret: str, *, ownership: IO[str]) -> bool:
        """Enroll once, holding the actual data-directory lock descriptor."""
        secret_hash = _secret_hash(secret)
        descriptor = ownership.fileno()
        actual = os.fstat(descriptor)
        expected = (self.path.parent / "rcp.lock").stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("Owner enrollment requires the data-directory lock.")
        with (self.path.parent / "rcp.lock").open("r+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                raise ValueError("Owner enrollment requires a held data-directory lock.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO owner_credentials VALUES (1, ?)", (secret_hash,)
            )
        return cursor.rowcount == 1

    def create_owner_session(self, secret: str) -> tuple[str, SpaceUserRecord]:
        supplied = _secret_hash(secret)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = self._require_owner(connection)
            row = connection.execute("SELECT secret_hash FROM owner_credentials").fetchone()
            if row is None or not hmac.compare_digest(row["secret_hash"], supplied):
                raise TeamAuthenticationError("owner_secret_invalid", "Invalid owner credential.")
            return self._mint_session(connection, owner, label="Owner device"), owner

    def create_owner_sign_in_code(self) -> str:
        code, code_id, code_hash = _new_device_pairing_code()
        expires = (
            datetime.fromisoformat(self.now()) + timedelta(minutes=OWNER_SIGN_IN_TTL_MINUTES)
        ).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection)
            connection.execute("DELETE FROM owner_sign_in_codes")
            connection.execute(
                "INSERT INTO owner_sign_in_codes(code_id, code_hash, expires_at) VALUES (?, ?, ?)",
                (code_id, code_hash, expires),
            )
        return code

    def redeem_owner_sign_in_code(
        self, code: str, *, secret: str | None = None
    ) -> tuple[str, SpaceUserRecord]:
        new_hash = _secret_hash(secret) if secret is not None else None
        parsed = _parse_device_pairing_code(code)
        if parsed is None:
            raise TeamAuthenticationError("owner_code_invalid", "Invalid sign-in code.")
        code_id, supplied_hash = parsed
        now = self.now()
        error = None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = self._require_owner(connection)
            row = connection.execute(
                "SELECT * FROM owner_sign_in_codes WHERE code_id=?", (code_id,)
            ).fetchone()
            if row is None:
                error = "invalid"
            elif row["consumed_at"] is not None:
                error = "consumed"
            elif row["locked_at"] is not None:
                error = "locked"
            elif row["expires_at"] <= now:
                error = "expired"
            elif not hmac.compare_digest(row["code_hash"], supplied_hash):
                attempts = row["failed_attempts"] + 1
                locked = now if attempts >= TEAM_CODE_FAILED_ATTEMPT_LIMIT else None
                connection.execute(
                    "UPDATE owner_sign_in_codes SET failed_attempts=?, locked_at=? WHERE code_id=?",
                    (attempts, locked, code_id),
                )
                error = "locked" if locked else "invalid"
            else:
                connection.execute(
                    "UPDATE owner_sign_in_codes SET consumed_at=? WHERE code_id=?", (now, code_id)
                )
                if new_hash is not None:
                    previous = connection.execute(
                        "SELECT secret_hash FROM owner_credentials WHERE singleton=1"
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO owner_credentials VALUES (1, ?) "
                        "ON CONFLICT(singleton) DO UPDATE SET secret_hash=excluded.secret_hash",
                        (new_hash,),
                    )
                    if previous is None or not hmac.compare_digest(
                        previous["secret_hash"], new_hash
                    ):
                        connection.execute(
                            "DELETE FROM team_sessions WHERE user_id=?", (owner.user_id,)
                        )
                session = self._mint_session(connection, owner, label="Owner device")
        if error is not None:
            raise TeamAuthenticationError(f"owner_code_{error}", "Sign-in code refused.")
        return session, owner

    def resolve_owner_session(
        self, session: str | None, *, touch: bool = True
    ) -> SpaceUserRecord | None:
        if self.space_kind != "personal":
            return None
        return self._resolve_session(session, identity_kind="local_owner", touch=touch)
