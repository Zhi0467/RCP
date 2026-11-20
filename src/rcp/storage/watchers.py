"""Watchers and their checks: the one writer of both tables.

A watcher belongs to the agent that armed it and wakes that agent once, when
every live check is done. Its state derives from its checks and is never
stored. Removing a check keeps its row and history; cancelling a watcher
never ends jobs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Literal, cast

from rcp.core.graph_targets import graph_target_json
from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import WATCHER_ERROR_MAX_CHARS, WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import (
    AgentTaskRecord,
    EpisodeRecord,
    WatcherCanceller,
    WatcherCheckRecord,
    WatcherRecord,
    _required_timestamp,
    watcher_next_check_at,
)

if TYPE_CHECKING:
    from rcp.storage import AppStore

GraphCheckResult = Literal["active", "completed", "removed"]

_WATCHER_TABLES = (
    """CREATE TABLE watchers (
        watcher_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        purpose TEXT NOT NULL,
        origin_turn_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        cancelled_at TEXT,
        cancelled_by TEXT CHECK(cancelled_by IN ('agent', 'rcp', 'human')),
        cancel_reason TEXT,
        wake_turn_id TEXT,
        delivery_closed_at TEXT,
        CHECK((cancelled_at IS NULL) = (cancelled_by IS NULL))
    )""",
    """CREATE TABLE watcher_checks (
        check_id TEXT PRIMARY KEY,
        watcher_id TEXT NOT NULL REFERENCES watchers(watcher_id),
        machine_id TEXT NOT NULL DEFAULT '',
        definition TEXT NOT NULL CHECK(definition IN ('external', 'graph')),
        check_command TEXT,
        log_path TEXT,
        cwd TEXT,
        graph_condition_json TEXT,
        graph_target_json TEXT,
        armed_head INTEGER,
        created_at TEXT NOT NULL,
        last_checked_at TEXT,
        last_exit_code INTEGER,
        last_error TEXT,
        completed_at TEXT,
        next_check_at TEXT,
        consecutive_error_count INTEGER NOT NULL DEFAULT 0,
        removed_at TEXT
    )""",
)
_WATCHER_INDEXES = (
    "CREATE INDEX watchers_agent ON watchers(agent_id, created_at)",
    "CREATE INDEX watchers_project ON watchers(project_id, created_at)",
    "CREATE INDEX watchers_origin ON watchers(origin_turn_id)",
    "CREATE INDEX watcher_checks_watcher ON watcher_checks(watcher_id)",
    "CREATE INDEX watcher_checks_open ON watcher_checks(definition, completed_at, removed_at)",
)
# An open watcher can still wake its agent.
_OPEN = "w.cancelled_at IS NULL AND w.wake_turn_id IS NULL AND w.delivery_closed_at IS NULL"


def _legacy_agent_id(connection: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Name the agent a pre-migration watcher wakes: an orchestrator is its chain root."""

    if row["origin_task_kind"] != "auto_research" or row["episode_id"] is None:
        return str(row["chat_id"])
    role = connection.execute(
        "SELECT role FROM auto_research_invocations WHERE operation_id = ?",
        (row["origin_operation_id"],),
    ).fetchone()
    if role is None or role["role"] != "orchestrator":
        return str(row["chat_id"])
    episode_id = str(row["episode_id"])
    seen: set[str] = set()
    while episode_id not in seen:
        seen.add(episode_id)
        parent = connection.execute(
            "SELECT continues_episode_id FROM episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if parent is None or parent["continues_episode_id"] is None:
            return episode_id
        episode_id = str(parent["continues_episode_id"])
    return episode_id


def _legacy_purpose(rows: list[sqlite3.Row]) -> str:
    first = rows[0]
    for value in (first["group_label"], first["purpose"]):
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:120]
    if first["graph_condition_json"] is not None:
        condition = json.loads(first["graph_condition_json"])
        return f"Graph condition on {condition.get('node_id', 'a node')}"[:120]
    return str(first["log_path"])[:120]


_LEGACY_CANCELLER = {"human": "human", "loop": "rcp", "agent": "agent"}


