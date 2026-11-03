from __future__ import annotations

import hashlib
import json

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


def requeue_report_in_place_as_older_rcp(store: AppStore, episode_id: str) -> str:
    """Write what an RCP before report-retry rows wrote when it restarted a stopped report.

    It put the same hidden task back to ``queued`` under a dispatch-reset fence and
    failed the provider call the restart cut off.
    """

    wrapup = store.episode_wrapup(episode_id)
    assert wrapup is not None and wrapup.allocation_operation_id is not None
    operation_id = wrapup.allocation_operation_id
    task = store.agent_task(operation_id)
    assert task is not None
    now = store.now()
    with store.connection() as connection:
        connection.execute(
            "UPDATE episode_report_attempts SET status = 'failed', "
            "error = 'The report provider call was interrupted by an RCP restart.', "
            "updated_at = ?, finished_at = ? WHERE episode_id = ? AND status = 'running'",
            (now, now, episode_id),
        )
        connection.execute(
            "UPDATE graph_runs SET status = 'queued', "
            "status_message = 'Wrapping up visualization and report', error = NULL, "
            "failure_kind = NULL, updated_at = ?, started_at = NULL, finished_at = NULL, "
            "last_activity_at = NULL, phase = 'queued', write_scope_fingerprint = NULL "
            "WHERE operation_id = ?",
            (now, operation_id),
        )
        connection.execute(
            "INSERT INTO graph_run_receipts (operation_id, created_at, tier, category, "
            "payload_json) VALUES (?, ?, 'summary', 'operation_dispatch_reset', ?)",
            (
                operation_id,
                now,
                json.dumps(
                    {
                        "status": "queued",
                        "reason": "episode_report_restart",
                        "previous_status": task.status,
                    }
                ),
            ),
        )
        connection.execute(
            "UPDATE episodes SET wrapup_state = 'pending', wrapup_error = NULL, updated_at = ? "
            "WHERE episode_id = ?",
            (now, episode_id),
        )
        connection.execute(
            "UPDATE episode_wrapups SET state = 'pending', diagnostic = NULL, updated_at = ?, "
            "finished_at = NULL WHERE episode_id = ?",
            (now, episode_id),
        )
    return operation_id
