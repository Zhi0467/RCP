"""Small publication badge for project snapshots; document content lives in PaperLive."""

from __future__ import annotations

from rcp.storage import AppStore


def publication_badge(store: AppStore, project_id: str) -> dict[str, object]:
    # One read observes publication and file membership at the same saved revision.
    with store.connection() as conn:
        row = conn.execute(
            """SELECT saved_revision, published_revision,
                      EXISTS(SELECT 1 FROM paper_files
                             WHERE project_id=? AND deleted_at IS NULL) AS has_files
               FROM paper_publication WHERE project_id=?""",
            (project_id, project_id),
        ).fetchone()
    return {
        "content": "",
        "draft_version": None,
        "sync_state": (
            "synced"
            if row is None or row["saved_revision"] == row["published_revision"]
            else "unsynced"
        ),
        "canonical_available": bool(row and row["has_files"]),
    }
