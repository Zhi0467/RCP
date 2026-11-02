"""Space-owned Evidence findings and atomic command/address receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from rcp.core.models import GraphState
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation.outcome_edges import is_current_evidence
from rcp.keyed_locks import KeyedLocks
from rcp.limits import EVIDENCE_FLAG_FINDING_MAX_CHARS, EVIDENCE_FLAG_SOURCE_MAX_CHARS
from rcp.storage.consolidation import target_ref
from rcp.storage.membership import live_project_member
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import AgentTaskRecord
from rcp.storage.resource_versions import _bump, _install

if TYPE_CHECKING:
    from rcp.storage import AppStore

# Agent-raised kinds, plus the one RCP raises on Evidence a consolidation apply creates.
EVIDENCE_FLAG_KINDS = frozenset(
    {"overclaim", "underclaim", "no_source", "unverified", "consolidation_added"}
)
# Kinds a human checks rather than an Address Work turn: a consolidation notice reports
# no problem, and an unread source is not something an agent can correct.
NOTICE_FLAG_KINDS = frozenset({"consolidation_added", "unverified"})

# An absolute execution-host path or `<alias>/<relative path>` with an optional `:LINE`,
# or an external http(s) URL.
_SOURCE_PATTERN = re.compile(
    r"(?:/|[A-Za-z0-9][A-Za-z0-9._-]*/)[^\r\n:]*[^\s/:](?::[1-9][0-9]*)?|https?://[^\s]+"
)


class EvidenceFlagError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_FIELDS = (
    "flag_id",
    "run_id",
    "occurrence_date",
    "target",
    "evidence_id",
    "evidence_revision",
    "kind",
    "finding",
    "source",
    "state",
    "chat_id",
    "created_at",
    "origin_flag_id",
)
_LOCKS = KeyedLocks()
_ADMISSION: ContextVar[tuple[object, str, tuple[str, ...], str] | None] = ContextVar(
    "evidence_flag_admission", default=None
)


def migrate_evidence_flags(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS evidence_flags (flag_id TEXT PRIMARY KEY, "
        "project_id TEXT NOT NULL, run_id TEXT, occurrence_date TEXT, evidence_id TEXT NOT NULL, "
        "evidence_revision INTEGER NOT NULL, kind TEXT NOT NULL "
        "CHECK(kind IN ('overclaim','underclaim','no_source','unverified','consolidation_added')), "
        "finding TEXT NOT NULL, source TEXT, "
        "state TEXT NOT NULL CHECK(state IN ('open','addressing','dismissed')), chat_id TEXT, "
        "created_at TEXT NOT NULL, operation_id TEXT, address_request_id TEXT, "
        "UNIQUE(project_id,address_request_id))"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS evidence_flags_live ON "
        "evidence_flags(project_id,evidence_id,kind) WHERE state IN ('open','addressing')"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS evidence_flag_command_receipts (project_id TEXT NOT NULL, "
        "operation_id TEXT NOT NULL, key TEXT NOT NULL, args_digest TEXT NOT NULL, "
        "result_json TEXT NOT NULL, PRIMARY KEY(operation_id,key))"
    )
    _install(connection, "evidence_flags", [_bump("ROW.project_id", "consolidation")])


def migrate_evidence_flag_targets(connection: sqlite3.Connection) -> None:
    """Old findings belong to main; a target owns each live finding independently."""
    connection.execute("ALTER TABLE evidence_flags ADD COLUMN target TEXT NOT NULL DEFAULT 'main'")
    connection.execute("DROP INDEX evidence_flags_live")
    connection.execute(
        "CREATE UNIQUE INDEX evidence_flags_live ON "
        "evidence_flags(project_id,target,evidence_id,kind) WHERE state IN ('open','addressing')"
    )
    for event in ("insert", "update", "delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS resource_evidence_flags_{event}")
    _install(connection, "evidence_flags", [_bump("ROW.project_id", "consolidation")])


def migrate_evidence_flag_origins(connection: sqlite3.Connection) -> None:
    """Keep carried findings and deduplicated deliveries durable after dismissal."""
    connection.execute("ALTER TABLE evidence_flags ADD COLUMN origin_flag_id TEXT")
    connection.execute(
        "CREATE TABLE evidence_flag_carries (project_id TEXT NOT NULL, origin_flag_id TEXT NOT NULL, "
        "destination TEXT NOT NULL, flag_id TEXT NOT NULL, "
        "PRIMARY KEY(project_id,origin_flag_id,destination))"
    )


def _record(row: sqlite3.Row) -> dict[str, Any]:
    fields = frozenset(row.keys())
    return {name: row[name] if name in fields else None for name in _FIELDS}


def _member(connection: sqlite3.Connection, project_id: str, user_id: str) -> None:
    if not live_project_member(connection, project_id, user_id):
        raise EvidenceFlagError("flag_forbidden")


def _flag(connection: sqlite3.Connection, project_id: str, flag_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM evidence_flags WHERE project_id=? AND flag_id=?", (project_id, flag_id)
    ).fetchone()
    if row is None:
        raise EvidenceFlagError("flag_not_found")
    return row


def record_evidence_flag_address(
    store: object, connection: sqlite3.Connection, task: AgentTaskRecord
) -> None:
    """Bind all flags in the transaction that admits their ordinary Work turn.

    A Resume or Retry of the flag's failed turn takes over the binding, unless the
    flag was since dismissed or bound to another attempt.
    """
    if task.parent_operation_id is not None and task.attempt > 1:
        connection.execute(
            "UPDATE evidence_flags SET state='addressing',operation_id=? "
            "WHERE operation_id=? AND state='open'",
            (task.operation_id, task.parent_operation_id),
        )
    admission = _ADMISSION.get()
    if admission is None or admission[0] is not store:
        return
    _, project_id, flag_ids, user_id = admission
    flags = [_flag(connection, project_id, flag_id) for flag_id in flag_ids]
    chat_id = task.request.get("chat_id")
    if (
        task.project_id != project_id
        or task.kind != ("node_chat" if len(flags) == 1 else "project_chat")
        or task.request.get("mode") != "work"
        or (len(flags) == 1 and task.request.get("node_id") != flags[0]["evidence_id"])
        or any(task.graph_target != target_ref(flag["target"]) for flag in flags)
        or task.authorized_by is None
        or task.authorized_by.user_id != user_id
        or not isinstance(chat_id, str)
        or not chat_id
    ):
        raise EvidenceFlagError("flag_address_conflict")
    _member(connection, project_id, user_id)
    # The request receipt is the admitted task's client request row.
    for flag_id in flag_ids:
        changed = connection.execute(
            "UPDATE evidence_flags SET state='addressing',chat_id=?,operation_id=? "
            "WHERE project_id=? AND flag_id=? AND state='open'",
            (chat_id, task.operation_id, project_id, flag_id),
        ).rowcount
        if changed != 1:
            raise EvidenceFlagError("flag_address_conflict")


_ENDED_TASK_STATUSES = frozenset({"succeeded", "failed", "interrupted", "paused"})


def _address_resolved(connection: sqlite3.Connection, operation_id: str) -> bool:
    """A finished turn resolves its flag only when it reports `done` and its graph update landed."""
    row = connection.execute(
        "SELECT result_json FROM graph_runs WHERE operation_id=?", (operation_id,)
    ).fetchone()
    result = json.loads(row["result_json"]) if row is not None and row["result_json"] else {}
    outcome = (result.get("turn_outcome") or {}).get("status")
    graph = (result.get("graph_update") or {}).get("status")
    return outcome == "done" and graph not in {"rejected", "unavailable"}


def settle_evidence_flag_address(
    connection: sqlite3.Connection, operation_id: str, status: str
) -> None:
    """Close a flag when its Address turn resolved it; any other end reopens it.

    A reopened flag keeps its chat, which is how readers tell a failed attempt.
    """
    bound = connection.execute(
        "SELECT 1 FROM evidence_flags WHERE operation_id=? AND state='addressing'",
        (operation_id,),
    ).fetchone()
    if bound is None:
        return
    resolved = status == "succeeded" and _address_resolved(connection, operation_id)
    connection.execute(
        "UPDATE evidence_flags SET state=? WHERE operation_id=? AND state='addressing'",
        ("dismissed" if resolved else "open", operation_id),
    )


def address_request_route(flag_id: str) -> str:
    """The client request route an Address request is recorded under."""
    return f"evidence_flags/{flag_id}/address"


def adopt_legacy_evidence_flag_requests(connection: sqlite3.Connection) -> None:
    """Move Address request ids older releases kept on the flag into client requests.

    Replays read only `client_requests`; startup moves each old id there once,
    under its turn's authorizer, and clears the column.
    """
    rows = connection.execute(
        "SELECT f.flag_id,f.project_id,f.address_request_id,f.operation_id,"
        "r.authorized_user_id,r.created_at FROM evidence_flags f "
        "JOIN graph_runs r ON r.operation_id=f.operation_id "
        "WHERE f.address_request_id IS NOT NULL AND r.authorized_user_id IS NOT NULL"
    ).fetchall()
    connection.executemany(
        "INSERT OR IGNORE INTO client_requests "
        "(project_id,user_id,key,route,operation_id,created_at) VALUES (?,?,?,?,?,?)",
        [
            (
                row["project_id"],
                row["authorized_user_id"],
                row["address_request_id"],
                address_request_route(row["flag_id"]),
                row["operation_id"],
                row["created_at"],
            )
            for row in rows
        ],
    )
    connection.execute("UPDATE evidence_flags SET address_request_id=NULL")


def settle_ended_evidence_flag_addresses(connection: sqlite3.Connection) -> None:
    """Bring addressing flags in line with their turns at startup.

    Older releases neither moved a flag to a Resume or Retry of its turn nor
    settled it when the turn ended, so the binding first follows recovery turns
    to the latest one, then settles if that turn ended. Later starts find
    nothing to change.
    """
    flags = connection.execute(
        "SELECT flag_id,operation_id FROM evidence_flags WHERE state='addressing'"
    ).fetchall()
    for flag in flags:
        operation_id = flag["operation_id"]
        while (
            child := connection.execute(
                "SELECT operation_id FROM graph_runs WHERE parent_operation_id=? AND attempt>1 "
                "ORDER BY created_at DESC,operation_id DESC LIMIT 1",
                (operation_id,),
            ).fetchone()
        ) is not None:
            operation_id = child["operation_id"]
        connection.execute(
            "UPDATE evidence_flags SET operation_id=? WHERE flag_id=?",
            (operation_id, flag["flag_id"]),
        )
        row = connection.execute(
            "SELECT status FROM graph_runs WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is not None and row["status"] in _ENDED_TASK_STATUSES:
            settle_evidence_flag_address(connection, operation_id, row["status"])


class EvidenceFlagStoreMixin(StoreMixinBase):
    def carry_merge_evidence_flags(
        self,
        project_id: str,
        *,
        source: GraphTargetRef,
        destination: GraphTargetRef,
        source_state: GraphState,
        destination_state: GraphState,
        recorded_state: Callable[[int], GraphState],
        created_before: str | None = None,
    ) -> list[str]:
        """Carry unchanged, unresolved source findings once, independently of dismissal.

        Compare the authored Evidence, the pinned source, and the destination.
        A deduplicated carry retains a delivery receipt too, so dismissing that
        destination finding cannot make a crashed effect reopen it on retry.
        Address-session bindings stay local to the source finding.
        """
        from rcp.history.delta import semantic_document

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_key = source.branch_id if source.kind == "branch" else "main"
            destination_key = destination.branch_id if destination.kind == "branch" else "main"
            rows = connection.execute(
                "SELECT * FROM evidence_flags WHERE project_id=? AND target=? "
                "AND state IN ('open','addressing') ORDER BY created_at,flag_id",
                (project_id, source_key),
            ).fetchall()
            carried = []
            for flag in rows:
                if created_before is not None and flag["created_at"] > created_before:
                    continue
                evidence_id = flag["evidence_id"]
                if not all(
                    is_current_evidence(state, evidence_id)
                    for state in (source_state, destination_state)
                ):
                    continue
                authored = recorded_state(flag["evidence_revision"])
                if not is_current_evidence(authored, evidence_id):
                    continue
                content = semantic_document(authored.nodes[evidence_id])
                if any(
                    semantic_document(state.nodes[evidence_id]) != content
                    for state in (source_state, destination_state)
                ):
                    continue
                previous = connection.execute(
                    "SELECT flag_id FROM evidence_flag_carries WHERE project_id=? "
                    "AND origin_flag_id=? AND destination=?",
                    (project_id, flag["flag_id"], destination_key),
                ).fetchone()
                if previous is not None:
                    continue
                duplicate = connection.execute(
                    "SELECT flag_id FROM evidence_flags WHERE project_id=? AND target=? "
                    "AND evidence_id=? AND kind=? AND state IN ('open','addressing')",
                    (project_id, destination_key, evidence_id, flag["kind"]),
                ).fetchone()
                flag_id = (
                    duplicate["flag_id"]
                    if duplicate
                    else uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"rcp:carried-evidence-flag:{project_id}:{flag['flag_id']}:{destination.key}",
                    ).hex
                )
                if duplicate is None:
                    connection.execute(
                        "INSERT OR IGNORE INTO evidence_flags(flag_id,project_id,run_id,occurrence_date,target,"
                        "evidence_id,evidence_revision,kind,finding,source,state,created_at,origin_flag_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                        (
                            flag_id,
                            project_id,
                            flag["run_id"],
                            flag["occurrence_date"],
                            destination_key,
                            evidence_id,
                            destination_state.revision,
                            flag["kind"],
                            flag["finding"],
                            flag["source"],
                            self.now(),
                            flag["flag_id"],
                        ),
                    )
                    carried.append(flag_id)
                connection.execute(
                    "INSERT INTO evidence_flag_carries VALUES (?,?,?,?)",
                    (project_id, flag["flag_id"], destination_key, flag_id),
                )
            return carried

    def list_evidence_flags(self, project_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [
                _record(row)
                for row in connection.execute(
                    "SELECT * FROM evidence_flags WHERE project_id=? AND state IN ('open','addressing') "
                    "ORDER BY created_at,flag_id",
                    (project_id,),
                )
            ]

    def evidence_flag(self, project_id: str, flag_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            return _record(_flag(connection, project_id, flag_id))

    def evidence_flag_address_result(
        self, project_id: str, flag_id: str, operation_id: str | None = None
    ) -> dict[str, Any]:
        """The flag with its latest turn, or with `operation_id`'s turn on a replay."""
        with self.connection() as connection:
            row = _flag(connection, project_id, flag_id)
        task = cast("AppStore", self).agent_task(operation_id) if operation_id else None
        return {
            "flag": _record(row),
            "chat_id": task.request["chat_id"] if task else row["chat_id"],
            "operation_id": task.operation_id if task else row["operation_id"],
        }

    def dismiss_evidence_flag(
        self, project_id: str, flag_id: str, *, user_id: str
    ) -> dict[str, Any]:
        with (
            _LOCKS(f"{self.path.resolve()}:{project_id}:{flag_id}"),
            self.connection() as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            _member(connection, project_id, user_id)
            cast("AppStore", self)._require_project_accepts_new_work(connection, project_id)
            row = _flag(connection, project_id, flag_id)
            if row["state"] == "dismissed":
                raise EvidenceFlagError("flag_dismissed")
            connection.execute(
                "UPDATE evidence_flags SET state='dismissed' WHERE flag_id=?", (flag_id,)
            )
            return _record(_flag(connection, project_id, flag_id))

    @contextmanager
    def evidence_flag_admission(
        self,
        project_id: str,
        flag_ids: list[str],
        *,
        user_id: str,
        require_open: bool = True,
    ) -> Iterator[list[dict[str, Any]]]:
        with ExitStack() as locks:
            for flag_id in sorted(flag_ids):
                locks.enter_context(_LOCKS(f"{self.path.resolve()}:{project_id}:{flag_id}"))
            with self.connection() as connection:
                _member(connection, project_id, user_id)
                cast("AppStore", self)._require_project_accepts_new_work(connection, project_id)
                rows = [_flag(connection, project_id, flag_id) for flag_id in flag_ids]
                for row in rows:
                    if row["state"] == "dismissed":
                        raise EvidenceFlagError("flag_dismissed")
                    if require_open and row["state"] != "open":
                        raise EvidenceFlagError("flag_address_conflict")
                    if row["kind"] in NOTICE_FLAG_KINDS:
                        raise EvidenceFlagError("flag_not_addressable")
            token = _ADMISSION.set((self, project_id, tuple(flag_ids), user_id))
            try:
                yield [_record(row) for row in rows]
            finally:
                _ADMISSION.reset(token)

    def execute_evidence_flag_command(
        self,
        project_id: str,
        operation_id: str,
        *,
        key: str | None,
        evidence_id: str,
        kind: str,
        finding: str,
        source: str | None = None,
        validate_evidence: Callable[[], int],
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT c.run_id,c.occurrence_date,c.target,g.authorized_user_id,g.dispatch_authority_json "
                "FROM consolidation_runs c JOIN graph_runs g ON g.operation_id=c.operation_id "
                "AND g.project_id=c.project_id WHERE c.operation_id=? AND c.project_id=?",
                (operation_id, project_id),
            ).fetchone()
            if run is None or not run["authorized_user_id"]:
                raise EvidenceFlagError("flag_forbidden")
            if (
                json.loads(run["dispatch_authority_json"] or "{}").get("task_contract")
                != "work_auto"
            ):
                raise EvidenceFlagError("flag_forbidden")
            _member(connection, project_id, run["authorized_user_id"])
            cast("AppStore", self)._require_project_accepts_new_work(connection, project_id)
            if not isinstance(key, str) or not key.strip():
                raise EvidenceFlagError("flag_key_invalid")
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "evidence_id": evidence_id,
                        "kind": kind,
                        "finding": finding,
                        "source": source,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            receipt = connection.execute(
                "SELECT args_digest,result_json FROM evidence_flag_command_receipts "
                "WHERE operation_id=? AND key=?",
                (operation_id, key),
            ).fetchone()
            outcome: dict[str, Any]
            if receipt is not None:
                if receipt["args_digest"] != digest:
                    raise EvidenceFlagError("flag_key_conflict")
                outcome = json.loads(receipt["result_json"])
            else:
                try:
                    if kind not in EVIDENCE_FLAG_KINDS:
                        raise EvidenceFlagError("flag_kind_invalid")
                    if (
                        not finding.strip()
                        or len(finding) > EVIDENCE_FLAG_FINDING_MAX_CHARS
                        or finding.splitlines() != [finding]
                    ):
                        raise EvidenceFlagError("flag_finding_invalid")
                    if source is not None and (
                        len(source) > EVIDENCE_FLAG_SOURCE_MAX_CHARS
                        or source.splitlines() != [source]
                        or _SOURCE_PATTERN.fullmatch(source) is None
                    ):
                        raise EvidenceFlagError("flag_source_invalid")
                    revision = validate_evidence()
                    existing = connection.execute(
                        "SELECT * FROM evidence_flags WHERE project_id=? AND target=? AND evidence_id=? "
                        "AND kind=? AND state IN ('open','addressing')",
                        (project_id, run["target"], evidence_id, kind),
                    ).fetchone()
                    if existing is not None:
                        result = {"flag": _record(existing), "code": "flag_exists"}
                    else:
                        flag_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO evidence_flags(flag_id,project_id,run_id,occurrence_date,target,"
                            "evidence_id,evidence_revision,kind,finding,source,state,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?)",
                            (
                                flag_id,
                                project_id,
                                run["run_id"],
                                run["occurrence_date"],
                                run["target"],
                                evidence_id,
                                revision,
                                kind,
                                finding,
                                source,
                                self.now(),
                            ),
                        )
                        result = {"flag": _record(_flag(connection, project_id, flag_id))}
                    outcome = {"result": result}
                except EvidenceFlagError as exc:
                    outcome = {"error": exc.code}
                connection.execute(
                    "INSERT INTO evidence_flag_command_receipts VALUES (?,?,?,?,?)",
                    (project_id, operation_id, key, digest, json.dumps(outcome)),
                )
        if "error" in outcome:
            raise EvidenceFlagError(outcome["error"])
        return outcome["result"]
