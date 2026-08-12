from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

from rcp.storage.models import (  # noqa: F401
    _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE,
    _EXPERIMENT_EPISODE_PINNED_FIELDS,
    _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC,
    _PROJECT_ID_TABLES,
    ACTIVE_AGENT_TASK_STATUSES,
    SPACE_NAME_MAX_LENGTH,
    AgentCommandInvocationRecord,
    AgentTaskContractRecord,
    AgentTaskEventRecord,
    AgentTaskKind,
    AgentTaskReceiptRecord,
    AgentTaskReceiptTier,
    AgentTaskRecord,
    AgentTaskStatus,
    AgentUsageCell,
    AgentUsageCountReason,
    AgentUsageMetric,
    AgentUsageRecord,
    AgentUsageSnapshot,
    CampaignActorBinding,
    CampaignActorBusy,
    CampaignBudgetExhausted,
    CampaignBudgetMeter,
    CampaignEnding,
    CampaignInvocationRole,
    CampaignMessageRecord,
    CampaignMessageRole,
    CampaignNotRunning,
    CampaignRecord,
    CampaignRecoveryMode,
    CampaignRecoveryPurpose,
    CampaignRecoveryRecord,
    CampaignRecoveryStatus,
    CampaignReportRecord,
    CampaignStatus,
    ChatSessionContextRecord,
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    ExperimentWatcherResourceRecord,
    GraphCondition,
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    ProjectRecord,
    ProjectStageRecord,
    ProposalResolvedGraphCondition,
    ProviderSkillInventoryRecord,
    ResultViewConflict,
    ResultViewRecord,
    SpaceKind,
    SpaceUserKind,
    SpaceUserRecord,
    StoredWatcherRecord,
    TeamAuthenticationError,
    TeamInvitationRecord,
    WatcherClaimConflict,
    WatcherContinuation,
    WatcherDeliveryRecord,
    WatcherRecord,
    WatcherStatus,
    WatcherStopRequest,
    _canonical_space_id,
    _canonical_uuid4,
    _discard_failed_team_initialization,
    _experiment_pinned_value,
    _new_enrollment_code,
    _new_member_token,
    _new_session_token,
    _optional_str,
    _parse_enrollment_code,
    _plain_html_name,
    _required_timestamp,
    _result_view_html_bytes,
    _result_view_is_visible,
    _result_view_reference_time,
    _sha256,
    _stored_space_kind,
    _validated_result_view_html,
    normalize_space_name,
    watcher_next_check_at,
)

if TYPE_CHECKING:
    from rcp.watchers import WatcherBinding