def migrate_watcher_checks(connection: sqlite3.Connection) -> None:
    """Fold per-check watcher rows into one watcher with checks.

    A group becomes one watcher. An agent-stopped member becomes a removed
    check; any other stop cancels the whole watcher with its attribution. A
    claimed wake keeps its turn; an acknowledged delivery is closed.
    """

    if any(row[1] == "agent_id" for row in connection.execute("PRAGMA table_info(watchers)")):
        return
    connection.execute("ALTER TABLE watchers RENAME TO _watchers_v1")
    for statement in _WATCHER_TABLES:
        connection.execute(statement)
    rows = connection.execute(
        "SELECT * FROM _watchers_v1 ORDER BY created_at, watcher_id"
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["group_id"] or row["watcher_id"]), []).append(row)
    for watcher_id, members in groups.items():
        first = members[0]
        blocking = next(
            (
                member
                for member in members
                if member["status"] == "stopped" and member["stopped_by"] != "agent"
            ),
            None,
        )
        if blocking is None and all(member["status"] == "stopped" for member in members):
            blocking = members[0]
        cancelled_at = (
            blocking["stopped_at"] or blocking["completed_at"] or blocking["created_at"]
            if blocking is not None
            else None
        )
        claimed = next(
            (
                member["notification_operation_id"]
                for member in members
                if member["notification_operation_id"]
            ),
            None,
        )
        closed = (
            None
            if blocking is not None
            or claimed is not None
            or not any(member["notified"] for member in members)
            else max(
                str(member["stopped_at"] or member["completed_at"] or member["created_at"])
                for member in members
            )
        )
        connection.execute(
            """
            INSERT INTO watchers (
                watcher_id, agent_id, project_id, purpose, origin_turn_id, created_at,
                cancelled_at, cancelled_by, cancel_reason, wake_turn_id, delivery_closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                watcher_id,
                _legacy_agent_id(connection, first),
                first["project_id"],
                _legacy_purpose(members),
                first["origin_operation_id"],
                first["created_at"],
                cancelled_at,
                _LEGACY_CANCELLER.get(str(blocking["stopped_by"]), "rcp")
                if blocking is not None
                else None,
                blocking["stop_reason"] if blocking is not None else None,
                claimed,
                closed,
            ),
        )
        for member in members:
            graph = member["graph_condition_json"] is not None
            removed_at = (
                member["stopped_at"] or member["created_at"]
                if blocking is None
                and member["status"] == "stopped"
                and member["stopped_by"] == "agent"
                else None
            )
            connection.execute(
                """
                INSERT INTO watcher_checks (
                    check_id, watcher_id, machine_id, definition, check_command, log_path,
                    cwd, graph_condition_json, graph_target_json, armed_head, created_at,
                    last_checked_at, last_exit_code, last_error, completed_at, next_check_at,
                    consecutive_error_count, removed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member["watcher_id"],
                    watcher_id,
                    member["machine_id"] or "",
                    "graph" if graph else "external",
                    None if graph else member["check_command"],
                    None if graph else member["log_path"],
                    None if graph else member["cwd"],
                    member["graph_condition_json"],
                    member["graph_target_json"] if graph else None,
                    member["armed_revision"] if graph else None,
                    member["created_at"],
                    member["last_checked_at"],
                    member["last_exit_code"],
                    member["last_error"],
                    member["completed_at"],
                    None
                    if member["status"] in {"completed", "stopped"}
                    else member["next_check_at"],
                    member["consecutive_error_count"],
                    removed_at,
                ),
            )
    connection.execute("DROP TABLE _watchers_v1")
    for statement in _WATCHER_INDEXES:
        connection.execute(statement)
    from rcp.storage.resource_versions import install_watcher_versions

    install_watcher_versions(connection)


def _check_record(row: sqlite3.Row) -> WatcherCheckRecord:
    data = dict(row)
    condition = data.pop("graph_condition_json")
    target = data.pop("graph_target_json")
    data["condition"] = json.loads(condition) if condition is not None else None
    data["graph_target"] = json.loads(target) if target is not None else None
    return WatcherCheckRecord.model_validate(data)


