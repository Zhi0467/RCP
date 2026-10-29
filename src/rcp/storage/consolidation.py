"""Space-local schedule admissions, outcome rows, and immutable Apply receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Literal, TypeVar, overload

from pydantic import BaseModel, Field

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import (
    CONSOLIDATION_AUTHORIZATION_DAYS,
    CONSOLIDATION_RECENT_NIGHTS,
    RUN_STAGE_RETENTION_DAYS,
)
from rcp.storage.digest import append_digest_event, digest_link
from rcp.storage.membership import live_project_member
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import AgentTaskRecord


class ConsolidationSchedule(BaseModel):
    project_id: str
    authorization_id: str
    local_time: str
    timezone: str
    authorized_by: AuthorizedHuman
    authorized_at: str
    expires_at: str
    next_due_at: str
    last_occurrence_date: str | None = None
    covered_head: int | None = None
    last_run_at: str | None = None
    last_outcome: Literal["succeeded", "failed", "skipped"] | None = None
    notification_observed_at: str | None = None
    # Skipped occurrences leave no run row; the newest few feed the nights strip.
    skipped_dates: list[str] = Field(default_factory=list)


class ConsolidationTarget(BaseModel):
    project_id: str
    target: str
    next_due_at: str
    covered_head: int | None = None
    last_occurrence_date: str | None = None
    skipped_dates: list[str] = Field(default_factory=list)


class ConsolidationRun(BaseModel):
    run_id: str
    project_id: str
    occurrence_date: str
    target: str = "main"
    operation_id: str | None = None
    chat_id: str | None = None
    authorization_id: str
    authorized_by: AuthorizedHuman
    input_head: int
    created_at: str
    outcome_settled_at: str | None = None
    kind: Literal["report", "failure"] | None = None
    report_artifact_id: str | None = None
    report_title: str | None = None
    applied_revisions: list[dict] = Field(default_factory=list)
    revisions_verified: bool = False
    proposals_created: int = 0
    error_code: str | None = None
    error_message: str | None = None
    state: Literal["open", "kept", "dismissed"] = "open"
    resolved_by: AuthorizedHuman | None = None
    resolved_at: str | None = None
    notification_observed_at: str | None = None

    @property
    def graph_target(self) -> GraphTargetRef:
        return target_ref(self.target)


def target_ref(target: str) -> GraphTargetRef:
    return GraphTargetRef() if target == "main" else GraphTargetRef(kind="branch", branch_id=target)


def target_label(target: str, names: Mapping[str, str]) -> str:
    """Display a stored target, retaining the short id for an unavailable branch."""
    return "main" if target == "main" else names.get(target, target[:8])


def migrate_consolidation(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS consolidation_schedules (
        project_id TEXT PRIMARY KEY, authorization_id TEXT NOT NULL UNIQUE,
        local_time TEXT NOT NULL, timezone TEXT NOT NULL, authorized_by_json TEXT NOT NULL,
        authorized_at TEXT NOT NULL, expires_at TEXT NOT NULL, next_due_at TEXT NOT NULL,
        covered_head INTEGER, last_occurrence_date TEXT, last_run_at TEXT, last_outcome TEXT,
        notification_observed_at TEXT, skipped_dates_json TEXT
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS consolidation_runs (
        run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, occurrence_date TEXT NOT NULL,
        operation_id TEXT UNIQUE, chat_id TEXT, authorization_id TEXT NOT NULL,
        authorized_by_json TEXT NOT NULL, input_head INTEGER NOT NULL, created_at TEXT NOT NULL,
        outcome_settled_at TEXT, kind TEXT CHECK(kind IN ('report','failure')),
        report_artifact_id TEXT, report_title TEXT, applied_revisions_json TEXT NOT NULL DEFAULT '[]',
        revisions_verified INTEGER NOT NULL DEFAULT 0, proposals_created INTEGER NOT NULL DEFAULT 0,
        error_code TEXT, error_message TEXT,
        state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','kept','dismissed')),
        resolved_by_json TEXT, resolved_at TEXT, notification_observed_at TEXT,
        UNIQUE(project_id, occurrence_date)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS consolidation_apply_receipts (
        project_id TEXT NOT NULL, operation_id TEXT NOT NULL, key TEXT NOT NULL,
        sha256 TEXT NOT NULL, patch_text TEXT NOT NULL,
        source_effect_id TEXT NOT NULL, result_json TEXT, last_failure_json TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY(operation_id,key)
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS consolidation_runs_unsettled ON consolidation_runs(project_id,outcome_settled_at)"
    )


def migrate_consolidation_targets(connection: sqlite3.Connection) -> None:
    """Move v37 coverage and occurrences to independent graph targets."""
    connection.execute("""CREATE TABLE consolidation_targets (
        project_id TEXT NOT NULL, target TEXT NOT NULL, next_due_at TEXT NOT NULL,
        covered_head INTEGER, last_occurrence_date TEXT, skipped_dates_json TEXT,
        PRIMARY KEY(project_id,target)
    )""")
    connection.execute("""INSERT INTO consolidation_targets
        SELECT project_id,'main',next_due_at,covered_head,last_occurrence_date,skipped_dates_json
        FROM consolidation_schedules""")
    connection.execute("""CREATE TABLE consolidation_runs_per_target (
        run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, occurrence_date TEXT NOT NULL,
        operation_id TEXT UNIQUE, chat_id TEXT, authorization_id TEXT NOT NULL,
        authorized_by_json TEXT NOT NULL, input_head INTEGER NOT NULL, created_at TEXT NOT NULL,
        outcome_settled_at TEXT, kind TEXT CHECK(kind IN ('report','failure')),
        report_artifact_id TEXT, report_title TEXT, applied_revisions_json TEXT NOT NULL DEFAULT '[]',
        revisions_verified INTEGER NOT NULL DEFAULT 0, proposals_created INTEGER NOT NULL DEFAULT 0,
        error_code TEXT, error_message TEXT,
        state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','kept','dismissed')),
        resolved_by_json TEXT, resolved_at TEXT, notification_observed_at TEXT,
        target TEXT NOT NULL DEFAULT 'main',
        UNIQUE(project_id,target,occurrence_date)
    )""")
    connection.execute(
        "INSERT INTO consolidation_runs_per_target SELECT *, 'main' FROM consolidation_runs"
    )
    connection.execute("DROP TABLE consolidation_runs")
    connection.execute("ALTER TABLE consolidation_runs_per_target RENAME TO consolidation_runs")
    connection.execute(
        "CREATE INDEX consolidation_runs_unsettled ON consolidation_runs(project_id,target,outcome_settled_at)"
    )
    for event in ("insert", "update", "delete"):
        connection.execute(f"DROP TRIGGER resource_consolidation_schedules_{event}")
    connection.execute("ALTER TABLE consolidation_schedules DROP COLUMN covered_head")
    from rcp.storage.resource_versions import install_consolidation_resource_versions

    install_consolidation_resource_versions(connection)
    from rcp.storage.evidence_flags import migrate_evidence_flag_targets

    migrate_evidence_flag_targets(connection)


_Record = TypeVar("_Record", bound=BaseModel)


@overload
def _record(model: type[_Record], row: sqlite3.Row) -> _Record: ...


@overload
def _record(model: type[_Record], row: None) -> None: ...


def _record(model: type[_Record], row: sqlite3.Row | None) -> _Record | None:
    if row is None:
        return None
    values = dict(row)
    for name in ("authorized_by", "resolved_by", "applied_revisions", "skipped_dates"):
        if name + "_json" in values:
            raw = values.pop(name + "_json")
            if raw is not None:
                values[name] = json.loads(raw)
            elif name != "skipped_dates":
                values[name] = None
    return model.model_validate(values)


class ConsolidationStoreMixin(StoreMixinBase):
    def _one_record(
        self, model: type[_Record], sql: str, params: tuple[object, ...]
    ) -> _Record | None:
        with self.connection() as conn:
            return _record(model, conn.execute(sql, params).fetchone())

    def consolidation_schedule(self, project_id: str) -> ConsolidationSchedule | None:
        return self._one_record(
            ConsolidationSchedule,
            "SELECT s.*,t.covered_head FROM consolidation_schedules s LEFT JOIN consolidation_targets t ON t.project_id=s.project_id AND t.target='main' WHERE s.project_id=?",
            (project_id,),
        )

    def consolidation_schedules(self) -> list[ConsolidationSchedule]:
        with self.connection() as conn:
            return [
                _record(ConsolidationSchedule, row)
                for row in conn.execute(
                    "SELECT s.*,t.covered_head FROM consolidation_schedules s LEFT JOIN consolidation_targets t ON t.project_id=s.project_id AND t.target='main'"
                )
            ]

    def put_consolidation_schedule(
        self,
        project_id: str,
        *,
        local_time: str,
        timezone: str,
        authorized_by: AuthorizedHuman,
        next_due_at: str,
        now: str | None = None,
    ) -> ConsolidationSchedule:
        now = now or self.now()
        expires_at = (
            datetime.fromisoformat(now) + timedelta(days=CONSOLIDATION_AUTHORIZATION_DAYS)
        ).isoformat()
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO consolidation_schedules(project_id,authorization_id,local_time,timezone,authorized_by_json,authorized_at,expires_at,next_due_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                authorization_id=excluded.authorization_id,local_time=excluded.local_time,
                timezone=excluded.timezone,authorized_by_json=excluded.authorized_by_json,
                authorized_at=excluded.authorized_at,expires_at=excluded.expires_at,
                next_due_at=excluded.next_due_at,notification_observed_at=NULL""",
                (
                    project_id,
                    uuid.uuid4().hex,
                    local_time,
                    timezone,
                    authorized_by.model_dump_json(),
                    now,
                    expires_at,
                    next_due_at,
                ),
            )
            conn.execute(
                "UPDATE consolidation_targets SET next_due_at=? WHERE project_id=?",
                (next_due_at, project_id),
            )
        return self.consolidation_schedule(project_id)

    def consolidation_target(
        self, project_id: str, target: str = "main"
    ) -> ConsolidationTarget | None:
        return self._one_record(
            ConsolidationTarget,
            "SELECT * FROM consolidation_targets WHERE project_id=? AND target=?",
            (project_id, target),
        )

    def consolidation_targets(self, project_id: str) -> list[ConsolidationTarget]:
        with self.connection() as conn:
            return [
                _record(ConsolidationTarget, row)
                for row in conn.execute(
                    "SELECT * FROM consolidation_targets WHERE project_id=?", (project_id,)
                )
            ]

    def ensure_consolidation_target(
        self,
        schedule: ConsolidationSchedule,
        target: str = "main",
        *,
        covered_head: int | None = None,
    ) -> ConsolidationTarget:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                conn.execute(
                    "SELECT 1 FROM consolidation_schedules WHERE project_id=? AND authorization_id=? AND next_due_at=?",
                    (schedule.project_id, schedule.authorization_id, schedule.next_due_at),
                ).fetchone()
                is None
            ):
                raise ValueError("The consolidation schedule changed before target initialization.")
            conn.execute(
                "INSERT OR IGNORE INTO consolidation_targets(project_id,target,next_due_at,covered_head) VALUES(?,?,?,?)",
                (schedule.project_id, target, schedule.next_due_at, covered_head),
            )
        coverage = self.consolidation_target(schedule.project_id, target)
        assert coverage is not None
        return coverage

    def finish_consolidation_occurrence(
        self, schedule: ConsolidationSchedule, *, targets: list[str], next_due_at: str
    ) -> None:
        """Keep the project due while any active target still owes an occurrence."""
        with self.connection() as conn:
            dues = [
                row["next_due_at"]
                for row in conn.execute(
                    "SELECT target,next_due_at FROM consolidation_targets WHERE project_id=?",
                    (schedule.project_id,),
                )
                if row["target"] in targets
            ]
            if len(dues) < len(set(targets)):
                dues.append(schedule.next_due_at)
            due = min([next_due_at, *dues], key=datetime.fromisoformat)
            conn.execute(
                "UPDATE consolidation_schedules SET next_due_at=? WHERE project_id=? AND authorization_id=? AND next_due_at=?",
                (due, schedule.project_id, schedule.authorization_id, schedule.next_due_at),
            )

    def delete_consolidation_schedule(self, project_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM consolidation_schedules WHERE project_id=?", (project_id,))

    def consolidation_run(self, run_id: str) -> ConsolidationRun | None:
        return self._one_record(
            ConsolidationRun, "SELECT * FROM consolidation_runs WHERE run_id=?", (run_id,)
        )

    def consolidation_run_for_operation(self, operation_id: str) -> ConsolidationRun | None:
        return self._one_record(
            ConsolidationRun,
            "SELECT * FROM consolidation_runs WHERE operation_id=?",
            (operation_id,),
        )

    def drop_consolidation_request_target(self, operation_id: str) -> None:
        # 0.4.35 also copied the run's target into its queued request, which no
        # request model carries, so the launch refused it on every retry.
        with self.connection() as conn:
            conn.execute(
                "UPDATE graph_runs SET request_json=json_remove(request_json, '$.graph_target') "
                "WHERE operation_id=? AND status='queued' "
                "AND json_type(request_json, '$.graph_target') IS NOT NULL",
                (operation_id,),
            )

    def pending_consolidation_operation_ids(self) -> set[str]:
        with self.connection() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT operation_id FROM consolidation_runs WHERE operation_id IS NOT NULL AND outcome_settled_at IS NULL"
                )
            }

    def consolidation_runs(
        self,
        project_id: str | None = None,
        *,
        target: str | None = None,
        unsettled_only: bool = False,
        open_only: bool = False,
    ) -> list[ConsolidationRun]:
        conditions, args = [], []
        if project_id is not None:
            conditions.append("project_id=?")
            args.append(project_id)
        if target is not None:
            conditions.append("target=?")
            args.append(target)
        if unsettled_only:
            conditions.append("(outcome_settled_at IS NULL OR revisions_verified=0)")
        if open_only:
            conditions.append("state='open' AND outcome_settled_at IS NOT NULL")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connection() as conn:
            return [
                _record(ConsolidationRun, row)
                for row in conn.execute(
                    "SELECT * FROM consolidation_runs" + where + " ORDER BY created_at DESC", args
                )
            ]

    def claim_consolidation_occurrence(
        self,
        schedule: ConsolidationSchedule,
        *,
        occurrence_date: str,
        next_due_at: str,
        input_head: int,
        target: str = "main",
        task: AgentTaskRecord | None = None,
        execution_host: str = "",
        error_code: str | None = None,
        error_message: str | None = None,
        skipped: bool = False,
        now: str | None = None,
    ) -> ConsolidationRun | None:
        """Claim an initialized target: due CAS, owner row, and task commit together."""
        now = now or self.now()
        run_id = uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM consolidation_schedules WHERE project_id=? AND authorization_id=? AND next_due_at=?",
                (schedule.project_id, schedule.authorization_id, schedule.next_due_at),
            ).fetchone()
            if (
                current is None
                or datetime.fromisoformat(current["expires_at"]) <= datetime.fromisoformat(now)
                or datetime.fromisoformat(current["next_due_at"]) > datetime.fromisoformat(now)
            ):
                return None
            target_state = conn.execute(
                "SELECT * FROM consolidation_targets WHERE project_id=? AND target=?",
                (schedule.project_id, target),
            ).fetchone()
            if target_state is None:
                raise ValueError("Consolidation target must be initialized before claiming it.")
            if datetime.fromisoformat(target_state["next_due_at"]) > datetime.fromisoformat(now):
                return None
            if conn.execute(
                "SELECT 1 FROM consolidation_runs WHERE project_id=? AND target=? AND outcome_settled_at IS NULL",
                (schedule.project_id, target),
            ).fetchone():
                return None
            # A renewed schedule may owe a different time on an already handled date.
            duplicate = conn.execute(
                "SELECT 1 FROM consolidation_runs WHERE project_id=? AND target=? AND occurrence_date=?",
                (schedule.project_id, target, occurrence_date),
            ).fetchone()
            if duplicate or target_state["last_occurrence_date"] == occurrence_date:
                conn.execute(
                    "UPDATE consolidation_targets SET next_due_at=? WHERE project_id=? AND target=?",
                    (next_due_at, schedule.project_id, target),
                )
                return None
            if not live_project_member(conn, schedule.project_id, schedule.authorized_by.user_id):
                task = None
                skipped = False
                error_code = "authorization_membership_lost"
                error_message = "The schedule authorizer is no longer a project member."
            if task is not None:
                if (
                    task.project_id != schedule.project_id
                    or task.authorized_by != schedule.authorized_by
                    or task.graph_target != target_ref(target)
                ):
                    raise ValueError(
                        "consolidation task does not match its authorization or target"
                    )
                self._require_project_accepts_new_work(conn, schedule.project_id)
                if self._has_active_chat_overlap(conn, task):
                    return None
                if self._has_resumable_paused_chat_task(
                    conn, task.project_id, task.kind, task.request["chat_id"]
                ):
                    task = None
                    error_code = "consolidation_chat_paused"
                    error_message = "The consolidation chat has a resumable paused turn."
            if not skipped:
                if task is None and error_code is None:
                    raise ValueError("an occurrence requires a task or typed failure")
                conn.execute(
                    """INSERT INTO consolidation_runs(run_id,project_id,occurrence_date,operation_id,chat_id,authorization_id,authorized_by_json,input_head,created_at,outcome_settled_at,kind,error_code,error_message,revisions_verified,target)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        schedule.project_id,
                        occurrence_date,
                        task.operation_id if task else None,
                        task.request.get("chat_id") if task else None,
                        schedule.authorization_id,
                        schedule.authorized_by.model_dump_json(),
                        input_head,
                        now,
                        None if task else now,
                        None if task else "failure",
                        error_code,
                        error_message,
                        int(task is None),
                        target,
                    ),
                )
                if task is None:
                    append_digest_event(
                        conn,
                        project_id=schedule.project_id,
                        kind="consolidation_failed",
                        item_id=run_id,
                        target=target_ref(target).key,
                        created_at=now,
                        payload={
                            "title": "Nightly consolidation",
                            "status": "failed",
                            "deep_link": digest_link(
                                schedule.project_id,
                                target_ref(target).model_dump(),
                                "consolidation",
                                run_id,
                            ),
                        },
                    )
                if task:
                    task = self._bind_consolidation_stage(conn, task, execution_host)
                    self._insert_agent_task(conn, task, continuation_cause="fresh")
            skipped_dates = json.loads(target_state["skipped_dates_json"] or "[]")
            if skipped:
                skipped_dates = [*skipped_dates, occurrence_date][-CONSOLIDATION_RECENT_NIGHTS:]
            conn.execute(
                "UPDATE consolidation_targets SET next_due_at=?,last_occurrence_date=?,skipped_dates_json=? WHERE project_id=? AND target=?",
                (
                    next_due_at,
                    occurrence_date,
                    json.dumps(skipped_dates),
                    schedule.project_id,
                    target,
                ),
            )
            conn.execute(
                "UPDATE consolidation_schedules SET last_run_at=?,last_outcome=?,last_occurrence_date=?,skipped_dates_json=CASE WHEN ?='main' THEN ? ELSE skipped_dates_json END WHERE project_id=?",
                (
                    now,
                    "skipped" if skipped else ("failed" if task is None else None),
                    occurrence_date,
                    target,
                    json.dumps(skipped_dates),
                    schedule.project_id,
                ),
            )
        return None if skipped else self.consolidation_run(run_id)

    @staticmethod
    def _bind_consolidation_stage(
        connection: sqlite3.Connection, task: AgentTaskRecord, execution_host: str
    ) -> AgentTaskRecord:
        rows = connection.execute(
            "SELECT DISTINCT stage_root FROM graph_runs "
            "WHERE project_id=? AND kind=? AND history_only=0 "
            "AND json_extract(request_json, '$.chat_id')=? "
            "AND COALESCE(stage_host, '')=? "
            "AND stage_root IS NOT NULL AND stage_root != ''",
            (task.project_id, task.kind, task.request.get("chat_id"), execution_host),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("The consolidation chat has conflicting saved workspace bindings.")
        return task.model_copy(
            update={
                "stage_host": (execution_host or None) if rows else None,
                "stage_root": rows[0]["stage_root"] if rows else None,
            }
        )

    def settle_consolidation_run(
        self,
        run_id: str,
        *,
        kind: Literal["report", "failure"],
        applied_revisions: list[dict],
        revisions_verified: bool,
        proposals_created: int,
        report_artifact_id: str | None = None,
        report_title: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        covered_head: int | None = None,
    ) -> ConsolidationRun:
        now = self.now()
        lock = self.artifact_lock(report_artifact_id) if report_artifact_id else nullcontext()
        with lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM consolidation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["outcome_settled_at"] is not None and row["revisions_verified"]:
                return _record(ConsolidationRun, row)
            if row["outcome_settled_at"] is not None:
                # Later history recovery enriches evidence, never a settled outcome.
                if revisions_verified:
                    conn.execute(
                        "UPDATE consolidation_runs SET applied_revisions_json=?,revisions_verified=1,proposals_created=? WHERE run_id=?",
                        (json.dumps(applied_revisions), proposals_created, run_id),
                    )
                return _record(
                    ConsolidationRun,
                    conn.execute(
                        "SELECT * FROM consolidation_runs WHERE run_id=?", (run_id,)
                    ).fetchone(),
                )
            if kind == "report":
                artifact = conn.execute(
                    "SELECT metadata FROM artifacts WHERE artifact_id=? AND project_id=?",
                    (report_artifact_id, row["project_id"]),
                ).fetchone()
                if artifact is None:
                    raise ValueError("consolidation report artifact is unavailable")
                metadata = json.loads(artifact[0])
                # A saved report is retained, not reviewed; only a human settles the row.
                if row["state"] == "open":
                    metadata["expires_at"] = None
                conn.execute(
                    "UPDATE artifacts SET metadata=? WHERE artifact_id=?",
                    (json.dumps(metadata), report_artifact_id),
                )
            conn.execute(
                """UPDATE consolidation_runs SET kind=?,outcome_settled_at=?,report_artifact_id=?,report_title=?,applied_revisions_json=?,revisions_verified=?,proposals_created=?,error_code=?,error_message=? WHERE run_id=?""",
                (
                    kind,
                    now,
                    report_artifact_id,
                    report_title,
                    json.dumps(applied_revisions),
                    int(revisions_verified),
                    proposals_created,
                    error_code,
                    error_message,
                    run_id,
                ),
            )
            append_digest_event(
                conn,
                project_id=row["project_id"],
                kind="consolidation_report" if kind == "report" else "consolidation_failed",
                item_id=run_id,
                target=target_ref(row["target"]).key,
                created_at=now,
                payload={
                    "title": report_title or "Nightly consolidation",
                    "report_artifact_id": report_artifact_id,
                    "status": "succeeded" if kind == "report" else "failed",
                    "deep_link": digest_link(
                        row["project_id"],
                        target_ref(row["target"]).model_dump(),
                        "consolidation",
                        run_id,
                    ),
                },
            )
            conn.execute(
                "UPDATE consolidation_targets SET covered_head=COALESCE(?,covered_head) WHERE project_id=? AND target=?",
                (
                    covered_head if kind == "report" else None,
                    row["project_id"],
                    row["target"],
                ),
            )
            conn.execute(
                "UPDATE consolidation_schedules SET last_outcome=? WHERE project_id=?",
                ("succeeded" if kind == "report" else "failed", row["project_id"]),
            )
        return self.consolidation_run(run_id)

    def resolve_consolidation_run(
        self,
        project_id: str,
        run_id: str,
        *,
        state: Literal["kept", "dismissed"],
        resolved_by: AuthorizedHuman,
    ) -> ConsolidationRun:
        run = self.consolidation_run(run_id)
        if run is None or run.project_id != project_id or run.kind is None:
            raise KeyError(run_id)
        if state == "kept" and run.kind != "report":
            raise ValueError("only a report can be kept")
        if run.state != "open":
            return run
        if state == "kept":
            self.keep_artifact(run.report_artifact_id, resolved_by=resolved_by)
            return self.consolidation_run(run_id)
        now = self.now()
        lock = (
            self.artifact_lock(run.report_artifact_id) if run.report_artifact_id else nullcontext()
        )
        with lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE consolidation_runs SET state='dismissed',resolved_by_json=?,resolved_at=? WHERE run_id=? AND state='open'",
                (resolved_by.model_dump_json(), now, run_id),
            ).rowcount
            if changed and run.report_artifact_id:
                expiry = (
                    datetime.fromisoformat(now) + timedelta(days=RUN_STAGE_RETENTION_DAYS)
                ).isoformat()
                conn.execute(
                    "UPDATE artifacts SET metadata=json_set(metadata,'$.expires_at',?) WHERE artifact_id=? AND json_extract(metadata,'$.kept_at') IS NULL",
                    (expiry, run.report_artifact_id),
                )
        return self.consolidation_run(run_id)

    def reserve_consolidation_apply(
        self, operation_id: str, key: str, sha256: str, patch_text: str, source_effect_id: str
    ) -> dict:
        if hashlib.sha256(patch_text.encode()).hexdigest() != sha256:
            raise ValueError("consolidation Patch digest mismatch")
        run = self.consolidation_run_for_operation(operation_id)
        if run is None:
            raise ValueError("operation has no consolidation binding")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM consolidation_apply_receipts WHERE operation_id=? AND key=?",
                (operation_id, key),
            ).fetchone()
            if row is not None and (
                row["sha256"] != sha256 or row["source_effect_id"] != source_effect_id
            ):
                raise ValueError("consolidation Apply key already names different bytes")
            if row is None:
                conn.execute(
                    "INSERT INTO consolidation_apply_receipts(project_id,operation_id,key,sha256,patch_text,source_effect_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        run.project_id,
                        operation_id,
                        key,
                        sha256,
                        patch_text,
                        source_effect_id,
                        self.now(),
                    ),
                )
        return next(
            row for row in self.list_consolidation_apply_receipts(operation_id) if row["key"] == key
        )

    def record_consolidation_apply_failure(
        self, operation_id: str, key: str, status: Literal["invalid", "unavailable"], message: str
    ) -> None:
        """Keep retryable failure evidence separate from the immutable success result."""
        if status not in {"invalid", "unavailable"}:
            raise ValueError("invalid consolidation Apply failure status")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT result_json FROM consolidation_apply_receipts WHERE operation_id=? AND key=?",
                (operation_id, key),
            ).fetchone()
            if row is None:
                raise KeyError((operation_id, key))
            if row[0] is None:
                conn.execute(
                    "UPDATE consolidation_apply_receipts SET last_failure_json=? WHERE operation_id=? AND key=?",
                    (json.dumps({"status": status, "message": message}), operation_id, key),
                )

    def finish_consolidation_apply(self, operation_id: str, key: str, result: dict) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT result_json FROM consolidation_apply_receipts WHERE operation_id=? AND key=?",
                (operation_id, key),
            ).fetchone()
            if row is None:
                raise KeyError((operation_id, key))
            if row[0] is not None and json.loads(row[0]) != result:
                raise ValueError("consolidation Apply result is immutable")
            conn.execute(
                "UPDATE consolidation_apply_receipts SET result_json=?,last_failure_json=NULL WHERE operation_id=? AND key=?",
                (json.dumps(result), operation_id, key),
            )

    def list_consolidation_apply_receipts(self, operation_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM consolidation_apply_receipts WHERE operation_id=? ORDER BY created_at,key",
                (operation_id,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            raw = value.pop("result_json")
            value["result"] = json.loads(raw) if raw else None
            failure = value.pop("last_failure_json")
            value["last_failure"] = json.loads(failure) if failure else None
            result.append(value)
        return result

    def consolidation_report_is_pending(self, artifact) -> bool:
        """Protect open reports and captures awaiting outcome reconciliation."""
        if (
            artifact.source_name != "consolidation-report.html"
            or artifact.media_type != "text/html"
        ):
            return False
        with self.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM consolidation_runs WHERE project_id=? AND ("
                    "(report_artifact_id=? AND kind='report' AND state='open') OR "
                    "(operation_id=? AND outcome_settled_at IS NULL))",
                    (
                        artifact.project_id,
                        artifact.artifact_id,
                        artifact.origin_operation_id or artifact.supplier_id,
                    ),
                ).fetchone()
                is not None
            )

    def detach_consolidation_for_restore(
        self, connection: sqlite3.Connection, *, diagnostic: str, now: str
    ) -> None:
        connection.execute("DELETE FROM consolidation_schedules")
        rows = connection.execute(
            "UPDATE consolidation_runs SET kind='failure',outcome_settled_at=?,error_code='restored_run_detached',error_message=?,revisions_verified=0 WHERE outcome_settled_at IS NULL RETURNING *",
            (now, diagnostic),
        ).fetchall()
        for row in rows:
            append_digest_event(
                connection,
                project_id=row["project_id"],
                kind="consolidation_failed",
                item_id=row["run_id"],
                target=target_ref(row["target"]).key,
                created_at=now,
                payload={
                    "title": "Nightly consolidation",
                    "status": "failed",
                    "deep_link": digest_link(
                        row["project_id"],
                        target_ref(row["target"]).model_dump(),
                        "consolidation",
                        row["run_id"],
                    ),
                },
            )
