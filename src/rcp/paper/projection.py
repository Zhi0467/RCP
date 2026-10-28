"""Publication badge for project snapshots; content lives in PaperLive."""

from __future__ import annotations

from rcp.storage import AppStore


def publication_badge(store: AppStore, project_id: str) -> bool:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT saved_revision, published_revision FROM paper_publication WHERE project_id=?",
            (project_id,),
        ).fetchone()
    return bool(row and row["saved_revision"] != row["published_revision"])
