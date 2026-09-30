from __future__ import annotations

import hashlib

from rcp.artifacts import html_document_title
from rcp.storage import AppStore, Artifact, EpisodeReportRecord


def stored_report(store: AppStore, *, html: str, **fields) -> EpisodeReportRecord:
    episode = store.episode(fields["episode_id"])
    assert episode is not None
    artifact = store.create_artifact(
        Artifact(
            artifact_id=hashlib.sha256(fields["report_id"].encode()).hexdigest()[:24],
            project_id=episode.project_id,
            supplier="episode_ending",
            supplier_id=episode.episode_id,
            episode_id=episode.episode_id,
            origin_operation_id=fields["allocation_operation_id"],
            source_name="episode-report.html",
            media_type="text/html",
            created_at=fields["created_at"],
            display_title=html_document_title(html),
        ),
        data=html.encode("utf-8"),
    )
    return EpisodeReportRecord(
        **fields, artifact_id=artifact.artifact_id, artifact_version_id=artifact.current_version
    )