class ExperimentStoreMixin:
    """Bounded Experiment episodes and their loop runtime projection."""

    @staticmethod
    def _validate_experiment_task_insert(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        request = record.request
        if request.get("patch_kind") != "experiment_loop":
            return

        recovery_binding_keys = (*_EXPERIMENT_EPISODE_PINNED_FIELDS, "control_invocation")
        node_id = request.get("control_node_id")
        control_revision = request.get("control_revision")
        episode_id = request.get("control_episode_id")
        invocation = request.get("control_invocation")
        ceiling = request.get("control_invocation_ceiling")
        decision_bundle = request.get("control_decision_bundle")
        completion_criteria = request.get("control_completion_criteria")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("A bounded experiment-loop task must name its control node.")
        if not isinstance(control_revision, int) or isinstance(control_revision, bool):
            raise ValueError("A bounded experiment-loop task must pin its control revision.")
        if not isinstance(decision_bundle, list):
            raise ValueError("A bounded experiment-loop task must pin its governing decisions.")
        if not isinstance(completion_criteria, list) or any(
            not isinstance(item, str) for item in completion_criteria
        ):
            raise ValueError("A bounded experiment-loop task must pin its completion criteria.")
        if not isinstance(episode_id, str):
            raise ValueError("A bounded experiment-loop task must name a valid episode id.")
        try:
            uuid.UUID(episode_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "A bounded experiment-loop task must name a valid episode id."
            ) from exc
        if not isinstance(invocation, int) or isinstance(invocation, bool) or invocation < 1:
            raise ValueError("A bounded experiment-loop task must name its invocation number.")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ValueError("A bounded experiment-loop task must pin its invocation ceiling.")
        if invocation > ceiling:
            raise ValueError("The experiment-loop invocation exceeds its pinned ceiling.")

        if record.parent_operation_id:
            parent = connection.execute(
                """
                SELECT project_id, kind, status, attempt, request_json, result_json
                FROM graph_runs WHERE operation_id = ?
                """,
                (record.parent_operation_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("An experiment-loop recovery task must have its parent task.")
            if parent["project_id"] != record.project_id or parent["kind"] != record.kind:
                raise ValueError("An experiment-loop recovery task must preserve its task scope.")
            parent_request = json.loads(parent["request_json"])
            if any(
                _experiment_pinned_value(parent_request, key)
                != _experiment_pinned_value(request, key)
                for key in recovery_binding_keys
            ):
                raise ValueError(
                    "An experiment-loop recovery task must preserve its control binding and "
                    "pinned configuration."
                )
            parent_result = json.loads(parent["result_json"]) if parent["result_json"] else None
            graph_update = (
                parent_result.get("graph_update") if isinstance(parent_result, dict) else None
            )
            patch_only_repair = (
                request.get("message") is None
                and parent["status"] == "succeeded"
                and isinstance(graph_update, dict)
                and graph_update.get("status") == "rejected"
                and graph_update.get("repairable") is False
            )
            if not patch_only_repair:
                ExperimentStoreMixin._validate_experiment_recovery_claim(
                    connection,
                    record,
                    parent,
                    parent_request,
                )
            else:
                ExperimentStoreMixin._validate_current_experiment_graph_repair(
                    connection,
                    project_id=record.project_id,
                    control_node_id=node_id,
                    episode_id=episode_id,
                    invocation=invocation,
                    operation_id=record.parent_operation_id,
                )
            return

        trigger = request.get("trigger")
        if trigger not in {"experiment_run", "watcher"}:
            raise ValueError("A root experiment-loop task must be a Run or watcher invocation.")
        rows = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            """,
            (record.project_id, node_id, episode_id),
        ).fetchall()
        prior = [json.loads(row["request_json"]) for row in rows]
        if any(
            _experiment_pinned_value(item, key) != _experiment_pinned_value(request, key)
            for item in prior
            for key in _EXPERIMENT_EPISODE_PINNED_FIELDS
        ):
            raise ValueError("An experiment-loop episode cannot change its pinned configuration.")
        expected = max((int(item["control_invocation"]) for item in prior), default=0) + 1
        if invocation != expected:
            raise ValueError(
                f"Experiment-loop invocation {invocation} is out of sequence; expected {expected}."
            )
        if invocation == 1 and prior:
            raise ValueError("An experiment-loop episode may have only one first invocation.")
        if trigger == "experiment_run" and invocation != 1:
            raise ValueError("A human Run must start at experiment-loop invocation 1.")
        if trigger == "watcher" and not prior:
            raise ValueError("An automatic watcher wake requires an existing loop episode.")
        if trigger == "watcher":
            ExperimentStoreMixin._validate_experiment_wake_binding(connection, record)

    @staticmethod
    def _validate_experiment_wake_binding(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        """Prove the saved native session before an automatic wake spends budget."""

        request = record.request
        episode_id = request.get("control_episode_id")
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("An automatic Experiment wake requires its episode session id.")
        if record.native_session_id != session_id or not record.stage_root:
            raise ValueError(
                "An automatic Experiment wake requires its exact saved session and stage."
            )
        episode = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode is None or episode["stop_requested_at"] is not None:
            raise ValueError("The automatic Experiment wake has no active episode binding.")
        binding_task = connection.execute(
            "SELECT request_json FROM graph_runs WHERE operation_id = ?",
            (episode["last_turn_operation_id"],),
        ).fetchone()
        if binding_task is None:
            raise ValueError("The automatic Experiment wake has no active binding task.")
        binding_request = json.loads(binding_task["request_json"])
        expected = {
            "project_id": record.project_id,
            "control_node_id": request.get("control_node_id"),
            "provider": request.get("provider"),
            "execution_machine": request.get("run_on"),
            "native_session_id": session_id,
            "stage_host": record.stage_host or "",
            "stage_root": record.stage_root,
            "chat_id": request.get("chat_id"),
            "model": request.get("model"),
            "reasoning": request.get("reasoning"),
        }
        actual = {
            "project_id": episode["project_id"],
            "control_node_id": episode["control_node_id"],
            "provider": episode["provider"],
            "execution_machine": episode["execution_machine"],
            "native_session_id": episode["native_session_id"],
            "stage_host": episode["stage_host"] or "",
            "stage_root": episode["stage_root"],
            "chat_id": episode["chat_id"],
            "model": binding_request.get("model"),
            "reasoning": binding_request.get("reasoning"),
        }
        mismatched = sorted(key for key, value in expected.items() if actual[key] != value)
        if (episode["execution_host"] or "") != (record.stage_host or ""):
            mismatched.append("execution_host")
        if mismatched:
            raise ValueError(
                "The automatic Experiment wake no longer matches its episode binding: "
                + ", ".join(sorted(set(mismatched)))
            )

    @staticmethod
    def _validate_experiment_recovery_claim(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
        parent: sqlite3.Row,
        parent_request: dict[str, object],
    ) -> None:
        abandoned = connection.execute(
            """
            SELECT 1 FROM graph_run_receipts
            WHERE operation_id = ? AND category = 'experiment_recovery_abandoned'
            LIMIT 1
            """,
            (record.parent_operation_id,),
        ).fetchone()
        if abandoned is not None:
            raise ValueError("Stop loop already abandoned recovery of this Experiment task.")
        if parent["status"] not in {"paused", "interrupted", "failed"}:
            raise ValueError("Only the latest unresolved loop task can be resumed or retried.")
        if record.attempt != int(parent["attempt"]) + 1:
            raise ValueError("A loop recovery task must advance its provider-attempt lineage.")
        child = connection.execute(
            "SELECT 1 FROM graph_runs WHERE parent_operation_id = ? LIMIT 1",
            (record.parent_operation_id,),
        ).fetchone()
        if child is not None:
            raise ValueError("This loop task already has a recovery child.")
        newest_root = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (record.project_id, parent_request["control_node_id"]),
        ).fetchone()
        if newest_root is None:
            raise ValueError("The loop episode root is no longer available.")
        newest_request = json.loads(newest_root["request_json"])
        if newest_request.get("control_episode_id") != parent_request.get(
            "control_episode_id"
        ) or newest_request.get("control_invocation") != parent_request.get("control_invocation"):
            raise ValueError("Only the newest loop episode and invocation can be recovered.")
        newer_attempt = connection.execute(
            """
            SELECT 1 FROM graph_runs
            WHERE project_id = ?
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
              AND json_extract(request_json, '$.control_invocation') = ?
              AND attempt > ?
            LIMIT 1
            """,
            (
                record.project_id,
                parent_request["control_node_id"],
                parent_request["control_episode_id"],
                parent_request["control_invocation"],
                parent["attempt"],
            ),
        ).fetchone()
        if newer_attempt is not None:
            raise ValueError("Only the latest unresolved loop task can be recovered.")

    @staticmethod
    def _validate_current_experiment_graph_repair(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        control_node_id: str,
        episode_id: str,
        invocation: int,
        operation_id: str,
    ) -> None:
        """Keep patch-only repair on the newest episode, invocation, and attempt."""

        newest_root = connection.execute(
            """
            SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
            FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if newest_root is None or newest_root["episode_id"] != episode_id:
            raise ValueError("Only the newest Experiment episode can repair its graph update.")
        stopped = connection.execute(
            "SELECT stop_requested_at FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if stopped is not None and stopped["stop_requested_at"] is not None:
            raise ValueError("A stopped Experiment episode cannot repair an old graph update.")
        latest = connection.execute(
            """
            SELECT operation_id,
                   json_extract(request_json, '$.control_invocation') AS invocation
            FROM graph_runs
            WHERE project_id = ?
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            ORDER BY CAST(json_extract(request_json, '$.control_invocation') AS INTEGER) DESC,
                     attempt DESC, created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id, episode_id),
        ).fetchone()
        if (
            latest is None
            or latest["invocation"] != invocation
            or latest["operation_id"] != operation_id
        ):
            raise ValueError(
                "Only the newest Experiment invocation and task attempt can repair its graph "
                "update."
            )

    def persist_experiment_watchers_idempotently(
        self,
        records: list[StoredWatcherRecord],
        *,
        stops: list[WatcherStopRequest] | None = None,
        binding: WatcherBinding | None = None,
        expected_watcher_snapshot_token: str | None = None,
    ) -> list[StoredWatcherRecord]:
        """Persist one loop handoff atomically with the episode's graceful stop.

        Deterministic watcher ids make Retry and crash recovery safe. The same
        ``BEGIN IMMEDIATE`` boundary used by Stop loop ensures either the handoff
        lands first and Stop terminalizes it, or the handoff sees stop intent and
        is born stopped. No pollable row can be created after a persisted stop.
        """

        stop_requests = list(stops or [])
        if not records and not stop_requests:
            return []
        records = [self._prepare_watcher_for_insert(record) for record in records]
        if records:
            self._validate_watch_list(records)
        if binding is None:
            raise ValueError("an Experiment handoff requires its bound watcher context")
        continuation = records[0].continuation if records else binding.continuation
        if continuation.patch_kind != "experiment_loop":
            raise ValueError("idempotent Experiment persistence requires loop watchers")
        episode_id = continuation.control_episode_id
        assert episode_id is not None
        if (
            binding is not None
            and records
            and any(
                (
                    record.project_id != binding.project_id
                    or record.origin_operation_id != binding.origin_operation_id
                    or record.origin_task_kind != binding.origin_task_kind
                    or record.chat_id != binding.chat_id
                    or record.node_id != binding.node_id
                    or record.execution_host != binding.execution_host
                    or record.continuation != binding.continuation
                )
                for record in records
            )
        ):
            raise ValueError("Experiment watcher handoff changed its bound continuation context.")
        stop_ids = [item.stop_watcher_id for item in stop_requests]
        if len(stop_ids) != len(set(stop_ids)):
            raise ValueError("Experiment watcher stop ids must be unique")
        watcher_ids = [record.watcher_id for record in records]
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resource = self._admit_experiment_watcher_maintenance(connection, binding)
            if resource is not None:
                if expected_watcher_snapshot_token is None:
                    raise ValueError(
                        "Experiment watcher maintenance requires its staged watcher snapshot."
                    )
                if expected_watcher_snapshot_token != resource.watcher_snapshot_token:
                    raise WatcherClaimConflict(
                        "Experiment watcher state changed after it was staged; inspect the "
                        "current resource before maintaining it."
                    )
            episode = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if episode is not None and (
                episode["project_id"] != (records[0].project_id if records else binding.project_id)
                or episode["control_node_id"] != continuation.control_node_id
            ):
                raise ValueError("This watcher handoff belongs to a different Experiment episode.")
            if stop_requests:
                assert binding is not None
                self._validate_and_apply_agent_watcher_stops(
                    connection,
                    binding,
                    stop_requests,
                    episode,
                )
            stopped = episode is not None and episode["stop_requested_at"] is not None
            existing_rows = []
            if watcher_ids:
                placeholders = ",".join("?" for _ in watcher_ids)
                existing_rows = connection.execute(
                    f"SELECT * FROM watchers WHERE watcher_id IN ({placeholders})",
                    watcher_ids,
                ).fetchall()
            existing_by_id = {
                str(row["watcher_id"]): self._watcher_record(row) for row in existing_rows
            }
            for desired in records:
                existing = existing_by_id.get(desired.watcher_id)
                if existing is not None:
                    self._validate_idempotent_watcher(existing, desired)
                    if stopped and (existing.status != "stopped" or not existing.notified):
                        self._stop_watcher_for_loop(connection, desired.watcher_id)
                    continue
                persisted = (
                    desired.model_copy(
                        update={
                            "status": "stopped",
                            "notified": True,
                            "next_check_at": None,
                            "stopped_by": "loop",
                            "stopped_at": self.now(),
                        }
                    )
                    if stopped
                    else desired
                )
                self._insert_watcher(connection, persisted)
            stored_rows = []
            if watcher_ids:
                placeholders = ",".join("?" for _ in watcher_ids)
                stored_rows = connection.execute(
                    f"SELECT * FROM watchers WHERE watcher_id IN ({placeholders})",
                    watcher_ids,
                ).fetchall()
            stored_by_id = {
                str(row["watcher_id"]): self._watcher_record(row) for row in stored_rows
            }
        return [stored_by_id[watcher_id] for watcher_id in watcher_ids]

    def validate_experiment_agent_watcher_stops(
        self,
        binding: WatcherBinding,
        stops: list[WatcherStopRequest],
    ) -> None:
        """Fail a malformed stop handoff before its Patch can be accepted."""

        if not stops:
            return
        episode_id = binding.continuation.control_episode_id
        with self.connection() as connection:
            self._admit_experiment_watcher_maintenance(connection, binding)
            episode = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            self._validate_and_apply_agent_watcher_stops(
                connection,
                binding,
                stops,
                episode,
                apply=False,
            )

    def experiment_watcher_resources(
        self,
        project_id: str,
        *,
        control_node_ids: set[str] | None = None,
    ) -> list[ExperimentWatcherResourceRecord]:
        """Return live Experiment resources visible within one already-resolved scope."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT json_extract(request_json, '$.control_node_id') AS control_node_id
                FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                """,
                (project_id,),
            ).fetchall()
            resources: list[ExperimentWatcherResourceRecord] = []
            for row in rows:
                control_node_id = row["control_node_id"]
                if not isinstance(control_node_id, str) or not control_node_id:
                    continue
                if control_node_ids is not None and control_node_id not in control_node_ids:
                    continue
                try:
                    resource = self._current_experiment_watcher_resource(
                        connection,
                        project_id,
                        control_node_id,
                    )
                except ValueError:
                    continue
                resources.append(resource)
        return sorted(resources, key=lambda item: item.control_node_id)

    def admit_experiment_watcher_maintenance(
        self,
        binding: WatcherBinding,
    ) -> ExperimentWatcherResourceRecord | None:
        """Authorize one node-attached watcher handoff from its durable Work task.

        A loop turn returns ``None`` before its first episode binding exists. A
        conversation maintenance turn always returns the current resource and
        fails closed when durable node, episode, or session identity is absent.
        """

        with self.connection() as connection:
            return self._admit_experiment_watcher_maintenance(connection, binding)

    def _admit_experiment_watcher_maintenance(
        self,
        connection: sqlite3.Connection,
        binding: WatcherBinding,
    ) -> ExperimentWatcherResourceRecord | None:
        task_row = connection.execute(
            "SELECT project_id, kind, request_json FROM graph_runs WHERE operation_id = ?",
            (binding.origin_operation_id,),
        ).fetchone()
        if task_row is None:
            raise ValueError("Experiment watcher maintenance permission denied: actor is missing.")
        request = json.loads(task_row["request_json"])
        if task_row["project_id"] != binding.project_id:
            raise ValueError(
                "Experiment watcher maintenance permission denied: project scope does not match."
            )
        if request.get("mode") != "work" or task_row["kind"] not in {
            "node_chat",
            "project_chat",
        }:
            raise ValueError(
                "Experiment watcher maintenance permission denied: Work capability is required."
            )
        if (
            request.get("chat_id") != binding.chat_id
            or task_row["kind"] != binding.origin_task_kind
        ):
            raise ValueError(
                "Experiment watcher maintenance permission denied: actor provenance does not match."
            )

        continuation = binding.continuation
        control_node_id = continuation.control_node_id
        episode_id = continuation.control_episode_id
        if continuation.patch_kind != "experiment_loop" or not control_node_id or not episode_id:
            raise ValueError(
                "Experiment watcher maintenance requires an explicit node and episode resource."
            )
        if binding.node_id != control_node_id:
            raise ValueError(
                "Experiment watcher maintenance permission denied: target node does not match."
            )

        actor_patch_kind = request.get("patch_kind")
        if actor_patch_kind == "experiment_loop":
            if (
                request.get("control_node_id") != control_node_id
                or request.get("control_episode_id") != episode_id
            ):
                raise ValueError(
                    "Experiment watcher maintenance permission denied: loop binding does not match."
                )
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if episode_row is not None and (
                episode_row["project_id"] != binding.project_id
                or episode_row["control_node_id"] != control_node_id
            ):
                raise ValueError("Experiment watcher maintenance targets a different episode.")
            return None

        if actor_patch_kind != "work":
            raise ValueError(
                "Experiment watcher maintenance permission denied: captured Patch policy is invalid."
            )
        if task_row["kind"] == "node_chat" and request.get("node_id") != control_node_id:
            raise ValueError(
                "Experiment watcher maintenance permission denied: node scope does not include "
                f"{control_node_id}."
            )
        resource = self._current_experiment_watcher_resource(
            connection,
            binding.project_id,
            control_node_id,
            expected_episode_id=episode_id,
        )
        if binding.execution_host != resource.execution_host:
            raise ValueError("Experiment watcher maintenance must use the episode execution host.")
        if continuation != resource.continuation:
            raise ValueError(
                "Experiment watcher maintenance no longer matches the live episode policy."
            )
        return resource

    def _current_experiment_watcher_resource(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        *,
        expected_episode_id: str | None = None,
    ) -> ExperimentWatcherResourceRecord:
        root_row = connection.execute(
            """
            SELECT kind, request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if root_row is None:
            raise ValueError("Experiment watcher maintenance requires a current live episode.")
        root_request = json.loads(root_row["request_json"])
        episode_id = root_request.get("control_episode_id")
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the current episode identity."
            )
        if expected_episode_id is not None and expected_episode_id != episode_id:
            raise ValueError("Experiment watcher maintenance targets a stale episode.")
        episode_row = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode_row is None:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode session binding."
            )
        episode = self._experiment_episode_record(episode_row)
        if (
            episode.project_id != project_id
            or episode.control_node_id != control_node_id
            or not episode.session_bound
        ):
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode session binding."
            )
        if episode.stop_requested_at is not None or episode.stop_settled_at is not None:
            raise ValueError("Experiment watcher maintenance requires a live, unstopped episode.")
        exited = connection.execute(
            """
            SELECT 1 FROM graph_run_receipts AS receipt
            JOIN graph_runs AS run ON run.operation_id = receipt.operation_id
            WHERE run.project_id = ?
              AND json_extract(run.request_json, '$.control_episode_id') = ?
              AND receipt.category = 'experiment_loop_exit'
            LIMIT 1
            """,
            (project_id, episode_id),
        ).fetchone()
        if exited is not None:
            raise ValueError("Experiment watcher maintenance requires a live, unexited episode.")
        if not episode.last_turn_operation_id:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode's latest turn."
            )
        turn_row = connection.execute(
            "SELECT request_json FROM graph_runs WHERE operation_id = ? AND project_id = ?",
            (episode.last_turn_operation_id, project_id),
        ).fetchone()
        if turn_row is None:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode's latest turn."
            )
        turn_request = json.loads(turn_row["request_json"])
        continuation_data = {
            key: turn_request[key]
            for key in WatcherContinuation.model_fields
            if key in turn_request
        }
        for nullable_list in ("workflow_ids", "skill_ids", "resolved_skill_packages"):
            if continuation_data.get(nullable_list) is None:
                continuation_data[nullable_list] = []
        continuation = WatcherContinuation.model_validate(continuation_data)
        if (
            continuation.patch_kind != "experiment_loop"
            or continuation.control_node_id != control_node_id
            or continuation.control_episode_id != episode_id
        ):
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode continuation policy."
            )
        wake_task_kind = root_row["kind"]
        if wake_task_kind != "node_chat":
            raise ValueError("Experiment watcher maintenance has an invalid wake task binding.")
        if not episode.chat_id:
            # The wake target is derived, never guessed: without the episode's own
            # conversation there is nothing to wake, so fail closed with a diagnostic
            # rather than an AssertionError that -O would strip.
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode's wake conversation."
            )
        return ExperimentWatcherResourceRecord(
            project_id=project_id,
            control_node_id=control_node_id,
            episode_id=episode_id,
            execution_host=episode.execution_host,
            wake_task_kind=wake_task_kind,
            wake_chat_id=episode.chat_id,
            continuation=continuation,
            watcher_snapshot_token=self._experiment_watcher_snapshot_token(
                connection,
                project_id,
                control_node_id,
            ),
        )

    @staticmethod
    def _experiment_watcher_snapshot_token(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
    ) -> str:
        """Fingerprint the node's observer membership, and nothing else.

        This defends exactly one gap. Every retirement is already a
        compare-and-swap inside the arming transaction, so a delivery claim, a
        **Stop loop**, or an already-resolved stop is caught per item without a
        fingerprint. Arming is not: new observers are plain inserts, so two
        maintenance turns could each retire the old set and each arm
        replacements, leaving the Experiment double-observed.

        Membership answers that and stays blind to everything RCP merely
        observed. Status and consecutive-error counts deliberately do not appear:
        a degraded observer is re-checked on the S84 backoff, so fingerprinting
        observation would reject the maintenance turn that exists to repair that
        very observer. Retired rows keep their id, so the set only grows and a
        concurrent retirement does not collide with an unrelated repair.
        """

        rows = connection.execute(
            """
            SELECT watcher_id FROM watchers
            WHERE project_id = ? AND node_id = ?
              AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
            ORDER BY watcher_id
            """,
            (project_id, control_node_id),
        ).fetchall()
        snapshot = json.dumps(
            [str(row["watcher_id"]) for row in rows],
            separators=(",", ":"),
        )
        return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    def experiment_watcher_ids(self, project_id: str, control_node_id: str) -> list[str]:
        """Live watchers armed by a bounded loop on one experiment."""

        return [
            record.watcher_id
            for record in self.watchers(project_id)
            if (
                (record.status in {"active", "degraded"} and not record.notified)
                or (record.status == "completed" and not record.notified)
            )
            and record.continuation.control_node_id == control_node_id
        ]

    def experiment_handoff_has_live_watcher_after_stops(
        self,
        binding: WatcherBinding,
        stop_watcher_ids: list[str],
    ) -> bool:
        """Whether a stop-only handoff leaves another compatible wake source."""

        continuation = binding.continuation
        episode_id = continuation.control_episode_id
        control_node_id = continuation.control_node_id
        if not episode_id or not control_node_id:
            return False
        stopped = set(stop_watcher_ids)
        with self.connection() as connection:
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if episode_row is None:
                return False
            episode = self._experiment_episode_record(episode_row)
            root = self._experiment_episode_root_request(
                connection,
                binding.project_id,
                control_node_id,
                episode_id,
            )
            if root is None:
                return False
            rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE project_id = ? AND status IN ('active', 'degraded', 'completed')
                  AND notified = 0
                """,
                (binding.project_id,),
            ).fetchall()
        return any(
            record.watcher_id not in stopped
            and self._experiment_watcher_matches_current(record, root, episode)
            for record in (self._watcher_record(row) for row in rows)
        )

    def experiment_episode(self, episode_id: str) -> ExperimentEpisodeRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return self._experiment_episode_record(row) if row is not None else None

    def experiment_episode_recovery_context_problem(self, operation_id: str) -> str | None:
        """Explain why this task lineage cannot retain its episode context on recovery."""

        with self.connection() as connection:
            return self._experiment_episode_recovery_context_problem(connection, operation_id)

    @staticmethod
    def _experiment_episode_recovery_context_problem(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> str | None:
        """Validate the immutable candidate on an Experiment invocation's lineage root."""

        current_id = operation_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                return (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage contains a cycle. Use Stop loop and press Run to start a fresh "
                    "episode."
                )
            seen.add(current_id)
            row = connection.execute(
                "SELECT parent_operation_id FROM graph_runs WHERE operation_id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                return (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage is incomplete. Use Stop loop and press Run to start a fresh episode."
                )
            parent_id = row["parent_operation_id"]
            if parent_id is None:
                break
            current_id = str(parent_id)

        contract = connection.execute(
            """
            SELECT content FROM graph_run_contracts
            WHERE operation_id = ? AND role = ?
            """,
            (current_id, _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE),
        ).fetchone()
        if contract is None:
            return _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC
        try:
            candidate = json.loads(contract["content"])
        except (json.JSONDecodeError, TypeError):
            candidate = None
        if not isinstance(candidate, dict):
            return (
                "This Experiment-loop turn cannot be resumed or retried because its retained "
                "episode context candidate is invalid. Use Stop loop and press Run to start a "
                "fresh episode."
            )
        return None

    def previous_experiment_episode(
        self,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Return the episode immediately before this one for the same Experiment.

        Ordering comes from the root invocations, not the episode table, because
        an episode only gets a row once it binds a session or receives a stop.
        """

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
                FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(request_json, '$.control_node_id') = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (project_id, control_node_id),
            ).fetchall()
        ordered: list[str] = []
        for row in rows:
            value = row["episode_id"]
            if isinstance(value, str) and value not in ordered:
                ordered.append(value)
        if episode_id not in ordered:
            return None
        position = ordered.index(episode_id) + 1
        if position >= len(ordered):
            return None
        return self.experiment_episode(ordered[position])

    def commit_experiment_episode_turn(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        provider: str,
        execution_machine: str,
        execution_host: str,
        native_session_id: str,
        stage_host: str | None,
        stage_root: str,
        chat_id: str,
        operation_id: str,
        invocation: int,
        graph_result: str,
        watcher_ids: list[str],
        context_baseline: dict[str, object],
        replace_binding: bool = False,
        replacement_provenance: dict[str, object] | None = None,
    ) -> ExperimentEpisodeRecord:
        """Bind this episode to the session a later automatic wake resumes.

        Only a mechanically successful joint handoff commits, so a wake never
        tries to continue a session that never established one, and the context
        baseline can only move forward with an accepted operational turn. A
        graph-only rejection is retained as that turn's truthful result.
        """

        if not native_session_id or not stage_root:
            raise ValueError("An episode binding requires a native session and its exact stage.")
        if replace_binding and replacement_provenance is None:
            raise ValueError("An episode binding replacement requires its recovery provenance.")
        replacement_payload_json = (
            self._bounded_receipt_payload(replacement_provenance)
            if replacement_provenance is not None
            else None
        )
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            existing = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if (
                existing is None
                or existing["project_id"] != project_id
                or existing["control_node_id"] != control_node_id
            ):
                raise ValueError("This episode id belongs to a different Experiment.")
            if existing["native_session_id"] is not None:
                fixed = {
                    "execution_machine": execution_machine,
                    "execution_host": execution_host,
                    "chat_id": chat_id,
                }
                fixed_conflicts = sorted(
                    field for field, value in fixed.items() if (existing[field] or "") != value
                )
                if fixed_conflicts:
                    raise ValueError(
                        "An Experiment episode recovery cannot change its pinned identity: "
                        + ", ".join(fixed_conflicts)
                    )
                binding = {
                    "provider": provider,
                    "native_session_id": native_session_id,
                    "stage_host": stage_host or "",
                    "stage_root": stage_root,
                }
                binding_conflicts = sorted(
                    field for field, value in binding.items() if (existing[field] or "") != value
                )
                if binding_conflicts and not replace_binding:
                    raise ValueError(
                        "An Experiment episode cannot change its native-session binding: "
                        + ", ".join(binding_conflicts)
                    )
            connection.execute(
                """
                UPDATE experiment_episodes
                SET provider = ?, execution_machine = ?, execution_host = ?,
                    native_session_id = ?, stage_host = ?, stage_root = ?, chat_id = ?,
                    last_turn_operation_id = ?, last_turn_invocation = ?,
                    last_graph_result = ?, last_watcher_ids_json = ?,
                    context_baseline_json = ?, session_diagnostic = NULL, updated_at = ?
                WHERE episode_id = ?
                """,
                (
                    provider,
                    execution_machine,
                    execution_host,
                    native_session_id,
                    stage_host,
                    stage_root,
                    chat_id,
                    operation_id,
                    invocation,
                    graph_result,
                    json.dumps(list(watcher_ids), separators=(",", ":")),
                    json.dumps(context_baseline, sort_keys=True, separators=(",", ":")),
                    now,
                    episode_id,
                ),
            )
            if replace_binding and replacement_payload_json is not None:
                self._insert_agent_task_receipt(
                    connection,
                    operation_id,
                    "experiment_episode_binding_replaced",
                    replacement_payload_json,
                    tier="summary",
                    created_at=now,
                )
        stored = self.experiment_episode(episode_id)
        assert stored is not None
        return stored

    def record_experiment_episode_diagnostic(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        diagnostic: str | None,
    ) -> None:
        """Persist why an automatic wake could not use this episode's session.

        The row is created on demand: the episode whose very first turn never
        bound a session is exactly the one that most needs a diagnostic, and it
        has nothing else to write a row for it.
        """

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            connection.execute(
                "UPDATE experiment_episodes SET session_diagnostic = ?, updated_at = ? "
                "WHERE episode_id = ? AND project_id = ? AND control_node_id = ?",
                (diagnostic, now, episode_id, project_id, control_node_id),
            )

    def request_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Persist a durable stop for the newest episode before any new claim can win.

        The intent is written under the same write lock a watcher claim takes, so
        a claim that committed first becomes the current turn and anything later
        finds the loop already stopped.
        """

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode_id = self._newest_experiment_episode_id(connection, project_id, control_node_id)
            if episode_id is None:
                return None
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            connection.execute(
                """
                UPDATE experiment_episodes
                SET stop_requested_at = COALESCE(stop_requested_at, ?), updated_at = ?
                WHERE episode_id = ?
                """,
                (now, now, episode_id),
            )
            self._settle_experiment_loop_stop(connection, project_id, control_node_id, episode_id)
        return self.experiment_episode(episode_id)

    def settle_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Reconcile a persisted stop once its authorized turn is no longer live."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode_id = self._newest_experiment_episode_id(connection, project_id, control_node_id)
            if episode_id is None:
                return None
            self._settle_experiment_loop_stop(connection, project_id, control_node_id, episode_id)
        return self.experiment_episode(episode_id)

    def _settle_experiment_loop_stop(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> bool:
        """Terminalize this episode's observers once its authorized turn is resolved.

        "Resolved" is the same predicate the runtime calls `task_active`, not just
        "not running": a turn that paused or failed is still the authorized turn
        the human may Resume, so the loop keeps reading Stopping until it reaches
        a terminal state. A claimed watcher keeps its notification provenance,
        but becomes stopped once the task it woke has finished successfully.
        """

        requested = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if requested is None or requested["stop_requested_at"] is None:
            return False
        # A superseded attempt does not count: only the newest attempt of each
        # invocation is the turn the human can still act on, which is exactly what
        # `experiment_loop_runtime` reports as `task_active`.
        unresolved = connection.execute(
            """
            SELECT task.operation_id, task.status FROM graph_runs AS task
            WHERE task.project_id = ?
              AND json_extract(task.request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(task.request_json, '$.control_node_id') = ?
              AND json_extract(task.request_json, '$.control_episode_id') = ?
              AND task.status IN ('queued', 'running', 'pausing', 'paused', 'failed', 'interrupted')
              AND NOT EXISTS (
                  SELECT 1 FROM graph_runs AS child
                  WHERE child.parent_operation_id = task.operation_id
              )
            """,
            (project_id, control_node_id, episode_id),
        ).fetchall()
        if unresolved:
            diagnostic = requested["session_diagnostic"]
            if not diagnostic:
                diagnostic = next(
                    (
                        problem
                        for row in unresolved
                        if (
                            problem := self._experiment_episode_recovery_context_problem(
                                connection,
                                str(row["operation_id"]),
                            )
                        )
                    ),
                    None,
                )
                if diagnostic:
                    now = self.now()
                    connection.execute(
                        "UPDATE experiment_episodes SET session_diagnostic = ?, updated_at = ? "
                        "WHERE episode_id = ?",
                        (diagnostic, now, episode_id),
                    )
            abandonable = bool(diagnostic) and all(
                row["status"] in {"paused", "failed", "interrupted"} for row in unresolved
            )
            if not abandonable:
                return False
            now = self.now()
            for row in unresolved:
                already_abandoned = connection.execute(
                    """
                    SELECT 1 FROM graph_run_receipts
                    WHERE operation_id = ? AND category = 'experiment_recovery_abandoned'
                    LIMIT 1
                    """,
                    (row["operation_id"],),
                ).fetchone()
                if already_abandoned is not None:
                    continue
                detail = (
                    "Stop loop abandoned recovery of this terminal task because its saved "
                    "episode session cannot be continued. The task and all history remain "
                    "inspectable."
                )
                self._insert_agent_task_receipt(
                    connection,
                    str(row["operation_id"]),
                    "experiment_recovery_abandoned",
                    self._bounded_receipt_payload({"episode_id": episode_id, "reason": diagnostic}),
                    tier="summary",
                    created_at=now,
                )
                self._insert_agent_task_event(
                    connection,
                    str(row["operation_id"]),
                    detail,
                    level="warning",
                    created_at=now,
                )
        root_request = self._experiment_episode_root_request(
            connection,
            project_id,
            control_node_id,
            episode_id,
        )
        episode = self._experiment_episode_record(requested)
        watcher_rows = connection.execute(
            """
            SELECT * FROM watchers
            WHERE project_id = ?
              AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(continuation_json, '$.control_node_id') = ?
              AND status IN ('active', 'degraded', 'completed')
            """,
            (project_id, control_node_id),
        ).fetchall()
        watcher_ids = {
            record.watcher_id
            for record in (self._watcher_record(row) for row in watcher_rows)
            if root_request is not None
            and self._experiment_watcher_matches_current(record, root_request, episode)
        }
        claimed_rows = connection.execute(
            """
            SELECT watcher_id FROM watchers
            WHERE project_id = ?
              AND notification_operation_id IN (
                  SELECT operation_id FROM graph_runs
                  WHERE project_id = ?
                    AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                    AND json_extract(request_json, '$.control_node_id') = ?
                    AND json_extract(request_json, '$.control_episode_id') = ?
              )
            """,
            (project_id, project_id, control_node_id, episode_id),
        ).fetchall()
        watcher_ids.update(str(row["watcher_id"]) for row in claimed_rows)
        if watcher_ids:
            placeholders = ",".join("?" for _ in watcher_ids)
            connection.execute(
                f"UPDATE watchers SET status = 'stopped', notified = 1, next_check_at = NULL, "
                "stopped_by = COALESCE(stopped_by, 'loop'), "
                "stopped_at = COALESCE(stopped_at, ?) "
                f"WHERE watcher_id IN ({placeholders})",
                (self.now(), *sorted(watcher_ids)),
            )
        if requested["stop_settled_at"] is None:
            now = self.now()
            connection.execute(
                "UPDATE experiment_episodes SET stop_settled_at = ?, updated_at = ? "
                "WHERE episode_id = ?",
                (now, now, episode_id),
            )
        return True

    def settle_ready_experiment_loop_stops(self) -> int:
        """Reconcile every durable stop that no longer has a recoverable turn."""

        settled = 0
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT episode_id, project_id, control_node_id
                FROM experiment_episodes
                WHERE stop_requested_at IS NOT NULL AND stop_settled_at IS NULL
                ORDER BY created_at, episode_id
                """
            ).fetchall()
            for row in rows:
                if self._settle_experiment_loop_stop(
                    connection,
                    str(row["project_id"]),
                    str(row["control_node_id"]),
                    str(row["episode_id"]),
                ):
                    settled += 1
        return settled

    @staticmethod
    def _newest_experiment_episode_id(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
            FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if row is None or not isinstance(row["episode_id"], str):
            return None
        return row["episode_id"]

    def experiment_loop_runtime(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentLoopRuntime:
        """Derive the newest episode from root invocations and its watcher ledger."""

        return self.experiment_loop_runtimes(project_id, [control_node_id])[control_node_id]

    def experiment_loop_runtimes(
        self,
        project_id: str,
        control_node_ids: Iterable[str],
    ) -> dict[str, ExperimentLoopRuntime]:
        """Derive several Experiment runtimes from one project-scoped projection."""

        requested = tuple(dict.fromkeys(control_node_ids))
        if not requested:
            return {}
        projected = self._project_experiment_loop_runtimes(project_id, set(requested))
        return {
            control_node_id: projected.get(control_node_id, ExperimentLoopRuntime())
            for control_node_id in requested
        }

    def _project_experiment_loop_runtimes(
        self,
        project_id: str,
        requested: set[str] | None,
    ) -> dict[str, ExperimentLoopRuntime]:
        """Load loop ledgers in four bounded reads and group them in memory."""

        with self.connection() as connection:
            task_rows = connection.execute(
                """
                SELECT operation_id, parent_operation_id, status, attempt, request_json,
                       created_at, phase, status_message, last_activity_at,
                       rowid AS storage_rowid
                FROM graph_runs
                WHERE project_id = ?
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                """,
                (project_id,),
            ).fetchall()
            receipt_rows = connection.execute(
                """
                SELECT receipt.operation_id, receipt.category
                FROM graph_run_receipts AS receipt
                JOIN graph_runs AS task ON task.operation_id = receipt.operation_id
                WHERE task.project_id = ?
                  AND json_extract(task.request_json, '$.patch_kind') = 'experiment_loop'
                  AND receipt.category IN (
                      'experiment_loop_exit', 'experiment_recovery_abandoned'
                  )
                """,
                (project_id,),
            ).fetchall()
            watcher_rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE project_id = ?
                  AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
                  AND notified = 0
                  AND status IN ('active', 'degraded', 'completed')
                """,
                (project_id,),
            ).fetchall()
            episode_rows = connection.execute(
                """
                SELECT * FROM experiment_episodes
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchall()

        tasks_by_control: dict[
            str,
            list[tuple[sqlite3.Row, dict[str, object]]],
        ] = {}
        for row in task_rows:
            request = json.loads(row["request_json"])
            control_node_id = request.get("control_node_id")
            if not isinstance(control_node_id, str) or not control_node_id:
                continue
            if requested is not None and control_node_id not in requested:
                continue
            tasks_by_control.setdefault(control_node_id, []).append((row, request))

        watchers_by_control: dict[str, list[StoredWatcherRecord]] = {}
        for row in watcher_rows:
            record = self._watcher_record(row)
            control_node_id = record.continuation.control_node_id
            if not control_node_id:
                continue
            if requested is not None and control_node_id not in requested:
                continue
            watchers_by_control.setdefault(control_node_id, []).append(record)

        receipt_categories: dict[str, set[str]] = {}
        for row in receipt_rows:
            receipt_categories.setdefault(str(row["operation_id"]), set()).add(str(row["category"]))
        episodes = {
            str(row["episode_id"]): self._experiment_episode_record(row) for row in episode_rows
        }
        control_node_ids = (
            set(tasks_by_control) | set(watchers_by_control) if requested is None else requested
        )
        return {
            control_node_id: self._derive_experiment_loop_runtime(
                tasks_by_control.get(control_node_id, []),
                watchers_by_control.get(control_node_id, []),
                receipt_categories,
                episodes,
            )
            for control_node_id in control_node_ids
        }

    @classmethod
    def _derive_experiment_loop_runtime(
        cls,
        task_entries: list[tuple[sqlite3.Row, dict[str, object]]],
        watchers: list[StoredWatcherRecord],
        receipt_categories: dict[str, set[str]],
        episodes: dict[str, ExperimentEpisodeRecord],
    ) -> ExperimentLoopRuntime:
        """Purely derive one runtime from an already-loaded project ledger."""

        root_entries = [entry for entry in task_entries if entry[0]["parent_operation_id"] is None]
        if not root_entries:
            return ExperimentLoopRuntime()
        _, root_request = max(
            root_entries,
            key=lambda entry: (entry[0]["created_at"], entry[0]["storage_rowid"]),
        )
        episode_id = root_request.get("control_episode_id")
        if not isinstance(episode_id, str):
            raise ValueError("Stored experiment-loop root is missing its episode id.")
        try:
            uuid.UUID(episode_id)
        except ValueError as exc:
            raise ValueError("Stored experiment-loop root has an invalid episode id.") from exc

        episode_entries = [
            entry for entry in task_entries if entry[1].get("control_episode_id") == episode_id
        ]
        episode_entries.sort(
            key=lambda entry: (
                entry[0]["attempt"],
                entry[0]["created_at"],
                entry[0]["storage_rowid"],
            ),
            reverse=True,
        )
        episode = episodes.get(episode_id)
        compatible_watchers = [
            record
            for record in watchers
            if cls._experiment_watcher_matches_current(record, root_request, episode)
        ]
        latest_by_invocation: dict[
            int,
            tuple[sqlite3.Row, dict[str, object]],
        ] = {}
        for row, request in episode_entries:
            invocation = request.get("control_invocation")
            if isinstance(invocation, int) and invocation not in latest_by_invocation:
                latest_by_invocation[invocation] = (row, request)
        ceiling = root_request.get("control_invocation_ceiling")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ValueError("Stored experiment-loop root is missing its pinned ceiling.")
        if not latest_by_invocation or min(latest_by_invocation) < 1:
            raise ValueError("Stored experiment-loop root is missing its invocation number.")
        invocations_used = max(latest_by_invocation)
        if set(latest_by_invocation) != set(range(1, invocations_used + 1)):
            raise ValueError("Stored experiment-loop root invocations are out of sequence.")
        if invocations_used > ceiling:
            raise ValueError("Stored experiment-loop root exceeds its pinned ceiling.")
        unresolved = any(
            row["status"] in {"queued", "running", "pausing", "paused", "failed", "interrupted"}
            and "experiment_recovery_abandoned"
            not in receipt_categories.get(str(row["operation_id"]), set())
            for row, _request in latest_by_invocation.values()
        )
        detached_work_active = any(
            record.status in {"active", "degraded"} and not record.notified
            for record in compatible_watchers
        )
        watcher_degraded = any(
            record.status == "degraded" and not record.notified for record in compatible_watchers
        )
        watcher_completion_pending = any(
            record.status == "completed" and not record.notified for record in compatible_watchers
        )
        has_watcher = detached_work_active or watcher_completion_pending
        episode_exited = any(
            "experiment_loop_exit" in receipt_categories.get(str(row["operation_id"]), set())
            for row, _request in episode_entries
        )
        at_ceiling = invocations_used >= ceiling
        pins = root_request.get("control_decision_bundle")
        if not isinstance(pins, list):
            raise ValueError("Stored experiment-loop root is missing its pinned decision bundle.")
        control_revision = root_request.get("control_revision")
        if not isinstance(control_revision, int) or isinstance(control_revision, bool):
            raise ValueError("Stored experiment-loop root is missing its control revision.")
        completion_criteria = root_request.get("control_completion_criteria")
        if not isinstance(completion_criteria, list) or any(
            not isinstance(item, str) for item in completion_criteria
        ):
            raise ValueError("Stored experiment-loop root is missing its completion criteria.")
        current_row, current_request = max(
            episode_entries,
            key=lambda entry: (entry[0]["created_at"], entry[0]["storage_rowid"]),
        )
        binding_request = next(
            (
                request
                for row, request in episode_entries
                if episode is not None and row["operation_id"] == episode.last_turn_operation_id
            ),
            root_request,
        )
        current_invocation = current_request.get("control_invocation")
        return ExperimentLoopRuntime(
            episode_id=episode_id,
            invocations_used=invocations_used,
            invocation_ceiling=ceiling,
            control_revision=control_revision,
            task_active=unresolved,
            detached_work_active=detached_work_active,
            watcher_degraded=watcher_degraded,
            watcher_completion_pending=watcher_completion_pending,
            episode_exited=episode_exited,
            active=unresolved
            or (
                has_watcher
                and not at_ceiling
                and not episode_exited
                and not (episode is not None and episode.stop_requested_at is not None)
            ),
            paused=has_watcher
            and at_ceiling
            and not unresolved
            and not episode_exited
            and not (episode is not None and episode.stop_requested_at is not None),
            decision_bundle=pins,
            completion_criteria=completion_criteria,
            stop_requested=episode is not None and episode.stop_requested_at is not None,
            stop_settled=episode is not None and episode.stop_settled_at is not None,
            session_bound=episode is not None and episode.session_bound,
            session_diagnostic=episode.session_diagnostic if episode else None,
            provider=(episode.provider if episode is not None else None)
            or _optional_str(binding_request.get("provider")),
            model=(
                binding_request["model"] if isinstance(binding_request.get("model"), str) else None
            ),
            reasoning=_optional_str(binding_request.get("reasoning")),
            run_on=(episode.execution_machine if episode is not None else None)
            or _optional_str(binding_request.get("run_on")),
            execution_host=episode.execution_host if episode else None,
            run_truth_scope=(
                [str(item) for item in root_request["run_truth_scope"]]
                if isinstance(root_request.get("run_truth_scope"), list)
                else None
            ),
            chat_id=_optional_str(root_request.get("chat_id")),
            current_operation_id=current_row["operation_id"],
            current_status=current_row["status"],
            current_phase=current_row["phase"],
            current_status_message=current_row["status_message"],
            current_last_activity_at=current_row["last_activity_at"],
            current_invocation=(
                current_invocation if isinstance(current_invocation, int) else None
            ),
        )

    @staticmethod
    def _experiment_watcher_matches_current(
        record: StoredWatcherRecord,
        root_request: dict[str, object],
        episode: ExperimentEpisodeRecord | None,
    ) -> bool:
        """Whether this node-owned observer can wake the current episode.

        Conversation, provider, execution-machine alias, and package provenance
        are deliberately absent. The episode owns its session and policy; the
        watcher owns only the node, episode, and check execution host needed to
        answer the operational question.
        """

        continuation = record.continuation
        control_node_id = root_request.get("control_node_id")
        episode_matches = episode is None or (
            record.project_id == episode.project_id
            and episode.control_node_id == control_node_id
            and record.execution_host == episode.execution_host
        )
        return (
            continuation.patch_kind == "experiment_loop"
            and continuation.control_node_id == control_node_id
            and record.node_id == control_node_id
            and record.experiment_episode_id is not None
            and episode_matches
        )

    @staticmethod
    def _experiment_episode_root_request(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id, episode_id),
        ).fetchone()
        return json.loads(row["request_json"]) if row is not None else None

    def experiment_watcher_compatible_with_episode(
        self,
        watcher_id: str,
        episode_id: str,
    ) -> bool:
        """Whether a stopped observer belonged to that episode operationally.

        Watcher origin remains immutable provenance. This derived relation lets
        a fresh post-stop Run stage compatible adopted observers as history even
        when an older invocation or episode originally armed them.
        """

        with self.connection() as connection:
            watcher_row = connection.execute(
                "SELECT * FROM watchers WHERE watcher_id = ?",
                (watcher_id,),
            ).fetchone()
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if watcher_row is None or episode_row is None:
                return False
            record = self._watcher_record(watcher_row)
            episode = self._experiment_episode_record(episode_row)
            root_request = self._experiment_episode_root_request(
                connection,
                episode.project_id,
                episode.control_node_id,
                episode_id,
            )
        return root_request is not None and self._experiment_watcher_matches_current(
            record,
            root_request,
            episode,
        )

    def active_experiment_control_ids(self, project_id: str) -> set[str]:
        """Return Experiments whose newest operational episode is still live."""

        return {
            control_node_id
            for control_node_id, runtime in self._project_experiment_loop_runtimes(
                project_id, None
            ).items()
            if runtime.active
        }

    def completed_experiment_watcher_group(
        self,
        project_id: str,
        control_node_id: str,
    ) -> list[StoredWatcherRecord] | None:
        """Return the oldest frozen group a human may reauthorize.

        Unlike automatic delivery, human reauthorization preserves the full
        watcher configuration, including model, reasoning, and package pointers.
        """

        with self.connection() as connection:
            units = self._ready_watcher_delivery_units(connection)
        groups: dict[tuple[object, ...], list[StoredWatcherRecord]] = {}
        for unit in units:
            first = unit[0]
            if (
                first.project_id != project_id
                or first.continuation.patch_kind != "experiment_loop"
                or first.continuation.control_node_id != control_node_id
            ):
                continue
            key = (
                first.node_id,
                first.execution_host,
                self._automatic_watcher_delivery_policy(first.continuation),
            )
            groups.setdefault(key, []).extend(unit)
        return next(iter(groups.values()), None)

    @staticmethod
    def _experiment_wake_is_stopped(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> bool:
        """Refuse an automatic wake whose episode already carries a stop request.

        The check runs inside the claim's own write transaction, so a claim either
        commits before the stop or finds it — there is no window where both win.
        """

        request = record.request
        if request.get("patch_kind") != "experiment_loop" or request.get("trigger") != "watcher":
            return False
        episode_id = request.get("control_episode_id")
        if not isinstance(episode_id, str):
            return False
        row = connection.execute(
            "SELECT stop_requested_at FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return row is not None and row["stop_requested_at"] is not None