def _watcher_records(
    connection: sqlite3.Connection, where: str, parameters: Iterable[object] = ()
) -> list[WatcherRecord]:
    rows = connection.execute(
        f"SELECT w.* FROM watchers AS w WHERE {where} ORDER BY w.created_at, w.watcher_id",
        tuple(parameters),
    ).fetchall()
    if not rows:
        return []
    ids = [str(row["watcher_id"]) for row in rows]
    checks: dict[str, list[WatcherCheckRecord]] = {watcher_id: [] for watcher_id in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for row in connection.execute(
            f"SELECT * FROM watcher_checks WHERE watcher_id IN ({','.join('?' * len(chunk))}) "
            "ORDER BY created_at, check_id",
            chunk,
        ):
            checks[str(row["watcher_id"])].append(_check_record(row))
    return [
        WatcherRecord.model_validate({**dict(row), "checks": checks[str(row["watcher_id"])]})
        for row in rows
    ]


class WatcherStoreMixin(StoreMixinBase):
    """Watchers, their checks, graph-boundary evaluation, and wake claims."""

    # Reads

    def watcher(self, watcher_id: str) -> WatcherRecord | None:
        with self.connection() as connection:
            found = _watcher_records(connection, "w.watcher_id = ?", (watcher_id,))
        return found[0] if found else None

    def agent_watchers(self, agent_id: str, *, open_only: bool = False) -> list[WatcherRecord]:
        where = "w.agent_id = ?" + (f" AND {_OPEN}" if open_only else "")
        with self.connection() as connection:
            return _watcher_records(connection, where, (agent_id,))

    def project_watchers(self, project_id: str) -> list[WatcherRecord]:
        with self.connection() as connection:
            return _watcher_records(connection, "w.project_id = ?", (project_id,))

    def episode_watchers(
        self, episode_id: str, *, newest: int | None = None
    ) -> list[WatcherRecord]:
        """Watchers armed by the episode's turns, newest first."""

        with self.connection() as connection:
            found = _watcher_records(
                connection,
                "w.origin_turn_id IN (SELECT operation_id FROM graph_runs WHERE episode_id = ?)",
                (episode_id,),
            )
        found.reverse()
        return found[:newest] if newest is not None else found

    def ready_watchers(self) -> list[WatcherRecord]:
        """Open watchers whose every live check is done or unobservable, oldest first."""

        with self.connection() as connection:
            candidates = _watcher_records(
                connection,
                f"{_OPEN} AND NOT EXISTS (SELECT 1 FROM watcher_checks AS c "
                "WHERE c.watcher_id = w.watcher_id AND c.removed_at IS NULL "
                "AND c.completed_at IS NULL AND c.consecutive_error_count < ?) "
                "AND EXISTS (SELECT 1 FROM watcher_checks AS c "
                "WHERE c.watcher_id = w.watcher_id AND c.removed_at IS NULL)",
                (WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT,),
            )
        return [watcher for watcher in candidates if watcher.ready]

    def due_external_checks(self, *, as_of: str | None = None) -> list[WatcherCheckRecord]:
        now = as_of or self.now()
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.* FROM watcher_checks AS c JOIN watchers AS w USING (watcher_id)
                WHERE {_OPEN} AND c.definition = 'external' AND c.removed_at IS NULL
                  AND c.completed_at IS NULL
                  AND (c.next_check_at IS NULL OR c.next_check_at <= ?)
                ORDER BY c.created_at, c.check_id
                """,
                (now,),
            ).fetchall()
        return [_check_record(row) for row in rows]

    def open_graph_checks(self, project_id: str) -> list[WatcherCheckRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.* FROM watcher_checks AS c JOIN watchers AS w USING (watcher_id)
                WHERE w.project_id = ? AND {_OPEN} AND c.definition = 'graph'
                  AND c.removed_at IS NULL AND c.completed_at IS NULL
                ORDER BY c.created_at, c.check_id
                """,
                (project_id,),
            ).fetchall()
        return [_check_record(row) for row in rows]

    def graph_watcher_project_ids(self) -> list[str]:
        """Projects with an open watcher holding a graph check."""

        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT w.project_id FROM watchers AS w
                JOIN watcher_checks AS c USING (watcher_id)
                WHERE {_OPEN} AND c.definition = 'graph' AND c.removed_at IS NULL
                ORDER BY w.project_id
                """
            ).fetchall()
        return [str(row["project_id"]) for row in rows]

    # Arming, revising, cancelling

    def arm_watcher(
        self,
        watcher: WatcherRecord,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WatcherRecord:
        """Insert one new watcher with its checks; an identical retry returns it."""

        if not watcher.checks or not watcher.open:
            raise ValueError("a new watcher needs at least one check and must be open")
        if connection is None:
            with self.connection() as owned:
                owned.execute("BEGIN IMMEDIATE")
                self._insert_watcher(owned, watcher)
        else:
            self._insert_watcher(connection, watcher)
        stored = self.watcher(watcher.watcher_id) if connection is None else watcher
        assert stored is not None
        return stored

    def arm_watchers(self, watchers: list[WatcherRecord]) -> None:
        """Insert several new watchers in one transaction."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for watcher in watchers:
                if not watcher.checks or not watcher.open:
                    raise ValueError("a new watcher needs at least one check and must be open")
                self._insert_watcher(connection, watcher)

    def agent_episode(self, agent_id: str) -> EpisodeRecord | None:
        """The newest episode this agent runs, if any.

        An Experiment agent is its Run's chat; an Auto-research orchestrator is
        its chain's root episode; a worker is its actor or child-Work route.
        """

        with self.connection() as connection:
            row = connection.execute(
                """
                WITH RECURSIVE chain(episode_id) AS (
                    SELECT episode_id FROM episodes
                    WHERE episode_id = :agent AND mode = 'auto_research'
                    UNION
                    SELECT child.episode_id FROM episodes AS child
                    JOIN chain ON child.continues_episode_id = chain.episode_id
                    WHERE child.mode = 'auto_research'
                )
                SELECT * FROM episodes WHERE episode_id IN (
                    SELECT episode_id FROM experiment_episode_state WHERE chat_id = :agent
                    UNION SELECT episode_id FROM auto_research_invocations
                        WHERE actor_operation_id = :agent AND role = 'worker'
                    UNION SELECT episode_id FROM auto_research_child_work
                        WHERE worker_id = :agent
                    UNION SELECT episode_id FROM chain
                    UNION SELECT run.episode_id FROM watchers AS w
                        JOIN graph_runs AS run ON run.operation_id = w.origin_turn_id
                        WHERE w.agent_id = :agent AND run.episode_id IS NOT NULL
                )
                ORDER BY created_at DESC, episode_id DESC LIMIT 1
                """,
                {"agent": agent_id},
            ).fetchone()
        return cast("AppStore", self)._episode_record(row) if row is not None else None

    def resolve_watcher_authorizer(self, watcher_ids: list[str]) -> AuthorizedHuman | None:
        """The one human whose arming turns authorized these watchers.

        Missing, legacy, or conflicting attribution cannot be proved; RCP closes
        those watchers' delivery with a visible reason instead of retrying.
        """

        ids = list(dict.fromkeys(watcher_ids))
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT run.operation_id, run.authorized_space_id, run.authorized_user_id,
                       run.authorized_display_name
                FROM watchers AS w LEFT JOIN graph_runs AS run
                  ON run.operation_id = w.origin_turn_id
                WHERE w.watcher_id IN ({",".join("?" * len(ids))}) AND {_OPEN}
                """,
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                return None
            try:
                found = [
                    None if row["operation_id"] is None else self._authorized_human_snapshot(row)
                    for row in rows
                ]
            except RuntimeError:
                found = [None]
            if found[0] is not None and all(item == found[0] for item in found):
                return found[0]
            self.close_watcher_delivery(
                connection,
                ids,
                reason=(
                    "RCP cannot prove which human authorized this wake. "
                    "Send the agent a message to continue."
                ),
            )
            return None

    def _insert_watcher(self, connection: sqlite3.Connection, watcher: WatcherRecord) -> None:
        existing = _watcher_records(connection, "w.watcher_id = ?", (watcher.watcher_id,))
        if existing:
            if _identity(existing[0]) != _identity(watcher):
                raise ValueError("a watcher id was reused for a different watcher")
            return
        connection.execute(
            """
            INSERT INTO watchers (
                watcher_id, agent_id, project_id, purpose, origin_turn_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                watcher.watcher_id,
                watcher.agent_id,
                watcher.project_id,
                watcher.purpose,
                watcher.origin_turn_id,
                watcher.created_at,
            ),
        )
        for check in watcher.checks:
            self._insert_check(
                connection, check.model_copy(update={"watcher_id": watcher.watcher_id})
            )

    def _insert_check(self, connection: sqlite3.Connection, check: WatcherCheckRecord) -> None:
        if check.definition == "graph":
            assert check.graph_target is not None
            consumed = connection.execute(
                "SELECT revision FROM graph_watcher_reconciliation "
                "WHERE project_id = (SELECT project_id FROM watchers WHERE watcher_id = ?) "
                "AND graph_target_key = ?",
                (check.watcher_id, check.graph_target.key),
            ).fetchone()
            if (
                check.armed_head is not None
                and consumed is not None
                and check.armed_head < int(consumed["revision"])
            ):
                raise ValueError("a graph check cannot arm behind the consumed target boundary")
        next_check_at = check.next_check_at
        if check.definition == "external" and check.completed_at is None and next_check_at is None:
            next_check_at = watcher_next_check_at(
                check.check_id,
                check.last_checked_at or check.created_at,
                check.consecutive_error_count,
            )
        connection.execute(
            """
            INSERT INTO watcher_checks (
                check_id, watcher_id, machine_id, definition, check_command, log_path, cwd,
                graph_condition_json, graph_target_json, armed_head, created_at,
                last_checked_at, last_exit_code, last_error, completed_at, next_check_at,
                consecutive_error_count, removed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                check.check_id,
                check.watcher_id,
                check.machine_id,
                check.definition,
                check.check_command,
                check.log_path,
                check.cwd,
                check.condition.model_dump_json() if check.condition is not None else None,
                graph_target_json(check.graph_target) if check.graph_target is not None else None,
                check.armed_head,
                check.created_at,
                check.last_checked_at,
                check.last_exit_code,
                check.last_error,
                check.completed_at,
                next_check_at,
                check.consecutive_error_count,
            ),
        )

    def revise_watcher(
        self,
        connection: sqlite3.Connection,
        watcher_id: str,
        *,
        purpose: str,
        removed_check_ids: list[str],
        new_checks: list[WatcherCheckRecord],
        at: str,
    ) -> None:
        """Change an open watcher in place: matched checks keep their state."""

        current = _watcher_records(connection, "w.watcher_id = ?", (watcher_id,))
        if not current or not current[0].open:
            raise ValueError(f"watcher {watcher_id} is no longer live and cannot be edited")
        connection.execute(
            "UPDATE watchers SET purpose = ? WHERE watcher_id = ?", (purpose, watcher_id)
        )
        for check_id in removed_check_ids:
            connection.execute(
                "UPDATE watcher_checks SET removed_at = ?, next_check_at = NULL "
                "WHERE check_id = ? AND watcher_id = ? AND removed_at IS NULL",
                (at, check_id, watcher_id),
            )
        for check in new_checks:
            self._insert_check(connection, check.model_copy(update={"watcher_id": watcher_id}))

    def cancel_watchers(
        self,
        watcher_ids: Iterable[str],
        *,
        by: WatcherCanceller,
        reason: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        ids = list(dict.fromkeys(watcher_ids))
        if not ids:
            return 0
        return self._cancel_where(
            f"watcher_id IN ({','.join('?' * len(ids))})",
            ids,
            by=by,
            reason=reason,
            connection=connection,
        )

    def cancel_agent_watchers(
        self,
        agent_id: str,
        *,
        by: WatcherCanceller,
        reason: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        return self._cancel_where(
            "agent_id = ?", (agent_id,), by=by, reason=reason, connection=connection
        )

    def _cancel_where(
        self,
        where: str,
        parameters: Iterable[object],
        *,
        by: WatcherCanceller,
        reason: str | None,
        connection: sqlite3.Connection | None,
        at: str | None = None,
    ) -> int:
        if connection is None:
            with self.connection() as owned:
                owned.execute("BEGIN IMMEDIATE")
                return self._cancel_where(
                    where, parameters, by=by, reason=reason, connection=owned, at=at
                )
        timestamp = at or self.now()
        return connection.execute(
            f"""
            UPDATE watchers SET cancelled_at = ?, cancelled_by = ?, cancel_reason = ?
            WHERE ({where}) AND cancelled_at IS NULL AND wake_turn_id IS NULL
              AND delivery_closed_at IS NULL
            """,
            (timestamp, by, reason, *parameters),
        ).rowcount

    def close_watcher_delivery(
        self, connection: sqlite3.Connection, watcher_ids: list[str], *, reason: str
    ) -> None:
        """End delivery RCP cannot perform, keeping the reason on the watcher."""

        placeholders = ",".join("?" * len(watcher_ids))
        connection.execute(
            f"""
            UPDATE watchers SET delivery_closed_at = ?, cancel_reason = ?
            WHERE watcher_id IN ({placeholders}) AND cancelled_at IS NULL
              AND wake_turn_id IS NULL AND delivery_closed_at IS NULL
            """,
            (self.now(), reason[:WATCHER_ERROR_MAX_CHARS], *watcher_ids),
        )

    def claim_watcher_wake(
        self,
        connection: sqlite3.Connection,
        agent_id: str,
        watcher_ids: list[str],
        wake_turn_id: str,
    ) -> bool:
        """Give ready watchers of one agent to one wake turn, inside its transaction.

        False means the set changed under the caller (cancelled, claimed, or no
        longer ready) and nothing was written.
        """

        ids = list(dict.fromkeys(watcher_ids))
        if not ids or len(ids) != len(watcher_ids):
            raise ValueError("a watcher wake requires unique watcher ids")
        found = _watcher_records(connection, f"w.watcher_id IN ({','.join('?' * len(ids))})", ids)
        if len(found) != len(ids) or any(
            watcher.agent_id != agent_id or not watcher.ready for watcher in found
        ):
            return False
        cursor = connection.execute(
            f"""
            UPDATE watchers SET wake_turn_id = ?
            WHERE watcher_id IN ({",".join("?" * len(ids))}) AND {_OPEN.replace("w.", "")}
            """,
            (wake_turn_id, *ids),
        )
        if cursor.rowcount != len(ids):
            raise RuntimeError("watcher wake claim changed during its transaction")
        return True

    def create_watcher_wake_task(
        self, record: AgentTaskRecord, agent_id: str, watcher_ids: list[str]
    ) -> AgentTaskRecord | None:
        """Queue an ordinary wake and claim its watchers in one transaction.

        A live turn of the same agent wins its slot; nothing changes and the
        watchers stay ready for a later pass.
        """

        if record.status != "queued" or record.request.get("watcher_ids") != watcher_ids:
            raise ValueError("a watcher wake must be queued and name exactly its watchers")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if cast("AppStore", self)._has_active_chat_overlap(connection, record):
                return None
            cast("AppStore", self)._insert_agent_task(
                connection, record, continuation_cause="watcher_wake"
            )
            if not self.claim_watcher_wake(connection, agent_id, watcher_ids, record.operation_id):
                connection.rollback()
                return None
        return cast("AppStore", self).agent_task(record.operation_id)

    @staticmethod
    def detach_watchers_for_restore(
        connection: sqlite3.Connection,
        *,
        diagnostic: str,
        confirmed_by: str,
        now: str,
    ) -> None:
        """Cancel every captured watcher that could still check or wake."""

        if not connection.in_transaction:
            raise ValueError("restored watcher detachment requires an active transaction")
        detail = " ".join(diagnostic.split())[:1500]
        confirmer = " ".join(confirmed_by.split())[:400]
        if not detail or not confirmer:
            raise ValueError("restored watcher detachment requires a reason and confirmer")
        _required_timestamp(now)
        connection.execute(
            """
            UPDATE watchers SET cancelled_at = ?, cancelled_by = 'human', cancel_reason = ?
            WHERE cancelled_at IS NULL AND wake_turn_id IS NULL AND delivery_closed_at IS NULL
            """,
            (now, f"{detail} Restore confirmed by {confirmer}."[:2000]),
        )

    # Observation

    def record_watcher_check(
        self,
        check_id: str,
        *,
        status: Literal["active", "completed", "degraded"],
        exit_code: int | None,
        error: str | None,
        checked_at: str | None = None,
    ) -> None:
        if status == "degraded" and not error:
            raise ValueError("a degraded check requires its error")
        timestamp = checked_at or self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT c.consecutive_error_count FROM watcher_checks AS c
                JOIN watchers AS w USING (watcher_id)
                WHERE c.check_id = ? AND c.definition = 'external' AND c.removed_at IS NULL
                  AND c.completed_at IS NULL AND {_OPEN}
                """,
                (check_id,),
            ).fetchone()
            if row is None:
                return
            errors = int(row["consecutive_error_count"]) + 1 if status == "degraded" else 0
            connection.execute(
                """
                UPDATE watcher_checks
                SET last_checked_at = ?, last_exit_code = ?, last_error = ?,
                    consecutive_error_count = ?, completed_at = ?, next_check_at = ?
                WHERE check_id = ?
                """,
                (
                    timestamp,
                    exit_code,
                    error[:WATCHER_ERROR_MAX_CHARS] if status == "degraded" and error else None,
                    errors,
                    timestamp if status == "completed" else None,
                    None
                    if status == "completed"
                    else watcher_next_check_at(check_id, timestamp, errors),
                    check_id,
                ),
            )

    # Graph conditions

    def graph_watcher_reconciliation_head(
        self,
        project_id: str,
        graph_target: GraphTargetRef,
    ) -> GraphHeadRef | None:
        """Return the last graph boundary atomically consumed for one target."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT graph_target_json, revision, transition_id
                FROM graph_watcher_reconciliation
                WHERE project_id = ? AND graph_target_key = ?
                """,
                (project_id, graph_target.key),
            ).fetchone()
        if row is None:
            return None
        try:
            stored_target = GraphTargetRef.model_validate_json(row["graph_target_json"])
            head = GraphHeadRef(
                target=stored_target,
                revision=row["revision"],
                transition_id=row["transition_id"],
            )
        except ValueError as exc:
            raise RuntimeError("Stored graph-watcher reconciliation head is invalid.") from exc
        if stored_target != graph_target:
            raise RuntimeError("Stored graph-watcher target key does not match its target.")
        return head

    def initialize_graph_watcher_target_baselines(
        self,
        project_id: str,
        graph_target: GraphTargetRef,
        *,
        armed_revision: int,
        evaluated_at: str | None = None,
    ) -> None:
        """Give every unbaselined graph check on one target its current boundary."""

        timestamp = evaluated_at or self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for check in self._open_graph_checks_in(connection, project_id):
                if check.graph_target == graph_target and check.armed_head is None:
                    connection.execute(
                        "UPDATE watcher_checks SET armed_head = ?, last_checked_at = ? "
                        "WHERE check_id = ? AND armed_head IS NULL",
                        (armed_revision, timestamp, check.check_id),
                    )

    @staticmethod
    def _open_graph_checks_in(
        connection: sqlite3.Connection, project_id: str
    ) -> list[WatcherCheckRecord]:
        rows = connection.execute(
            f"""
            SELECT c.* FROM watcher_checks AS c JOIN watchers AS w USING (watcher_id)
            WHERE w.project_id = ? AND {_OPEN} AND c.definition = 'graph'
              AND c.removed_at IS NULL AND c.completed_at IS NULL
            ORDER BY c.created_at, c.check_id
            """,
            (project_id,),
        ).fetchall()
        return [_check_record(row) for row in rows]

    def consume_graph_watcher_boundary(
        self,
        project_id: str,
        head: GraphHeadRef,
        *,
        evaluate: Callable[[WatcherCheckRecord], GraphCheckResult],
        evaluated_at: str | None = None,
    ) -> bool:
        """Evaluate and checkpoint one accepted target boundary atomically.

        The target-local head is the durable receipt. A repeated or older
        boundary writes nothing; a same-revision identity mismatch fails closed.
        If evaluation raises, both the check changes and the head roll back.
        """

        timestamp = evaluated_at or self.now()
        target_json = graph_target_json(head.target)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor_row = connection.execute(
                """
                SELECT graph_target_json, revision, transition_id
                FROM graph_watcher_reconciliation
                WHERE project_id = ? AND graph_target_key = ?
                """,
                (project_id, head.target.key),
            ).fetchone()
            if cursor_row is not None:
                try:
                    stored_target = GraphTargetRef.model_validate_json(
                        cursor_row["graph_target_json"]
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        "Stored graph-watcher reconciliation target is invalid."
                    ) from exc
                if stored_target != head.target:
                    raise RuntimeError("Stored graph-watcher target key does not match its target.")
                stored_revision = int(cursor_row["revision"])
                if stored_revision > head.revision:
                    return False
                if stored_revision == head.revision:
                    if cursor_row["transition_id"] != head.transition_id:
                        raise RuntimeError(
                            "Accepted graph boundary changed identity at a consumed revision."
                        )
                    return False
            for check in self._open_graph_checks_in(connection, project_id):
                if (
                    check.graph_target != head.target
                    or check.armed_head is None
                    or check.armed_head >= head.revision
                ):
                    continue
                self._record_graph_result(connection, check, evaluate(check), timestamp)
            if cursor_row is None:
                connection.execute(
                    """
                    INSERT INTO graph_watcher_reconciliation(
                        project_id, graph_target_key, graph_target_json,
                        revision, transition_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        head.target.key,
                        target_json,
                        head.revision,
                        head.transition_id,
                        timestamp,
                    ),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE graph_watcher_reconciliation
                    SET graph_target_json = ?, revision = ?, transition_id = ?, updated_at = ?
                    WHERE project_id = ? AND graph_target_key = ? AND revision = ?
                    """,
                    (
                        target_json,
                        head.revision,
                        head.transition_id,
                        timestamp,
                        project_id,
                        head.target.key,
                        int(cursor_row["revision"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("Graph-watcher reconciliation head changed concurrently.")
        return True

    def record_graph_check_result(
        self,
        check_id: str,
        *,
        result: GraphCheckResult,
        evaluated_at: str | None = None,
    ) -> None:
        """Persist one graph evaluation outside a consumed boundary."""

        timestamp = evaluated_at or self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM watcher_checks WHERE check_id = ?", (check_id,)
            ).fetchone()
            if row is None:
                raise KeyError(check_id)
            self._record_graph_result(connection, _check_record(row), result, timestamp)

    @classmethod
    def _record_graph_result(
        cls,
        connection: sqlite3.Connection,
        check: WatcherCheckRecord,
        result: GraphCheckResult,
        at: str,
    ) -> None:
        if result == "removed":
            # A condition whose node is gone can never complete; RCP cancels it.
            connection.execute(
                "UPDATE watcher_checks SET last_checked_at = ? WHERE check_id = ?",
                (at, check.check_id),
            )
            connection.execute(
                """
                UPDATE watchers SET cancelled_at = ?, cancelled_by = 'rcp',
                    cancel_reason = 'Graph condition target was removed.'
                WHERE watcher_id = ? AND cancelled_at IS NULL AND wake_turn_id IS NULL
                  AND delivery_closed_at IS NULL
                """,
                (at, check.watcher_id),
            )
            return
        connection.execute(
            """
            UPDATE watcher_checks SET last_checked_at = ?,
                completed_at = CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE completed_at END
            WHERE check_id = ? AND removed_at IS NULL AND completed_at IS NULL
            """,
            (at, result == "completed", at, check.check_id),
        )

    def initialize_graph_check_baseline(
        self,
        check_id: str,
        *,
        armed_revision: int,
        evaluated_at: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE watcher_checks SET armed_head = ?, last_checked_at = ? "
                "WHERE check_id = ? AND armed_head IS NULL",
                (armed_revision, evaluated_at or self.now(), check_id),
            )


def _identity(watcher: WatcherRecord) -> tuple[object, ...]:
    return (
        watcher.agent_id,
        watcher.project_id,
        watcher.origin_turn_id,
        tuple(
            (
                check.check_id,
                check.machine_id,
                check.definition,
                check.check_command,
                check.log_path,
                check.cwd,
                check.condition.model_dump_json() if check.condition is not None else None,
                check.graph_target.key if check.graph_target is not None else None,
            )
            for check in watcher.checks
        ),
    )
