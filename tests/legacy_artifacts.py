"""Archived candidate rows from releases before versioned artifact editing."""

from __future__ import annotations

from rcp.storage import AppStore, ArtifactRevisionCandidateRecord


def insert_legacy_candidate(
    store: AppStore, candidate: ArtifactRevisionCandidateRecord
) -> ArtifactRevisionCandidateRecord:
    values = candidate.model_dump(mode="json")
    actor = values.pop("decided_by")
    assert actor is None
    values["decided_by_json"] = None
    columns = tuple(values)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO artifact_revision_candidates ("
            + ", ".join(columns)
            + ") VALUES ("
            + ", ".join("?" for _ in columns)
            + ")",
            tuple(values[column] for column in columns),
        )
    return candidate
