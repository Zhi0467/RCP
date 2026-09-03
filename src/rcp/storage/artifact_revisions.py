from __future__ import annotations

import json
import sqlite3

from rcp.artifacts import AgentArtifactDescriptor
from rcp.core.models import AuthorizedHuman
from rcp.storage.models import (
    ArtifactRevisionCandidateRecord,
    ArtifactRevisionConflict,
)


class ArtifactRevisionStoreMixin:
    """Durable human disposition for one candidate replacement at a time."""

    def create_artifact_revision_candidate(
        self,
        candidate: ArtifactRevisionCandidateRecord,
    ) -> ArtifactRevisionCandidateRecord:
        if candidate.status != "pending" or candidate.decided_at or candidate.decided_by:
            raise ValueError("a new artifact revision candidate must be pending")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owners = connection.execute(
                """
                SELECT operation_id, project_id FROM graph_runs
                WHERE operation_id IN (?, ?)
                """,
                (candidate.source_operation_id, candidate.revision_operation_id),
            ).fetchall()
            if {(str(row["operation_id"]), str(row["project_id"])) for row in owners} != {
                (candidate.source_operation_id, candidate.project_id),
                (candidate.revision_operation_id, candidate.project_id),
            }:
                raise ValueError("artifact revision tasks do not belong to one project")
            try:
                connection.execute(
                    """
                    INSERT INTO artifact_revision_candidates (
                        candidate_id, project_id, source_operation_id, source_artifact_id,
                        revision_operation_id, stage_host, stage_root, artifact_scope_id,
                        source_name, media_type, base_sha256, candidate_sha256,
                        candidate_size_bytes, status, created_at, updated_at,
                        decided_at, decided_by_json, diagnostic
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        candidate.candidate_id,
                        candidate.project_id,
                        candidate.source_operation_id,
                        candidate.source_artifact_id,
                        candidate.revision_operation_id,
                        candidate.stage_host,
                        candidate.stage_root,
                        candidate.artifact_scope_id,
                        candidate.source_name,
                        candidate.media_type,
                        candidate.base_sha256,
                        candidate.candidate_sha256,
                        candidate.candidate_size_bytes,
                        candidate.status,
                        candidate.created_at,
                        candidate.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ArtifactRevisionConflict(
                    "Review the current pending revision before creating another one."
                ) from exc
        return candidate

    def artifact_revision_candidate(
        self,
        candidate_id: str,
    ) -> ArtifactRevisionCandidateRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_revision_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return self._artifact_revision_candidate_record(row) if row is not None else None

    def unresolved_artifact_revision_candidate(
        self,
        source_operation_id: str,
        source_artifact_id: str,
    ) -> ArtifactRevisionCandidateRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM artifact_revision_candidates
                WHERE source_operation_id = ? AND source_artifact_id = ?
                  AND status IN ('pending', 'accepting', 'conflicted')
                ORDER BY created_at DESC, candidate_id DESC
                LIMIT 1
                """,
                (source_operation_id, source_artifact_id),
            ).fetchone()
        return self._artifact_revision_candidate_record(row) if row is not None else None

    def begin_artifact_revision_acceptance(
        self,
        candidate_id: str,
        *,
        decided_by: AuthorizedHuman,
    ) -> ArtifactRevisionCandidateRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._artifact_revision_candidate_row(connection, candidate_id)
            if row["status"] in {"accepted", "accepting"}:
                return self._artifact_revision_candidate_record(row)
            if row["status"] == "rejected":
                raise ArtifactRevisionConflict("This artifact revision was already rejected.")
            if row["status"] == "abandoned":
                raise ArtifactRevisionConflict("This artifact revision was abandoned by recovery.")
            if row["status"] == "conflicted":
                raise ArtifactRevisionConflict(
                    "This artifact changed after the candidate was produced. Reject it and request "
                    "a new revision."
                )
            now = self.now()
            connection.execute(
                """
                UPDATE artifact_revision_candidates
                SET status = 'accepting', updated_at = ?, decided_by_json = ?, diagnostic = NULL
                WHERE candidate_id = ? AND status = 'pending'
                """,
                (
                    now,
                    json.dumps(decided_by.model_dump(mode="json"), separators=(",", ":")),
                    candidate_id,
                ),
            )
            row = self._artifact_revision_candidate_row(connection, candidate_id)
        return self._artifact_revision_candidate_record(row)

    def reset_artifact_revision_acceptance(self, candidate_id: str) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE artifact_revision_candidates
                SET status = 'pending', updated_at = ?, decided_by_json = NULL, diagnostic = NULL
                WHERE candidate_id = ? AND status = 'accepting'
                """,
                (self.now(), candidate_id),
            )

    def complete_artifact_revision_acceptance(
        self,
        candidate_id: str,
    ) -> ArtifactRevisionCandidateRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._artifact_revision_candidate_row(connection, candidate_id)
            if row["status"] == "accepted":
                return self._artifact_revision_candidate_record(row)
            if row["status"] != "accepting":
                raise ArtifactRevisionConflict("Artifact revision acceptance is not in progress.")
            if not row["decided_by_json"]:
                raise ArtifactRevisionConflict("Artifact revision acceptance has no human actor.")
            source_row = connection.execute(
                "SELECT result_json FROM graph_runs WHERE operation_id = ?",
                (row["source_operation_id"],),
            ).fetchone()
            if source_row is None:
                raise ArtifactRevisionConflict("The artifact revision source is unavailable.")
            result = json.loads(source_row["result_json"]) if source_row["result_json"] else None
            if not isinstance(result, dict) or not isinstance(result.get("artifacts"), list):
                raise ArtifactRevisionConflict("The artifact revision source is unavailable.")
            replaced = False
            artifacts: list[dict[str, object]] = []
            for raw in result["artifacts"]:
                descriptor = AgentArtifactDescriptor.model_validate(raw)
                if descriptor.artifact_id == row["source_artifact_id"]:
                    descriptor = descriptor.model_copy(
                        update={"size_bytes": int(row["candidate_size_bytes"])}
                    )
                    replaced = True
                artifacts.append(descriptor.model_dump(mode="json"))
            if not replaced:
                raise ArtifactRevisionConflict("The artifact revision source is unavailable.")
            now = self.now()
            connection.execute(
                "UPDATE graph_runs SET result_json = ?, updated_at = ? WHERE operation_id = ?",
                (
                    self._bounded_result_json({**result, "artifacts": artifacts}),
                    now,
                    row["source_operation_id"],
                ),
            )
            connection.execute(
                """
                UPDATE artifact_revision_candidates
                SET status = 'accepted', updated_at = ?, decided_at = ?, diagnostic = NULL
                WHERE candidate_id = ? AND status = 'accepting'
                """,
                (now, now, candidate_id),
            )
            row = self._artifact_revision_candidate_row(connection, candidate_id)
        return self._artifact_revision_candidate_record(row)

    def conflict_artifact_revision_candidate(
        self,
        candidate_id: str,
        diagnostic: str,
    ) -> ArtifactRevisionCandidateRecord:
        detail = " ".join(diagnostic.split())[:2000]
        if not detail:
            raise ValueError("artifact revision conflict requires a diagnostic")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._artifact_revision_candidate_row(connection, candidate_id)
            if row["status"] in {"accepted", "rejected", "abandoned"}:
                return self._artifact_revision_candidate_record(row)
            now = self.now()
            connection.execute(
                """
                UPDATE artifact_revision_candidates
                SET status = 'conflicted', updated_at = ?, diagnostic = ?
                WHERE candidate_id = ?
                """,
                (now, detail, candidate_id),
            )
            connection.execute(
                "UPDATE graph_runs SET updated_at = ? WHERE operation_id = ?",
                (now, row["source_operation_id"]),
            )
            row = self._artifact_revision_candidate_row(connection, candidate_id)
        return self._artifact_revision_candidate_record(row)

    def reject_artifact_revision_candidate(
        self,
        candidate_id: str,
        *,
        decided_by: AuthorizedHuman,
    ) -> ArtifactRevisionCandidateRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._artifact_revision_candidate_row(connection, candidate_id)
            if row["status"] == "rejected":
                return self._artifact_revision_candidate_record(row)
            if row["status"] == "accepted":
                raise ArtifactRevisionConflict("This artifact revision was already accepted.")
            if row["status"] == "abandoned":
                raise ArtifactRevisionConflict("This artifact revision was abandoned by recovery.")
            if row["status"] == "accepting":
                raise ArtifactRevisionConflict(
                    "Finish recovering this acceptance before rejecting the candidate."
                )
            now = self.now()
            connection.execute(
                """
                UPDATE artifact_revision_candidates
                SET status = 'rejected', updated_at = ?, decided_at = ?, decided_by_json = ?
                WHERE candidate_id = ? AND status IN ('pending', 'conflicted')
                """,
                (
                    now,
                    now,
                    json.dumps(decided_by.model_dump(mode="json"), separators=(",", ":")),
                    candidate_id,
                ),
            )
            row = self._artifact_revision_candidate_row(connection, candidate_id)
        return self._artifact_revision_candidate_record(row)

    def abandon_artifact_revisions_for_restore(
        self,
        connection: sqlite3.Connection,
        *,
        diagnostic: str,
        now: str,
    ) -> int:
        """Retire candidates whose task-stage bytes are outside an offline backup."""

        if not connection.in_transaction:
            raise ValueError("artifact revision restore detachment requires a transaction")
        detail = " ".join(diagnostic.split())[:2000]
        if not detail:
            raise ValueError("artifact revision restore detachment requires a diagnostic")
        return connection.execute(
            """
            UPDATE artifact_revision_candidates
            SET status = 'abandoned', updated_at = ?, decided_at = ?,
                decided_by_json = NULL, diagnostic = ?
            WHERE status IN ('pending', 'accepting', 'conflicted')
            """,
            (now, now, detail),
        ).rowcount

    @staticmethod
    def _artifact_revision_candidate_row(
        connection: sqlite3.Connection,
        candidate_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM artifact_revision_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return row

    @staticmethod
    def _artifact_revision_candidate_record(row: sqlite3.Row) -> ArtifactRevisionCandidateRecord:
        decided_by = json.loads(row["decided_by_json"]) if row["decided_by_json"] else None
        return ArtifactRevisionCandidateRecord(
            candidate_id=row["candidate_id"],
            project_id=row["project_id"],
            source_operation_id=row["source_operation_id"],
            source_artifact_id=row["source_artifact_id"],
            revision_operation_id=row["revision_operation_id"],
            stage_host=row["stage_host"],
            stage_root=row["stage_root"],
            artifact_scope_id=row["artifact_scope_id"],
            source_name=row["source_name"],
            media_type=row["media_type"],
            base_sha256=row["base_sha256"],
            candidate_sha256=row["candidate_sha256"],
            candidate_size_bytes=row["candidate_size_bytes"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            decided_at=row["decided_at"],
            decided_by=decided_by,
            diagnostic=row["diagnostic"],
        )


__all__ = ["ArtifactRevisionStoreMixin"]
