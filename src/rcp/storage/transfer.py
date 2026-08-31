from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Iterable

from pydantic import JsonValue

from rcp.transfer.archive import (
    TransferArchiveAttribution,
    TransferGraphHead,
    TransferGraphTarget,
    inspect_transfer_table_inventory,
)
from rcp.transfer.records import (
    TransferArtifactReference,
    TransferAssistantHistory,
    TransferAutoResearchApplyResult,
    TransferAutoResearchChildAdmission,
    TransferAutoResearchChildExperiment,
    TransferAutoResearchChildExperimentRequest,
    TransferAutoResearchChildWork,
    TransferAutoResearchChildWorkAttempt,
    TransferAutoResearchCommand,
    TransferAutoResearchExperimentInvocation,
    TransferAutoResearchFinishReceipt,
    TransferAutoResearchHistory,
    TransferAutoResearchInboxReceipt,
    TransferAutoResearchInvocation,
    TransferAutoResearchLifecycleNotice,
    TransferAutoResearchMessage,
    TransferAutoResearchRecovery,
    TransferEpisodeInvocation,
    TransferEpisodeRecord,
    TransferEpisodeReport,
    TransferEpisodeReportAttempt,
    TransferEpisodeWrapup,
    TransferExperimentEpisodeHistory,
    TransferJsonDocument,
    TransferLocalId,
    TransferPaperDraft,
    TransferRecordBundle,
    TransferTaskContract,
    TransferTaskEvent,
    TransferTaskOutput,
    TransferTaskReceipt,
    TransferTaskRecord,
    TransferTaskUsage,
    TransferWatcherRecord,
    capture_task_request_history,
    validate_transfer_table_policy,
)


def _json_value(raw: str) -> JsonValue:
    value: JsonValue = json.loads(raw)
    return value


def _json_object(raw: str | None) -> dict[str, JsonValue]:
    if raw is None:
        return {}
    value = _json_value(raw)
    if not isinstance(value, dict):
        raise ValueError("stored transfer JSON must be an object")
    return value


def _optional_json_document(raw: str | None) -> TransferJsonDocument | None:
    if raw is None:
        return None
    return TransferJsonDocument.capture_sanitized(_json_value(raw))


def _rows_by(
    rows: Iterable[sqlite3.Row],
    key: str,
) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    return grouped


class ProjectTransferStoreMixin:
    """One read-only, snapshot-consistent projection of finished project history."""

    def export_project_transfer_records(
        self,
        project_id: str,
        *,
        attributions: tuple[TransferArchiveAttribution, ...],
    ) -> TransferRecordBundle:
        with self.connection() as connection:
            connection.execute("BEGIN")
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            inventory = inspect_transfer_table_inventory(connection)
            validate_transfer_table_policy(inventory.project_linked_tables)
            self._validate_transfer_relational_integrity(connection, project_id)
            self._require_finished_transfer_state(connection, project_id)
            tasks = self._transfer_tasks(connection, project_id, attributions)
            watchers = self._transfer_watchers(connection, project_id)
            episodes = self._transfer_episodes(connection, project_id, attributions)
            paper = self._transfer_paper_draft(connection, project_id)
        return TransferRecordBundle(
            project_id=project_id,
            attributions=attributions,
            tasks=tasks,
            watchers=watchers,
            episodes=episodes,
            paper_draft=paper,
        )

    @staticmethod
    def _validate_transfer_relational_integrity(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> None:
        ownership_checks = (
            (
                "SELECT usage.usage_id FROM agent_usage AS usage "
                "LEFT JOIN graph_runs AS run ON run.operation_id = usage.operation_id "
                "WHERE (usage.project_id = ? OR run.project_id = ?) "
                "AND (run.project_id IS NULL OR usage.project_id != run.project_id) LIMIT 1",
                "agent usage",
            ),
            (
                "SELECT child.worker_id FROM auto_research_child_work AS child "
                "LEFT JOIN episodes AS episode ON episode.episode_id = child.episode_id "
                "WHERE (child.project_id = ? OR episode.project_id = ?) "
                "AND (episode.project_id IS NULL OR child.project_id != episode.project_id) "
                "LIMIT 1",
                "Auto-research child Work",
            ),
            (
                "SELECT child.child_episode_id FROM auto_research_child_experiments AS child "
                "LEFT JOIN episodes AS episode "
                "ON episode.episode_id = child.auto_research_episode_id "
                "WHERE (child.project_id = ? OR episode.project_id = ?) "
                "AND (episode.project_id IS NULL OR child.project_id != episode.project_id) "
                "LIMIT 1",
                "Auto-research child Experiment",
            ),
            (
                "SELECT admission.admission_id "
                "FROM auto_research_child_admissions AS admission "
                "LEFT JOIN episodes AS episode ON episode.episode_id = admission.episode_id "
                "WHERE (admission.project_id = ? OR episode.project_id = ?) "
                "AND (episode.project_id IS NULL OR admission.project_id != episode.project_id) "
                "LIMIT 1",
                "Auto-research child admission",
            ),
        )
        for query, label in ownership_checks:
            if connection.execute(query, (project_id, project_id)).fetchone() is not None:
                raise ValueError(f"stored {label} belongs to conflicting projects")

    @staticmethod
    def _require_finished_transfer_state(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> None:
        checks = (
            (
                "SELECT operation_id FROM graph_runs WHERE project_id = ? "
                "AND status NOT IN ('succeeded', 'failed', 'interrupted') LIMIT 1",
                "agent task",
            ),
            (
                "SELECT watcher_id FROM watchers WHERE project_id = ? "
                "AND (status NOT IN ('completed', 'stopped') "
                "OR (status = 'completed' AND notified = 0)) LIMIT 1",
                "watcher",
            ),
            (
                "SELECT episode_id FROM episodes WHERE project_id = ? "
                "AND status NOT IN ('completed', 'stopped', 'failed') LIMIT 1",
                "episode",
            ),
            (
                "SELECT attempt.attempt_id FROM episode_report_attempts AS attempt "
                "JOIN episodes AS episode ON episode.episode_id = attempt.episode_id "
                "WHERE episode.project_id = ? "
                "AND attempt.status NOT IN ('succeeded', 'failed') LIMIT 1",
                "episode report attempt",
            ),
            (
                "SELECT episode_id FROM episodes WHERE project_id = ? "
                "AND wrapup_state IN ('pending', 'running') LIMIT 1",
                "episode wrap-up",
            ),
            (
                "SELECT recovery.recovery_id FROM auto_research_recoveries AS recovery "
                "JOIN episodes AS episode ON episode.episode_id = recovery.episode_id "
                "WHERE episode.project_id = ? AND recovery.status = 'pending' LIMIT 1",
                "Auto-research recovery",
            ),
            (
                "SELECT child.child_episode_id FROM auto_research_child_experiments AS child "
                "WHERE child.project_id = ? AND child.state IN ('pending', 'running') LIMIT 1",
                "Auto-research child Experiment",
            ),
            (
                "SELECT admission.admission_id FROM auto_research_child_admissions AS admission "
                "WHERE admission.project_id = ? AND admission.state = 'accepted' LIMIT 1",
                "Auto-research child admission",
            ),
            (
                "SELECT notice.notice_id FROM auto_research_lifecycle_notices AS notice "
                "JOIN episodes AS episode ON episode.episode_id = notice.episode_id "
                "WHERE episode.project_id = ? AND notice.acknowledged_at IS NULL LIMIT 1",
                "Auto-research lifecycle notice",
            ),
            (
                "SELECT message.message_id FROM auto_research_messages AS message "
                "JOIN episodes AS episode ON episode.episode_id = message.episode_id "
                "WHERE episode.project_id = ? AND message.delivered_at IS NULL LIMIT 1",
                "Auto-research message",
            ),
        )
        for query, label in checks:
            row = connection.execute(query, (project_id,)).fetchone()
            if row is not None:
                raise ValueError(f"project transfer requires every {label} to be settled")

    @classmethod
    def _transfer_tasks(
        cls,
        connection: sqlite3.Connection,
        project_id: str,
        attributions: tuple[TransferArchiveAttribution, ...],
    ) -> tuple[TransferTaskRecord, ...]:
        task_rows = connection.execute(
            "SELECT * FROM graph_runs WHERE project_id = ? ORDER BY created_at, operation_id",
            (project_id,),
        ).fetchall()
        event_rows = connection.execute(
            "SELECT event.* FROM graph_run_events AS event "
            "JOIN graph_runs AS run ON run.operation_id = event.operation_id "
            "WHERE run.project_id = ? ORDER BY event.operation_id, event.event_id",
            (project_id,),
        ).fetchall()
        receipt_rows = connection.execute(
            "SELECT receipt.* FROM graph_run_receipts AS receipt "
            "JOIN graph_runs AS run ON run.operation_id = receipt.operation_id "
            "WHERE run.project_id = ? ORDER BY receipt.operation_id, receipt.receipt_id",
            (project_id,),
        ).fetchall()
        usage_rows = connection.execute(
            "SELECT usage.* FROM agent_usage AS usage "
            "JOIN graph_runs AS run ON run.operation_id = usage.operation_id "
            "WHERE run.project_id = ? ORDER BY usage.operation_id, usage.created_at, usage.usage_id",
            (project_id,),
        ).fetchall()
        contract_rows = connection.execute(
            "SELECT contract.* FROM graph_run_contracts AS contract "
            "JOIN graph_runs AS run ON run.operation_id = contract.operation_id "
            "WHERE run.project_id = ? ORDER BY contract.operation_id, contract.role",
            (project_id,),
        ).fetchall()
        output_rows = connection.execute(
            "SELECT output.* FROM graph_run_outputs AS output "
            "JOIN graph_runs AS run ON run.operation_id = output.operation_id "
            "WHERE run.project_id = ?",
            (project_id,),
        ).fetchall()
        events = _rows_by(event_rows, "operation_id")
        receipts = _rows_by(receipt_rows, "operation_id")
        usages = _rows_by(usage_rows, "operation_id")
        contracts = _rows_by(contract_rows, "operation_id")
        outputs = {str(row["operation_id"]): row for row in output_rows}

        records: list[TransferTaskRecord] = []
        for row in task_rows:
            operation_id = str(row["operation_id"])
            request = _json_object(row["request_json"])
            result = _json_object(row["result_json"])
            output = outputs.get(operation_id)
            records.append(
                TransferTaskRecord(
                    operation_id=operation_id,
                    kind=row["kind"],
                    status=row["status"],
                    request=capture_task_request_history(row["kind"], request),
                    assistant=cls._transfer_assistant_history(result),
                    error=row["error"],
                    applied_revision=row["applied_revision"],
                    graph_updates=cls._transfer_graph_updates(result),
                    attempt=row["attempt"],
                    parent_operation_id=row["parent_operation_id"],
                    episode_id=row["episode_id"],
                    graph_target=TransferGraphTarget.model_validate_json(row["graph_target_json"]),
                    authorized_by_attribution_id=cls._transfer_attribution_id(
                        row,
                        attributions,
                    ),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                    status_message=row["status_message"],
                    events=tuple(
                        cls._transfer_task_event(project_id, item) for item in events[operation_id]
                    ),
                    receipts=tuple(
                        cls._transfer_task_receipt(project_id, item)
                        for item in receipts[operation_id]
                    ),
                    usage=tuple(cls._transfer_task_usage(item) for item in usages[operation_id]),
                    contracts=tuple(
                        TransferTaskContract(
                            role=item["role"],
                            content=item["content"],
                            sha256=item["sha256"],
                            created_at=item["created_at"],
                        )
                        for item in contracts[operation_id]
                    ),
                    output=(
                        TransferTaskOutput(
                            created_at=output["created_at"],
                            patch=TransferJsonDocument.capture_sanitized(
                                _json_value(output["patch_json"])
                            ),
                        )
                        if output is not None
                        else None
                    ),
                    artifacts=cls._transfer_artifacts(result),
                    visible=bool(row["visible"]),
                    history_only=True,
                )
            )
        return tuple(records)

    @staticmethod
    def _transfer_assistant_history(result: dict[str, JsonValue]) -> TransferAssistantHistory:
        answer = result.get("answer")
        traces = result.get("trace_messages")
        legacy = result.get("messages")
        return TransferAssistantHistory(
            answer=answer if isinstance(answer, str) else None,
            trace_messages=tuple(item for item in traces if isinstance(item, str))
            if isinstance(traces, list)
            else (),
            legacy_unlabelled_lines=tuple(item for item in legacy if isinstance(item, str))
            if isinstance(legacy, list)
            else (),
        )

    @staticmethod
    def _transfer_graph_updates(
        result: dict[str, JsonValue],
    ) -> tuple[TransferJsonDocument, ...]:
        updates = result.get("graph_updates")
        values = list(updates) if isinstance(updates, list) else []
        latest = result.get("graph_update")
        if latest is not None and not values:
            values.append(latest)
        return tuple(TransferJsonDocument.capture_sanitized(item) for item in values)

    @staticmethod
    def _transfer_artifacts(
        result: dict[str, JsonValue],
    ) -> tuple[TransferArtifactReference, ...]:
        raw = result.get("artifacts")
        if not isinstance(raw, list):
            return ()
        artifacts: list[TransferArtifactReference] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("stored artifact history must be an object")
            artifacts.append(
                TransferArtifactReference(
                    artifact_id=item.get("artifact_id"),
                    source_name=item.get("name"),
                    media_type=item.get("media_type"),
                    size_bytes=item.get("size_bytes"),
                    content_sha256=item.get("content_sha256"),
                    expires_at=item.get("expires_at"),
                    kept_filename=item.get("kept_filename"),
                    kept_at=item.get("kept_at"),
                )
            )
        return tuple(artifacts)

    @staticmethod
    def _transfer_task_event(project_id: str, row: sqlite3.Row) -> TransferTaskEvent:
        return TransferTaskEvent(
            identity=TransferLocalId(
                archive_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"rcp:transfer:{project_id}:graph_run_events:{row['event_id']}",
                    )
                ),
                source_table="graph_run_events",
                source_id=str(row["event_id"]),
            ),
            created_at=row["created_at"],
            level=row["level"],
            message=row["message"],
            event_kind=row["event_kind"],
            command_id=row["command_id"],
            command_verb=row["command_verb"],
            command_phase=row["command_phase"],
            payload=_optional_json_document(row["payload_json"]),
        )

    @staticmethod
    def _transfer_task_receipt(project_id: str, row: sqlite3.Row) -> TransferTaskReceipt:
        return TransferTaskReceipt(
            identity=TransferLocalId(
                archive_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"rcp:transfer:{project_id}:graph_run_receipts:{row['receipt_id']}",
                    )
                ),
                source_table="graph_run_receipts",
                source_id=str(row["receipt_id"]),
            ),
            created_at=row["created_at"],
            tier=row["tier"],
            category=row["category"],
            payload=TransferJsonDocument.capture_sanitized(_json_value(row["payload_json"])),
        )

    @staticmethod
    def _transfer_task_usage(row: sqlite3.Row) -> TransferTaskUsage:
        return TransferTaskUsage(
            usage_id=row["usage_id"],
            provider=row["provider"],
            model=row["model"],
            provider_profile=row["provider_profile"],
            provider_event_type=row["provider_event_type"],
            counted=bool(row["counted"]),
            count_reason=row["count_reason"],
            processed_input_tokens=row["processed_input_tokens"],
            generated_tokens=row["generated_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            cache_creation_input_tokens=row["cache_creation_input_tokens"],
            cache_write_input_tokens=row["cache_write_input_tokens"],
            reasoning_output_tokens=row["reasoning_output_tokens"],
            reported_input_tokens=row["reported_input_tokens"],
            reported_output_tokens=row["reported_output_tokens"],
            reported_total_tokens=row["reported_total_tokens"],
            provider_fields=TransferJsonDocument.capture_sanitized(
                _json_value(row["provider_fields_json"])
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _transfer_attribution_id(
        row: sqlite3.Row,
        attributions: tuple[TransferArchiveAttribution, ...],
    ) -> str | None:
        space_id = row["authorized_space_id"]
        user_id = row["authorized_user_id"]
        display_name = row["authorized_display_name"]
        if (space_id, user_id, display_name) == (None, None, None):
            return None
        if any(value is None for value in (space_id, user_id, display_name)):
            raise ValueError("stored human attribution is incomplete")
        for attribution in attributions:
            actor = attribution.source_actor
            if (actor.space_id, actor.user_id) == (space_id, user_id):
                return attribution.archive_actor_id
        raise ValueError("stored history references an unmapped human attribution")

    @staticmethod
    def _transfer_watchers(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> tuple[TransferWatcherRecord, ...]:
        rows = connection.execute(
            "SELECT * FROM watchers WHERE project_id = ? ORDER BY created_at, watcher_id",
            (project_id,),
        ).fetchall()
        return tuple(
            TransferWatcherRecord(
                watcher_id=row["watcher_id"],
                kind="graph" if row["graph_condition_json"] is not None else "external",
                origin_operation_id=row["origin_operation_id"],
                origin_task_kind=row["origin_task_kind"],
                chat_id=row["chat_id"],
                node_id=row["node_id"],
                episode_id=row["episode_id"],
                graph_target=TransferGraphTarget.model_validate_json(row["graph_target_json"]),
                status=row["status"],
                graph_condition=_optional_json_document(row["graph_condition_json"]),
                last_checked_at=row["last_checked_at"],
                last_exit_code=row["last_exit_code"],
                last_error=row["last_error"],
                consecutive_error_count=row["consecutive_error_count"],
                group_id=row["group_id"],
                group_label=row["group_label"],
                stopped_by=row["stopped_by"],
                stop_reason=row["stop_reason"],
                created_at=row["created_at"],
                completed_at=row["completed_at"],
                stopped_at=row["stopped_at"],
                stop_operation_id=row["stop_operation_id"],
            )
            for row in rows
        )

    @classmethod
    def _transfer_episodes(
        cls,
        connection: sqlite3.Connection,
        project_id: str,
        attributions: tuple[TransferArchiveAttribution, ...],
    ) -> tuple[TransferEpisodeRecord, ...]:
        rows = connection.execute(
            "SELECT * FROM episodes WHERE project_id = ? ORDER BY created_at, episode_id",
            (project_id,),
        ).fetchall()
        return tuple(cls._transfer_episode(connection, row, attributions) for row in rows)

    @classmethod
    def _transfer_episode(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        attributions: tuple[TransferArchiveAttribution, ...],
    ) -> TransferEpisodeRecord:
        episode_id = str(row["episode_id"])
        invocations = connection.execute(
            "SELECT * FROM episode_invocations WHERE episode_id = ? ORDER BY invocation_number",
            (episode_id,),
        ).fetchall()
        attempts = connection.execute(
            "SELECT * FROM episode_report_attempts WHERE episode_id = ? ORDER BY attempt_number",
            (episode_id,),
        ).fetchall()
        wrapup = connection.execute(
            "SELECT * FROM episode_wrapups WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        report = connection.execute(
            "SELECT * FROM episode_reports WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        cls._validate_episode_report_history(row, attempts, wrapup, report)
        return TransferEpisodeRecord(
            episode_id=episode_id,
            mode=row["mode"],
            control_node_id=row["control_node_id"],
            graph_target=TransferGraphTarget.model_validate_json(row["graph_target_json"]),
            graph_base_head=(
                TransferGraphHead.model_validate_json(row["graph_base_head_json"])
                if row["graph_base_head_json"] is not None
                else None
            ),
            root_operation_id=row["root_operation_id"],
            status=row["status"],
            invocation_ceiling=row["invocation_ceiling"],
            invocations_used=row["invocations_used"],
            authorized_by_attribution_id=cls._transfer_attribution_id(row, attributions),
            ending=row["ending"],
            ending_diagnostic=row["ending_diagnostic"],
            wrapup_state=row["wrapup_state"],
            wrapup_error=row["wrapup_error"],
            report_attempts_used=row["report_attempts_used"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            invocations=tuple(
                TransferEpisodeInvocation(
                    operation_id=item["operation_id"],
                    invocation_number=item["invocation_number"],
                    created_at=item["created_at"],
                )
                for item in invocations
            ),
            report_attempts=tuple(
                TransferEpisodeReportAttempt(
                    attempt_id=item["attempt_id"],
                    attempt_number=item["attempt_number"],
                    allocation_operation_id=item["allocation_operation_id"],
                    status=item["status"],
                    error=item["error"],
                    created_at=item["created_at"],
                    updated_at=item["updated_at"],
                    finished_at=item["finished_at"],
                )
                for item in attempts
            ),
            wrapup=cls._transfer_episode_wrapup(wrapup),
            report=cls._transfer_episode_report(report),
            experiment=(
                cls._transfer_experiment_history(connection, episode_id)
                if row["mode"] == "experiment_loop"
                else None
            ),
            auto_research=(
                cls._transfer_auto_research_history(connection, episode_id, attributions)
                if row["mode"] == "auto_research"
                else None
            ),
        )

    @staticmethod
    def _validate_episode_report_history(
        episode: sqlite3.Row,
        attempts: list[sqlite3.Row],
        wrapup: sqlite3.Row | None,
        report: sqlite3.Row | None,
    ) -> None:
        wrapup_state = episode["wrapup_state"]
        if wrapup is None:
            if wrapup_state != "not_started" or attempts or report is not None:
                raise ValueError("stored episode report history is incomplete")
            return
        if wrapup["state"] != wrapup_state or wrapup["ending"] != episode["ending"]:
            raise ValueError("stored episode and wrap-up lifecycle disagree")
        if report is None:
            if wrapup_state == "ready" or any(item["status"] == "succeeded" for item in attempts):
                raise ValueError("stored succeeded episode report history is incomplete")
            return
        attempt = next(
            (item for item in attempts if item["attempt_id"] == report["attempt_id"]),
            None,
        )
        if (
            wrapup_state != "ready"
            or attempt is None
            or attempt["status"] != "succeeded"
            or report["ending"] != episode["ending"]
            or report["allocation_operation_id"] != attempt["allocation_operation_id"]
            or report["allocation_operation_id"] != wrapup["allocation_operation_id"]
        ):
            raise ValueError("stored episode report lineage is inconsistent")

    @staticmethod
    def _transfer_episode_wrapup(row: sqlite3.Row | None) -> TransferEpisodeWrapup | None:
        if row is None:
            return None
        if hashlib.sha256(row["receipt_json"].encode()).hexdigest() != row["receipt_sha256"]:
            raise ValueError("stored episode wrap-up receipt does not match its digest")
        return TransferEpisodeWrapup(
            ending=row["ending"],
            partial=bool(row["partial"]),
            concluding_operation_id=row["concluding_operation_id"],
            allocation_operation_id=row["allocation_operation_id"],
            provider=row["provider"],
            skill_id=row["skill_id"],
            skill_version=row["skill_version"],
            receipt=TransferJsonDocument.capture_sanitized(_json_value(row["receipt_json"])),
            state=row["state"],
            diagnostic=row["diagnostic"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _transfer_episode_report(row: sqlite3.Row | None) -> TransferEpisodeReport | None:
        if row is None:
            return None
        return TransferEpisodeReport(
            report_id=row["report_id"],
            attempt_id=row["attempt_id"],
            allocation_operation_id=row["allocation_operation_id"],
            ending=row["ending"],
            sha256=row["sha256"],
            html=row["html"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _transfer_experiment_history(
        connection: sqlite3.Connection,
        episode_id: str,
    ) -> TransferExperimentEpisodeHistory:
        row = connection.execute(
            "SELECT * FROM experiment_episode_state WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Experiment episode state is missing")
        watcher_ids = _json_value(row["last_watcher_ids_json"])
        if not isinstance(watcher_ids, list) or any(
            not isinstance(item, str) for item in watcher_ids
        ):
            raise ValueError("stored Experiment watcher history is invalid")
        return TransferExperimentEpisodeHistory(
            provider=row["provider"],
            chat_id=row["chat_id"],
            last_turn_operation_id=row["last_turn_operation_id"],
            last_turn_invocation=row["last_turn_invocation"],
            last_graph_result=row["last_graph_result"],
            last_watcher_ids=tuple(watcher_ids),
            session_diagnostic=row["session_diagnostic"],
        )

    @classmethod
    def _transfer_auto_research_history(
        cls,
        connection: sqlite3.Connection,
        episode_id: str,
        attributions: tuple[TransferArchiveAttribution, ...],
    ) -> TransferAutoResearchHistory:
        metadata = connection.execute(
            "SELECT * FROM auto_research_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if metadata is None:
            raise ValueError("Auto-research episode metadata is missing")
        invocation_rows = connection.execute(
            "SELECT * FROM auto_research_invocations WHERE episode_id = ? "
            "ORDER BY created_at, operation_id",
            (episode_id,),
        ).fetchall()
        message_rows = connection.execute(
            "SELECT * FROM auto_research_messages WHERE episode_id = ? "
            "ORDER BY created_at, message_id",
            (episode_id,),
        ).fetchall()
        recovery_rows = connection.execute(
            "SELECT * FROM auto_research_recoveries WHERE episode_id = ? "
            "ORDER BY created_at, recovery_id",
            (episode_id,),
        ).fetchall()
        work_rows = connection.execute(
            "SELECT * FROM auto_research_child_work WHERE episode_id = ? "
            "ORDER BY created_at, worker_id",
            (episode_id,),
        ).fetchall()
        experiment_rows = connection.execute(
            "SELECT * FROM auto_research_child_experiments "
            "WHERE auto_research_episode_id = ? ORDER BY created_at, child_episode_id",
            (episode_id,),
        ).fetchall()
        admission_rows = connection.execute(
            "SELECT * FROM auto_research_child_admissions WHERE episode_id = ? "
            "ORDER BY created_at, admission_id",
            (episode_id,),
        ).fetchall()
        notice_rows = connection.execute(
            "SELECT * FROM auto_research_lifecycle_notices WHERE episode_id = ? "
            "ORDER BY created_at, notice_id",
            (episode_id,),
        ).fetchall()
        inbox_rows = connection.execute(
            "SELECT * FROM auto_research_inbox_receipts WHERE episode_id = ? "
            "ORDER BY created_at, effect_id",
            (episode_id,),
        ).fetchall()
        finish_rows = connection.execute(
            "SELECT * FROM auto_research_finish_receipts WHERE episode_id = ? "
            "ORDER BY created_at, effect_id",
            (episode_id,),
        ).fetchall()
        apply_rows = connection.execute(
            "SELECT * FROM auto_research_apply_results WHERE episode_id = ? "
            "ORDER BY created_at, apply_id",
            (episode_id,),
        ).fetchall()
        command_rows = connection.execute(
            "SELECT * FROM auto_research_command_files WHERE episode_id = ? "
            "ORDER BY created_at, command_id",
            (episode_id,),
        ).fetchall()
        return TransferAutoResearchHistory(
            starting_instruction=metadata["starting_instruction"],
            created_at=metadata["created_at"],
            updated_at=metadata["updated_at"],
            invocations=tuple(
                TransferAutoResearchInvocation(
                    operation_id=row["operation_id"],
                    allocation_operation_id=row["allocation_operation_id"],
                    role=row["role"],
                    actor_operation_id=row["actor_operation_id"],
                    control_node_id=row["control_node_id"],
                    created_at=row["created_at"],
                )
                for row in invocation_rows
            ),
            messages=tuple(
                TransferAutoResearchMessage(
                    message_id=row["message_id"],
                    sender_role=row["sender_role"],
                    sender_task_id=row["sender_task_id"],
                    authorized_by_attribution_id=cls._transfer_attribution_id(
                        row,
                        attributions,
                    )
                    if row["authorized_space_id"] is not None
                    else None,
                    recipient_task_id=row["recipient_task_id"],
                    control_node_id=row["control_node_id"],
                    body=row["body"],
                    disposition="delivered",
                    created_at=row["created_at"],
                    delivered_at=row["delivered_at"],
                )
                for row in message_rows
            ),
            recoveries=tuple(
                TransferAutoResearchRecovery(
                    recovery_id=row["recovery_id"],
                    operation_id=row["operation_id"],
                    failure_kind=row["failure_kind"],
                    retry_mode=row["retry_mode"],
                    attempts=row["attempts"],
                    max_attempts=row["max_attempts"],
                    status=row["status"],
                    diagnostic=row["diagnostic"],
                    admitted_operation_id=row["admitted_operation_id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in recovery_rows
            ),
            child_work=tuple(cls._transfer_child_work(connection, row) for row in work_rows),
            child_experiments=tuple(
                cls._transfer_child_experiment(connection, row) for row in experiment_rows
            ),
            child_admissions=tuple(
                TransferAutoResearchChildAdmission(
                    admission_id=row["admission_id"],
                    child_kind=row["child_kind"],
                    child_id=row["child_id"],
                    state=row["state"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in admission_rows
            ),
            lifecycle_notices=tuple(
                TransferAutoResearchLifecycleNotice(
                    notice_id=row["notice_id"],
                    source_kind=row["source_kind"],
                    source_id=row["source_id"],
                    source_event=row["source_event"],
                    source_attempt=row["source_attempt"],
                    payload=TransferJsonDocument.capture_sanitized(
                        _json_value(row["payload_json"])
                    ),
                    created_at=row["created_at"],
                    delivered_at=row["delivered_at"],
                    acknowledged_at=row["acknowledged_at"],
                    acknowledged_by=row["acknowledged_by"],
                )
                for row in notice_rows
            ),
            inbox_receipts=tuple(
                TransferAutoResearchInboxReceipt(
                    effect_id=row["effect_id"],
                    mode=row["mode"],
                    result=TransferJsonDocument.capture_sanitized(_json_value(row["result_json"])),
                    acknowledged_by=row["acknowledged_by"],
                    created_at=row["created_at"],
                )
                for row in inbox_rows
            ),
            finish_receipts=tuple(
                TransferAutoResearchFinishReceipt(
                    effect_id=row["effect_id"],
                    actor_operation_id=row["actor_operation_id"],
                    disposition=row["disposition"],
                    blocker_count=row["blocker_count"],
                    result=TransferJsonDocument.capture(_json_value(row["result_json"])),
                    result_sha256=row["result_sha256"],
                    created_at=row["created_at"],
                )
                for row in finish_rows
            ),
            apply_results=tuple(
                TransferAutoResearchApplyResult(
                    apply_id=row["apply_id"],
                    operation_id=row["operation_id"],
                    patch_sha256=row["patch_sha256"],
                    result=TransferJsonDocument.capture_sanitized(_json_value(row["result_json"])),
                    created_at=row["created_at"],
                )
                for row in apply_rows
            ),
            commands=tuple(
                TransferAutoResearchCommand(
                    command_id=row["command_id"],
                    operation_id=row["operation_id"],
                    kind=row["kind"],
                    filename=row["filename"],
                    sha256=row["sha256"],
                    content=row["content"],
                    created_at=row["created_at"],
                )
                for row in command_rows
            ),
        )

    @staticmethod
    def _transfer_child_work(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> TransferAutoResearchChildWork:
        attempts = connection.execute(
            "SELECT * FROM auto_research_child_work_attempts WHERE worker_id = ? "
            "ORDER BY created_at, operation_id",
            (row["worker_id"],),
        ).fetchall()
        return TransferAutoResearchChildWork(
            worker_id=row["worker_id"],
            control_node_id=row["control_node_id"],
            root_operation_id=row["root_operation_id"],
            final_operation_id=row["current_operation_id"],
            admitted_by_operation_id=row["admitted_by_operation_id"],
            instruction=row["instruction"],
            instruction_sha256=row["instruction_sha256"],
            attempts=tuple(
                TransferAutoResearchChildWorkAttempt(
                    operation_id=item["operation_id"],
                    allocation_operation_id=item["allocation_operation_id"],
                    created_at=item["created_at"],
                )
                for item in attempts
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _transfer_child_experiment(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> TransferAutoResearchChildExperiment:
        invocations = connection.execute(
            "SELECT * FROM auto_research_experiment_invocations WHERE child_episode_id = ? "
            "ORDER BY created_at, operation_id",
            (row["child_episode_id"],),
        ).fetchall()
        request = _json_object(row["request_json"])
        return TransferAutoResearchChildExperiment(
            child_episode_id=row["child_episode_id"],
            control_node_id=row["control_node_id"],
            state=row["state"],
            replaces_episode_id=row["replaces_episode_id"],
            request=TransferAutoResearchChildExperimentRequest(
                goal=request.get("goal"),
                invocation_limit=request.get("invocation_limit"),
            ),
            goal_sha256=row["goal_sha256"],
            parent_operation_id=row["parent_operation_id"],
            terminal_diagnostic=row["terminal_diagnostic"],
            invocations=tuple(
                TransferAutoResearchExperimentInvocation(
                    operation_id=item["operation_id"],
                    created_at=item["created_at"],
                )
                for item in invocations
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _transfer_paper_draft(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> TransferPaperDraft | None:
        row = connection.execute(
            "SELECT * FROM paper_drafts WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return TransferPaperDraft(
            content=row["content"],
            base_hash=row["base_hash"],
            ancestor_content=row["ancestor_content"],
            cursor_state=row["cursor_state"],
            updated_at=row["updated_at"],
        )


__all__ = ["ProjectTransferStoreMixin"]
