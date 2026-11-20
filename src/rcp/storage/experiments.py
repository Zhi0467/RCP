from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import (
    EPISODE_RECEIPT_MAX_BYTES,
    QUESTION_SNAPSHOT_MAX_RECORDS,
    SPACE_RUNS_COMPLETED_TTL,
)
from rcp.machine_cards import BACKEND_MACHINE_ID, host_of
from rcp.storage.branch_names import _chain_root
from rcp.storage.digest import append_question_attention
from rcp.storage.episodes import _LIVE_EPISODE_STATUSES, _VISIBLE_EPISODE_REPORTS
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import (
    _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE,
    _EXPERIMENT_EPISODE_PINNED_FIELDS,
    _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC,
    _NON_PROMPT_CONTRACT_ROLES,
    _START_NEW_EPISODE,
    AgentTaskAdmissionConflict,
    AgentTaskRecord,
    AutoResearchChildExperimentRecord,
    EpisodeBudgetMeter,
    EpisodeInvocationCeilingReached,
    EpisodeNotRunning,
    EpisodeRecord,
    ExperimentControlProjectionSnapshot,
    ExperimentEpisodeProjectionSnapshot,
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    WatcherRecord,
    _experiment_pinned_value,
    _optional_str,
)
from rcp.storage.question_models import QuestionOrigin
from rcp.storage.questions import _question_record
from rcp.storage.watchers import _OPEN, _watcher_records

if TYPE_CHECKING:
    from rcp.storage import AppStore
    from rcp.watcher_folder import WatcherFolderChange
    from rcp.watchers import WatcherBinding

_STOP_LOOP_CANCEL_REASON = "Stop loop ended this Experiment episode."


@dataclass(frozen=True)
class _ExperimentResumeBinding:
    native_session_id: str
    stage_machine_id: str
    stage_root: str

    def workspace_problem(self, store: AppStore) -> str | None:
        if self.stage_machine_id and self.stage_machine_id != BACKEND_MACHINE_ID:
            from rcp.transport import RemoteRunStage

            try:
                available = RemoteRunStage(host_of(store, self.stage_machine_id)).directory_exists(
                    self.stage_root
                )
            except Exception as exc:
                raise OSError("The saved provider workspace could not be checked.") from exc
            if available is None:
                raise OSError("The saved provider workspace could not be checked.")
        else:
            stage = Path(self.stage_root)
            available = stage.is_dir() and not stage.is_symlink()
        return None if available is True else "the saved provider workspace is unavailable"


class ExperimentStoreMixin(StoreMixinBase):
    """Bounded Experiment episodes and their loop runtime projection."""

    @staticmethod
    def _experiment_question_binding_matches(
        record: AgentTaskRecord, origin: QuestionOrigin, original: AgentTaskRecord
    ) -> bool:
        # As with answer wakes, prove the saved authority and its execution inputs.
        return (
            record.project_id == origin.project_id
            and record.native_session_id == origin.native_session_id
            and record.request.get("provider") == origin.provider
            and record.stage_root == origin.stage_root
            and (record.stage_machine_id or "") == (origin.stage_machine_id or "")
            and record.graph_target == origin.graph_target
            and original.dispatch_authority is not None
            and original.dispatch_authority.task_contract == origin.capability
            and original.request.get("mode") == "work"
            and all(
                record.request.get(key) == original.request.get(key)
                for key in ("provider", "run_on", "run_truth_scope", "mode")
            )
        )

    def experiment_question_owner_ids(self, episode_id: str) -> list[str]:
        """Follow continuation provenance without moving immutable question origins."""
        owners: list[str] = []
        while episode_id and episode_id not in owners:
            episode = self.episode(episode_id)
            if episode is None or episode.mode != "experiment_loop":
                break
            owners.append(episode_id)
            episode_id = episode.continues_episode_id or ""
        return owners

    def experiment_has_question_continuation(
        self, project_id: str, episode_id: str, *, operation_id: str | None = None
    ) -> bool:
        # Successful settlement will receipt these live answers. They cannot also
        # justify an otherwise empty handoff and leave a running episode stranded.
        acknowledged = (
            {
                (receipt.payload.get("question_id"), receipt.payload.get("answer_revision"))
                for receipt in self.agent_task_receipts_by_category(
                    operation_id, "question_answer_acknowledged"
                )
            }
            if operation_id
            else set()
        )
        return any(
            not q.withdrawn_readonly
            and (
                q.state == "pending"
                or (
                    q.state == "answered"
                    and q.client_receipt_revision is None
                    and q.followup_operation_id is None
                    and (q.question_id, q.answer_revision) not in acknowledged
                )
            )
            for owner in self.experiment_question_owner_ids(episode_id)
            for q in self.list_questions(
                project_id=project_id, owner_kind="episode", owner_id=owner
            )
        )

    def create_experiment_question_invocation(
        self, record: AgentTaskRecord, *, question_id: str, answer_revision: int
    ) -> AgentTaskRecord | None:
        """Claim an answer and spend one normal Experiment invocation atomically."""
        question = self.get_question(question_id)
        if question is None or question.origin.owner_kind != "episode":
            raise ValueError("Experiment answer has no episode question")
        origin = question.origin
        if (
            record.status != "queued"
            or record.parent_operation_id is not None
            or record.request.get("patch_kind") != "experiment_loop"
            or record.request.get("trigger") != "watcher"
            or origin.owner_id not in self.experiment_question_owner_ids(record.episode_id or "")
        ):
            raise ValueError("Experiment answer changed its origin binding")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state,answer_revision,client_receipt_revision,followup_operation_id,"
                "withdrawn_readonly FROM questions WHERE question_id=?",
                (question_id,),
            ).fetchone()
            if (
                current is None
                or current["state"] != "answered"
                or current["answer_revision"] != answer_revision
                or current["client_receipt_revision"] is not None
                or current["followup_operation_id"] is not None
                or current["withdrawn_readonly"]
            ):
                return None
            row = connection.execute(
                "SELECT * FROM episodes WHERE episode_id=?", (record.episode_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Experiment answer lost its episode")
            episode = self._episode_record(row)
            if (
                episode.status != "running"
                or episode.ending is not None
                or episode.stop_requested_at is not None
                or self._experiment_has_ending_receipt(connection, episode.episode_id)
            ):
                return None
            if episode.invocations_used >= episode.invocation_ceiling:
                raise EpisodeInvocationCeilingReached("the episode requires human reauthorization")
            if record.request.get("control_invocation") != episode.invocations_used + 1:
                raise ValueError("Experiment answer invocation is out of sequence")
            if connection.execute(
                "SELECT 1 FROM auto_research_child_experiments WHERE child_episode_id=?",
                (episode.episode_id,),
            ).fetchone():
                raise ValueError("An orchestrator child cannot receive human questions")
            asking = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id=?", (origin.operation_id,)
            ).fetchone()
            if asking is None or asking["status"] not in {"succeeded", "failed"}:
                return None
            original = self._agent_task_record(asking)
            if not self._experiment_question_binding_matches(record, origin, original):
                raise ValueError("Experiment question origin authority is unavailable")
            if self._has_resumable_paused_chat_task(
                connection, origin.project_id, original.kind, original.request.get("chat_id")
            ):
                return None
            origin_stage_card = origin.stage_machine_id
            if origin_stage_card and self._unresolved_remote_provider_passes(
                connection, origin_stage_card, origin.stage_root
            ):
                return None
            self._require_project_accepts_new_work(connection, origin.project_id)
            if self._has_active_chat_overlap(connection, record):
                return None
            self._validate_experiment_wake_binding(connection, record)
            if not self.claim_question_followup(
                connection,
                question_id,
                answer_revision=answer_revision,
                operation_id=record.operation_id,
            ):
                return None
            self._insert_agent_task(connection, record, continuation_cause="message_wake")
            connection.execute(
                "INSERT INTO episode_invocations (episode_id,operation_id,invocation_number,created_at) VALUES (?,?,?,?)",
                (
                    episode.episode_id,
                    record.operation_id,
                    episode.invocations_used + 1,
                    record.created_at,
                ),
            )
            connection.execute(
                "UPDATE episodes SET invocations_used=invocations_used+1,updated_at=? WHERE episode_id=?",
                (record.created_at, episode.episode_id),
            )
        return self.agent_task(record.operation_id)

    @staticmethod
    def _experiment_episode_row(
        connection: sqlite3.Connection,
        episode_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT state.*, episode.project_id, episode.control_node_id,
                   episode.graph_target_json, episode.browser_requested,
                   episode.stop_requested_at, episode.stop_settled_at
            FROM experiment_episode_state AS state
            JOIN episodes AS episode ON episode.episode_id = state.episode_id
            WHERE state.episode_id = ? AND episode.mode = 'experiment_loop'
            """,
            (episode_id,),
        ).fetchone()

    def create_experiment_episode_with_invocation(
        self,
        record: AgentTaskRecord,
        *,
        auto_research_route: AutoResearchChildExperimentRecord | None = None,
        auto_research_admission_id: str | None = None,
        continues_episode_id: str | None = None,
        continuation_request_id: str | None = None,
        graph_base_head: GraphHeadRef | None = None,
    ) -> AgentTaskRecord:
        """Atomically create the Experiment parent, mode child, and invocation 1.

        A continuation names the ended episode whose native session and stage
        its first invocation resumes; the source must be continuable under
        ``_require_continuable_source`` and control the same Experiment.
        """

        if continues_episode_id is not None and auto_research_route is not None:
            raise ValueError("an orchestrator Experiment start cannot continue an episode")
        parent_episode = (
            self.episode(auto_research_route.auto_research_episode_id)
            if auto_research_route is not None
            else None
        )
        if auto_research_route is not None and (
            parent_episode is None
            or parent_episode.mode != "auto_research"
            or (
                record.graph_target != parent_episode.graph_target
                and record.graph_target.branch_id
                not in {
                    branch.branch_id
                    for branch in cast("AppStore", self).branches_owned_by(
                        record.project_id, parent_episode.episode_id
                    )
                }
            )
        ):
            raise ValueError("an Auto-research child Experiment changed its parent graph target")
        graph_branch = (
            cast("AppStore", self).graph_branch(record.project_id, record.graph_target.branch_id)
            if record.graph_target.kind == "branch" and record.graph_target.branch_id is not None
            else None
        )
        owns_branch = graph_base_head is not None and graph_branch is None
        if graph_base_head is not None and (
            not owns_branch
            or graph_base_head.target.kind != "main"
            or record.request.get("graph_isolation") is not True
            or auto_research_route is not None
            or continues_episode_id is not None
        ):
            raise ValueError("experiment_graph_isolation_invalid_base")
        if (
            record.graph_target.kind == "branch"
            and not owns_branch
            and (graph_branch is None or graph_branch.project_id != record.project_id)
        ):
            raise ValueError("a branch Experiment requires its exact project graph branch")
        episode = self._new_experiment_episode(
            record,
            auto_research_route=auto_research_route,
            graph_base_head=(graph_branch.parent_head if graph_branch else graph_base_head),
            continues_episode_id=continues_episode_id,
            continuation_request_id=continuation_request_id,
        )
        source = self.episode(continues_episode_id) if continues_episode_id else None
        branch_owner = (
            cast("AppStore", self).episode(graph_branch.owner_episode_id)
            if graph_branch is not None and graph_branch.owner_episode_id is not None
            else None
        )
        isolation_parent = source or parent_episode or branch_owner
        # A human-owned fork has no owner episode. Its owner is reserved under the
        # admission lock below, so two Runs admitted before either launch share it.
        human_fork = graph_branch is not None and isolation_parent is None

        def inherit_isolation(
            episode: EpisodeRecord, record: AgentTaskRecord, owner: EpisodeRecord | None
        ) -> tuple[EpisodeRecord, AgentTaskRecord]:
            if owner is not None:
                episode = episode.model_copy(
                    update={
                        "isolation_owner_episode_id": owner.isolation_owner_episode_id
                        or owner.episode_id,
                        "code_worktree": owner.code_worktree,
                        "graph_isolation": owner.graph_isolation,
                    }
                )
            # A human-owned fork's effective isolation comes from its branch
            # target; record that, not the request's.
            if owner is not None or graph_branch is not None:
                record = record.model_copy(
                    update={
                        "request": {
                            **record.request,
                            "isolation_owner_episode_id": episode.isolation_owner_episode_id,
                            "code_worktree": episode.code_worktree,
                            "graph_isolation": episode.graph_isolation,
                        }
                    }
                )
            return episode, record

        episode, record = inherit_isolation(episode, record, isolation_parent)
        browser_parent = source or parent_episode
        if browser_parent is not None:
            episode = episode.model_copy(
                update={"browser_requested": browser_parent.browser_requested}
            )
            record = record.model_copy(
                update={
                    "request": {**record.request, "browser_requested": episode.browser_requested}
                }
            )
        self._validate_new_episode(episode)
        self._validate_experiment_watcher_ids(record, [])
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if (
                    auto_research_route is None
                    and record.request.get("trigger") == "experiment_run"
                ):
                    self._require_project_accepts_new_work(connection, record.project_id)
                if (
                    connection.execute(
                        "SELECT 1 FROM episodes WHERE episode_id = ?", (episode.episode_id,)
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("This Experiment episode id is already in use.")
                if self._live_episode_row(connection, episode) is not None:
                    raise ValueError("This Experiment already has a live episode.")
                if human_fork:
                    # The fork's launch binding, or else its first admitted Experiment.
                    owner_row = connection.execute(
                        "SELECT * FROM episodes WHERE episode_id = COALESCE("
                        "(SELECT owner_episode_id FROM episode_isolations "
                        "WHERE project_id = ? AND graph_branch_id = ?), "
                        "(SELECT COALESCE(isolation_owner_episode_id, episode_id) FROM episodes "
                        "WHERE project_id = ? AND graph_target_json = ? AND mode = 'experiment_loop' "
                        "ORDER BY created_at, episode_id LIMIT 1))",
                        (
                            record.project_id,
                            record.graph_target.branch_id,
                            record.project_id,
                            graph_target_json(record.graph_target),
                        ),
                    ).fetchone()
                    if owner_row is not None:
                        episode, record = inherit_isolation(
                            episode, record, cast("AppStore", self)._episode_record(owner_row)
                        )
                if continues_episode_id is not None:
                    source_row = connection.execute(
                        "SELECT * FROM episodes WHERE episode_id = ?", (continues_episode_id,)
                    ).fetchone()
                    if source_row is None:
                        raise ValueError("The continued Experiment episode no longer exists.")
                    source = self._episode_record(source_row)
                    if (
                        source.mode != "experiment_loop"
                        or source.control_node_id != episode.control_node_id
                    ):
                        raise ValueError(
                            "a continuation controls the same Experiment as its source"
                        )
                    self._require_continuable_source(connection, source, episode)
                started = episode.model_copy(
                    update={
                        "root_operation_id": record.operation_id,
                        "status": "running",
                        "invocations_used": 1,
                        "updated_at": record.created_at,
                    }
                )
                if auto_research_route is not None:
                    if (
                        auto_research_route.child_episode_id != episode.episode_id
                        or auto_research_route.project_id != episode.project_id
                        or auto_research_route.control_node_id != episode.control_node_id
                    ):
                        raise ValueError(
                            "the Auto-research route does not match the Experiment episode"
                        )
                    self._activate_auto_research_child_experiment(
                        connection,
                        auto_research_route,
                        admission_id=auto_research_admission_id,
                    )
                self._insert_episode(
                    connection, started, branch_name=record.request.get("branch_name")
                )
                if continues_episode_id is not None:
                    reopened = connection.execute(
                        """WITH RECURSIVE owners(episode_id) AS (
                            SELECT ? UNION ALL SELECT e.continues_episode_id FROM episodes e
                            JOIN owners o ON e.episode_id=o.episode_id
                            WHERE e.continues_episode_id IS NOT NULL
                        ) UPDATE questions SET withdrawn_readonly=0
                        WHERE owner_kind='episode' AND owner_id IN (SELECT episode_id FROM owners)
                        RETURNING *""",
                        (continues_episode_id,),
                    ).fetchall()
                    claimable = []
                    for question_row in reopened:
                        question = _question_record(question_row)
                        if (
                            question.state != "answered"
                            or question.client_receipt_revision is not None
                            or question.followup_operation_id is not None
                        ):
                            continue
                        asking = connection.execute(
                            "SELECT * FROM graph_runs WHERE operation_id=?",
                            (question.origin.operation_id,),
                        ).fetchone()
                        if (
                            asking is None
                            or asking["status"] not in {"succeeded", "failed"}
                            or not self._experiment_question_binding_matches(
                                record, question.origin, self._agent_task_record(asking)
                            )
                        ):
                            connection.execute(
                                "UPDATE questions SET withdrawn_readonly=1 WHERE question_id=?",
                                (question.question_id,),
                            )
                            continue
                        claimable.append(question)
                    for question_row in reopened:
                        current = connection.execute(
                            "SELECT * FROM questions WHERE question_id=?",
                            (question_row["question_id"],),
                        ).fetchone()
                        append_question_attention(connection, current, record.created_at)
                    # Claim only what this invocation's snapshot carries: claimed answers
                    # sort first there, by the same key, under the same cap. The rest stay
                    # unclaimed for later answer wakes.
                    claimable.sort(key=lambda item: (item.created_at, item.question_id))
                    for question in claimable[:QUESTION_SNAPSHOT_MAX_RECORDS]:
                        self.claim_question_followup(
                            connection,
                            question.question_id,
                            answer_revision=question.answer_revision,
                            operation_id=record.operation_id,
                        )
                connection.execute(
                    """
                    INSERT INTO experiment_episode_state (
                        episode_id, machine_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        episode.episode_id,
                        record.request["run_on"],
                        episode.created_at,
                        record.created_at,
                    ),
                )
                self._insert_agent_task(connection, record, continuation_cause="fresh")
                if auto_research_route is not None:
                    self._claim_auto_research_experiment_allowance(
                        connection,
                        auto_research_episode_id=auto_research_route.auto_research_episode_id,
                        child_episode_id=episode.episode_id,
                        operation_id=record.operation_id,
                        created_at=record.created_at,
                    )
                connection.execute(
                    """
                    INSERT INTO episode_invocations (
                        episode_id, operation_id, invocation_number, created_at
                    ) VALUES (?, ?, 1, ?)
                    """,
                    (episode.episode_id, record.operation_id, record.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Could not create the Experiment episode; an episode is continued at most once."
            ) from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def create_experiment_watcher_invocation(
        self,
        record: AgentTaskRecord,
        watcher_ids: list[str],
    ) -> AgentTaskRecord | None:
        """Check Stop, ending and budget, spend one invocation, and claim the wake at once.

        None means the episode is ending or the claim lost: nothing was written.
        """

        ids = list(watcher_ids)
        agent_id = record.request.get("chat_id")
        if (
            record.status != "queued"
            or not record.visible
            or record.parent_operation_id is not None
            or record.kind != "node_chat"
            or record.request.get("patch_kind") != "experiment_loop"
            or record.request.get("trigger") != "watcher"
            or record.request.get("control_episode_id") != record.episode_id
            or record.request.get("node_id") != record.request.get("control_node_id")
            or not isinstance(agent_id, str)
            or not agent_id
        ):
            raise ValueError("An Experiment watcher wake must be a queued paid root task.")
        self._validate_experiment_watcher_ids(record, ids)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                episode_id = record.episode_id
                assert episode_id is not None
                state_row = self._experiment_episode_row(connection, episode_id)
                if state_row is None:
                    raise KeyError(episode_id)
                episode_row = connection.execute(
                    "SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)
                ).fetchone()
                episode = self._episode_record(episode_row)
                if (
                    episode.project_id != record.project_id
                    or episode.control_node_id != record.request.get("control_node_id")
                    or episode.graph_target != record.graph_target
                ):
                    raise ValueError("The watcher wake belongs to another episode.")
                if (
                    episode.status != "running"
                    or episode.ending is not None
                    or episode.stop_requested_at is not None
                    or self._experiment_has_ending_receipt(connection, episode_id)
                ):
                    return None
                if episode.invocations_used >= episode.invocation_ceiling:
                    raise EpisodeInvocationCeilingReached(
                        "the episode has spent its operational invocation ceiling"
                    )
                expected = episode.invocations_used + 1
                if record.request.get("control_invocation") != expected:
                    raise ValueError(
                        f"Experiment-loop invocation is out of sequence; expected {expected}."
                    )
                if state_row["chat_id"] != agent_id:
                    raise ValueError("The watcher wake belongs to another agent.")
                if self._has_active_chat_overlap(connection, record):
                    return None
                if not cast("AppStore", self).claim_watcher_wake(
                    connection, agent_id, ids, record.operation_id
                ):
                    return None
                route = connection.execute(
                    """
                    SELECT auto_research_episode_id
                    FROM auto_research_child_experiments
                    WHERE child_episode_id = ? AND state = 'running'
                    """,
                    (episode_id,),
                ).fetchone()
                self._insert_agent_task(connection, record, continuation_cause="watcher_wake")
                if route is not None:
                    self._claim_auto_research_experiment_allowance(
                        connection,
                        auto_research_episode_id=str(route["auto_research_episode_id"]),
                        child_episode_id=episode_id,
                        operation_id=record.operation_id,
                        created_at=record.created_at,
                    )
                connection.execute(
                    """
                    INSERT INTO episode_invocations (
                        episode_id, operation_id, invocation_number, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (episode_id, record.operation_id, expected, record.created_at),
                )
                connection.execute(
                    """
                    UPDATE episodes
                    SET invocations_used = ?, updated_at = ?
                    WHERE episode_id = ?
                    """,
                    (expected, self.now(), episode_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not queue the Experiment watcher wake.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def create_experiment_recovery_task(
        self,
        record: AgentTaskRecord,
        *,
        continuation_cause: str = "resume",
    ) -> AgentTaskRecord:
        """Create a recovery/repair child without spending another paid invocation."""

        if (
            record.status != "queued"
            or not record.visible
            or record.parent_operation_id is None
            or record.episode_id is None
        ):
            raise ValueError("An Experiment recovery requires its parent and episode lineage.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_experiment_recovery_task(
                connection,
                record,
                continuation_cause=continuation_cause,
            )
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def create_experiment_graph_repair_task(
        self,
        parent_operation_id: str,
        record: AgentTaskRecord,
    ) -> AgentTaskRecord:
        """Atomically claim an Experiment graph repair and admit its child task."""

        if (
            record.status != "queued"
            or not record.visible
            or record.parent_operation_id != parent_operation_id
            or record.episode_id is None
            or record.request.get("patch_kind") != "experiment_loop"
            or record.request.get("message") is not None
        ):
            raise ValueError("An Experiment graph repair requires a queued patch-only child.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._claim_agent_task_graph_repair(connection, parent_operation_id)
            self._insert_experiment_recovery_task(
                connection,
                record,
                continuation_cause="graph_repair",
            )
            stored = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id = ?", (record.operation_id,)
            ).fetchone()
            assert stored is not None
        return self._agent_task_record(stored)

    def _insert_experiment_recovery_task(
        self,
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
        *,
        continuation_cause: str,
    ) -> None:
        """Insert one validated Experiment recovery task in an open transaction."""

        episode_id = record.episode_id
        assert episode_id is not None
        episode_row = connection.execute(
            "SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if episode_row is None:
            raise KeyError(episode_id)
        episode = self._episode_record(episode_row)
        if (
            episode.mode != "experiment_loop"
            or episode.project_id != record.project_id
            or episode.graph_target != record.graph_target
            or episode.control_node_id != record.request.get("control_node_id")
        ):
            raise ValueError("The recovery task belongs to another episode.")
        route = connection.execute(
            """
            SELECT auto_research_episode_id, state
            FROM auto_research_child_experiments
            WHERE child_episode_id = ?
            """,
            (episode_id,),
        ).fetchone()
        if route is not None:
            if route["state"] != "running":
                raise EpisodeNotRunning(
                    "the routed child Experiment is no longer accepting recovery work"
                )
            parent = self._load_auto_research_episode(
                connection,
                str(route["auto_research_episode_id"]),
            )
            # Recovery settles an already-paid invocation, including behind its
            # parent's Stop or ending fence. Only a new invocation needs E and
            # the parent's new-work admission check.
            if parent.project_id != episode.project_id or (
                parent.graph_target != episode.graph_target
                and connection.execute(
                    "SELECT 1 FROM graph_branches WHERE project_id=? AND branch_id=? "
                    "AND owner_episode_id=?",
                    (
                        episode.project_id,
                        episode.graph_target.branch_id,
                        _chain_root(connection, episode.project_id, parent.episode_id),
                    ),
                ).fetchone()
                is None
            ):
                raise ValueError("The Experiment recovery changed its parent graph target.")
        if (
            episode.status not in {"running", "stopping"}
            or episode.ending is not None
            or self._experiment_has_ending_receipt(connection, episode_id)
        ):
            raise EpisodeNotRunning("the episode is not admitting recovery work")
        if not self._experiment_recovery_has_paid_root(
            connection,
            episode_id,
            record.parent_operation_id,
        ):
            raise ValueError("The Experiment recovery has no paid invocation ancestor.")
        if (
            connection.execute(
                """
            SELECT 1 FROM graph_runs
            WHERE episode_id = ? AND status IN ('queued', 'running', 'pausing')
            LIMIT 1
            """,
                (episode_id,),
            ).fetchone()
            is not None
        ):
            raise AgentTaskAdmissionConflict("Another task is already active in this episode.")
        latest = connection.execute(
            """
            SELECT operation_id,
                   json_extract(request_json, '$.control_invocation') AS invocation
            FROM graph_runs
            WHERE episode_id = ? AND visible = 1
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (episode_id,),
        ).fetchone()
        if (
            latest is None
            or latest["operation_id"] != record.parent_operation_id
            or latest["invocation"] != record.request.get("control_invocation")
        ):
            raise AgentTaskAdmissionConflict(
                "Only the latest Experiment task can be recovered. Refresh and use its recovery."
            )
        if self._has_active_chat_overlap(connection, record):
            raise AgentTaskAdmissionConflict("Another task is already active in this conversation.")
        self._insert_agent_task(
            connection,
            record,
            continuation_cause=continuation_cause,
        )

    @staticmethod
    def _new_experiment_episode(
        record: AgentTaskRecord,
        *,
        auto_research_route: AutoResearchChildExperimentRecord | None = None,
        graph_base_head: GraphHeadRef | None = None,
        continues_episode_id: str | None = None,
        continuation_request_id: str | None = None,
    ) -> EpisodeRecord:
        request = record.request
        episode_id = request.get("control_episode_id")
        control_node_id = request.get("control_node_id")
        ceiling = request.get("control_invocation_ceiling")
        expected_trigger = "orchestrator" if auto_research_route is not None else "experiment_run"
        if (
            record.status != "queued"
            or not record.visible
            or record.kind != "node_chat"
            or record.parent_operation_id is not None
            or request.get("patch_kind") != "experiment_loop"
            or request.get("trigger") != expected_trigger
            or request.get("control_invocation") != 1
            or not isinstance(episode_id, str)
            or record.episode_id != episode_id
            or not isinstance(control_node_id, str)
            or not control_node_id
            or request.get("node_id") != control_node_id
            or not isinstance(ceiling, int)
            or isinstance(ceiling, bool)
            or ceiling < 1
        ):
            raise ValueError("An Experiment Run must be its episode's queued invocation 1.")
        episode = EpisodeRecord(
            episode_id=episode_id,
            project_id=record.project_id,
            mode="experiment_loop",
            isolation_owner_episode_id=episode_id,
            browser_requested=bool(request.get("browser_requested", False)),
            code_worktree=bool(request.get("code_worktree", False)),
            graph_isolation=bool(request.get("graph_isolation", False)),
            control_node_id=control_node_id,
            graph_target=record.graph_target,
            graph_base_head=graph_base_head,
            status="queued",
            invocation_ceiling=ceiling,
            authorized_by=record.authorized_by,
            created_at=record.created_at,
            updated_at=record.created_at,
            continues_episode_id=continues_episode_id,
            continuation_request_id=continuation_request_id,
        )
        ExperimentStoreMixin._validate_new_experiment_episode(episode)
        return episode

    @staticmethod
    def _validate_new_experiment_episode(episode: EpisodeRecord) -> None:
        if episode.authorized_by is None:
            raise ValueError("A new Experiment episode requires its human authorization snapshot.")

    @staticmethod
    def _validate_experiment_watcher_ids(
        record: AgentTaskRecord,
        watcher_ids: list[str],
    ) -> None:
        if len(watcher_ids) != len(set(watcher_ids)):
            raise ValueError("an Experiment watcher claim requires unique watcher ids")
        requested = record.request.get("watcher_ids")
        if (
            not isinstance(requested, list)
            or any(not isinstance(item, str) for item in requested)
            or set(requested) != set(watcher_ids)
            or len(requested) != len(watcher_ids)
        ):
            raise ValueError("the Experiment task must name exactly its watcher ids")

    @staticmethod
    def _experiment_recovery_has_paid_root(
        connection: sqlite3.Connection,
        episode_id: str,
        operation_id: str,
    ) -> bool:
        current_id: str | None = operation_id
        seen: set[str] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            if (
                connection.execute(
                    """
                SELECT 1 FROM episode_invocations
                WHERE episode_id = ? AND operation_id = ?
                """,
                    (episode_id, current_id),
                ).fetchone()
                is not None
            ):
                return True
            row = connection.execute(
                "SELECT parent_operation_id FROM graph_runs WHERE operation_id = ?",
                (current_id,),
            ).fetchone()
            current_id = (
                str(row["parent_operation_id"]) if row and row["parent_operation_id"] else None
            )
        return False

    @staticmethod
    def _experiment_has_ending_receipt(
        connection: sqlite3.Connection,
        episode_id: str,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM graph_run_receipts AS receipt
                JOIN graph_runs AS run ON run.operation_id = receipt.operation_id
                WHERE run.episode_id = ? AND receipt.category = 'experiment_loop_exit'
                LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
            is not None
        )

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
        if record.episode_id != episode_id:
            raise ValueError("A bounded experiment-loop task must use its episode parent lineage.")
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
                    graph_target=record.graph_target,
                    episode_id=episode_id,
                    invocation=invocation,
                    operation_id=record.parent_operation_id,
                )
            return

        trigger = request.get("trigger")
        if trigger not in {"experiment_run", "orchestrator", "watcher"}:
            raise ValueError(
                "A root experiment-loop task must be a Run, orchestrator, or watcher invocation."
            )
        if trigger == "orchestrator":
            route = connection.execute(
                """
                SELECT 1 FROM auto_research_child_experiments
                WHERE child_episode_id = ? AND project_id = ? AND control_node_id = ?
                  AND state = 'running'
                """,
                (episode_id, record.project_id, node_id),
            ).fetchone()
            if route is None:
                raise ValueError(
                    "An orchestrator Experiment start requires its active Auto-research route."
                )
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
        if trigger in {"experiment_run", "orchestrator"} and invocation != 1:
            raise ValueError("A Run or orchestrator start must be experiment-loop invocation 1.")
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
        episode = ExperimentStoreMixin._experiment_episode_row(connection, str(episode_id))
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
            "machine_id": request.get("run_on"),
            "native_session_id": session_id,
            "stage_machine_id": record.stage_machine_id or "",
            "stage_root": record.stage_root,
            "chat_id": request.get("chat_id"),
            "model": request.get("model"),
            "reasoning": request.get("reasoning"),
        }
        actual = {
            "project_id": episode["project_id"],
            "control_node_id": episode["control_node_id"],
            "provider": episode["provider"],
            "machine_id": episode["machine_id"],
            "native_session_id": episode["native_session_id"],
            "stage_machine_id": episode["stage_machine_id"] or "",
            "stage_root": episode["stage_root"],
            "chat_id": episode["chat_id"],
            "model": binding_request.get("model"),
            "reasoning": binding_request.get("reasoning"),
        }
        mismatched = sorted(key for key, value in expected.items() if actual[key] != value)
        if mismatched:
            raise ValueError(
                "The automatic Experiment wake no longer matches its episode binding: "
                + ", ".join(mismatched)
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
              AND json_extract(graph_target_json, '$.kind') = ?
              AND json_extract(graph_target_json, '$.branch_id') IS ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (
                record.project_id,
                parent_request["control_node_id"],
                record.graph_target.kind,
                record.graph_target.branch_id,
            ),
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
        graph_target: GraphTargetRef,
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
              AND json_extract(graph_target_json, '$.kind') = ?
              AND json_extract(graph_target_json, '$.branch_id') IS ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id, graph_target.kind, graph_target.branch_id),
        ).fetchone()
        if newest_root is None or newest_root["episode_id"] != episode_id:
            raise ValueError("Only the newest Experiment episode can repair its graph update.")
        stopped = connection.execute(
            "SELECT stop_requested_at FROM episodes "
            "WHERE episode_id = ? AND mode = 'experiment_loop'",
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

    def experiment_episode(self, episode_id: str) -> ExperimentEpisodeRecord | None:
        with self.connection() as connection:
            row = self._experiment_episode_row(connection, episode_id)
        return self._experiment_episode_record(row) if row is not None else None

    def experiment_episode_ending_signal(
        self,
        episode_id: str,
    ) -> tuple[str, dict[str, object]] | None:
        """Return the exact accepted continuation and its mode-owned ending receipt."""

        with self.connection() as connection:
            state = self._experiment_episode_row(connection, episode_id)
            operation_id = state["last_turn_operation_id"] if state is not None else None
            if not isinstance(operation_id, str) or not operation_id:
                return None
            receipt = connection.execute(
                """
                SELECT payload_json FROM graph_run_receipts
                WHERE operation_id = ? AND category = 'experiment_loop_exit'
                ORDER BY receipt_id DESC LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
        if receipt is None:
            return None
        signal = json.loads(receipt["payload_json"])
        if not isinstance(signal, dict) or signal.get("episode_id") != episode_id:
            raise ValueError("The Experiment ending receipt has invalid episode lineage.")
        return operation_id, signal

    def settle_experiment_episode_wrapup(self, episode_id: str) -> bool:
        """Whether this episode's non-Stop ending is fenced and its turns are settled.

        The generic episode fence prevents any later watcher claim from spending
        another invocation. The agent keeps its open watchers, so a continuation
        of the same chat can still be woken by them.
        """

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode = connection.execute(
                """
                SELECT * FROM episodes
                WHERE episode_id = ? AND mode = 'experiment_loop'
                """,
                (episode_id,),
            ).fetchone()
            if episode is None:
                raise KeyError(episode_id)
            if episode["ending"] is None or episode["ending"] == "stopped":
                return False
            active = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE episode_id = ? AND visible = 1
                  AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
            if active is not None:
                return False
        return True

    def experiment_episode_recovery_context_problem(self, operation_id: str) -> str | None:
        """Explain why this task lineage cannot retain its episode context on recovery."""

        with self.connection() as connection:
            return self._experiment_episode_recovery_context_problem(connection, operation_id)

    def experiment_episode_context_root(self, operation_id: str) -> str | None:
        """Name the attempt whose episode context a recovery of this lineage keeps.

        That is the newest attempt that recorded a candidate. Only the root does,
        unless it never sent a prompt and was run again as itself; the rerun's
        candidate is then the one the provider saw.
        """

        with self.connection() as connection:
            lineage, problem = self._experiment_task_lineage(connection, operation_id)
            if problem is not None:
                raise ValueError(problem)
            return self._newest_attempt_with_candidate(connection, lineage)

    def experiment_lineage_never_composed(self, operation_id: str) -> bool:
        """Whether no attempt in this lineage got far enough to send the provider anything."""

        with self.connection() as connection:
            lineage, problem = self._experiment_task_lineage(connection, operation_id)
            return problem is None and self._lineage_never_composed(connection, lineage)

    @staticmethod
    def _experiment_task_lineage(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> tuple[list[str], str | None]:
        """Return the lineage oldest attempt first, or the problem that broke the walk."""

        lineage: list[str] = []
        current_id = operation_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                return [], (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage contains a cycle. " + _START_NEW_EPISODE
                )
            seen.add(current_id)
            row = connection.execute(
                "SELECT parent_operation_id FROM graph_runs WHERE operation_id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                return [], (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage is incomplete. " + _START_NEW_EPISODE
                )
            lineage.append(current_id)
            if row["parent_operation_id"] is None:
                lineage.reverse()
                return lineage, None
            current_id = str(row["parent_operation_id"])

    @staticmethod
    def _newest_attempt_with_candidate(
        connection: sqlite3.Connection,
        lineage: list[str],
    ) -> str | None:
        for current_id in reversed(lineage):
            found = connection.execute(
                "SELECT 1 FROM graph_run_contracts WHERE operation_id = ? AND role = ? LIMIT 1",
                (current_id, _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE),
            ).fetchone()
            if found is not None:
                return current_id
        return None

    @staticmethod
    def _lineage_never_composed(connection: sqlite3.Connection, lineage: list[str]) -> bool:
        """No prompt contract anywhere: every attempt failed before its prompt.

        A watcher wake whose link dropped while its stage was prepared is one,
        even when it had already recorded its candidate. It sent the provider
        nothing, so there is no prompt to continue.
        """

        roles = sorted(_NON_PROMPT_CONTRACT_ROLES)
        placeholders = ", ".join("?" for _ in roles)
        return all(
            connection.execute(
                "SELECT 1 FROM graph_run_contracts WHERE operation_id = ? "
                f"AND role NOT IN ({placeholders}) LIMIT 1",
                (current_id, *roles),
            ).fetchone()
            is None
            for current_id in lineage
        )

    @staticmethod
    def _experiment_episode_recovery_context_problem(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> str | None:
        """Validate the immutable candidate on an Experiment lineage's context root."""

        unrecoverable = connection.execute(
            """
            SELECT payload_json FROM graph_run_receipts
            WHERE operation_id = ? AND category = 'experiment_unrecoverable_deliverable'
            ORDER BY receipt_id DESC LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        if unrecoverable is not None:
            try:
                payload = json.loads(unrecoverable["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = None
            diagnostic = payload.get("diagnostic") if isinstance(payload, dict) else None
            if isinstance(diagnostic, str) and diagnostic.strip():
                return "This Experiment-loop turn cannot continue: " + diagnostic.strip()
            return "This Experiment-loop turn cannot continue because its output was unreadable."

        lineage, problem = ExperimentStoreMixin._experiment_task_lineage(connection, operation_id)
        if problem is not None:
            return problem
        if ExperimentStoreMixin._lineage_never_composed(connection, lineage):
            # Nothing was sent, so there is no context to keep: recovery re-runs
            # the turn and records the context that run sends.
            return None
        context_root = ExperimentStoreMixin._newest_attempt_with_candidate(connection, lineage)
        if context_root is None:
            return _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC
        contract = connection.execute(
            """
            SELECT content FROM graph_run_contracts
            WHERE operation_id = ? AND role = ?
            """,
            (context_root, _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE),
        ).fetchone()
        try:
            candidate = json.loads(contract["content"])
        except (json.JSONDecodeError, TypeError):
            candidate = None
        if not isinstance(candidate, dict):
            return (
                "This Experiment-loop turn cannot be resumed or retried because its retained "
                "episode context candidate is invalid. " + _START_NEW_EPISODE
            )
        return None

    def experiment_episode_resume_binding_problem(self, operation_id: str) -> str | None:
        """Check the exact saved Experiment session and workspace without changing state."""

        with self.connection() as connection:
            binding = self._experiment_episode_resume_binding(connection, operation_id)
        return (
            binding
            if isinstance(binding, str)
            else binding.workspace_problem(cast("AppStore", self))
        )

    @staticmethod
    def _experiment_episode_resume_binding(
        connection: sqlite3.Connection, operation_id: str
    ) -> _ExperimentResumeBinding | str:
        """Validate persisted recovery inputs without contacting the execution host."""

        task = connection.execute(
            "SELECT * FROM graph_runs WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if task is None:
            return "the saved Experiment task is unavailable"
        if task["status"] not in {"paused", "interrupted", "failed"}:
            return "only a paused, interrupted, or failed attempt can be resumed"
        receipts = connection.execute(
            "SELECT category, payload_json FROM graph_run_receipts "
            "WHERE operation_id = ? AND category IN "
            "('provider_terminal_error', 'continuation_context_unavailable')",
            (operation_id,),
        ).fetchall()
        context_unavailable = False
        for receipt in receipts:
            payload = json.loads(receipt["payload_json"])
            context_unavailable |= (
                receipt["category"] == "continuation_context_unavailable"
                and payload.get("retry_required") is True
            )
        if context_unavailable:
            return "the saved continuation context is unavailable"
        # Experiment episodes are node chats: a persisted native session is RCP-owned.
        if task["kind"] != "node_chat" or not task["native_session_id"] or not task["stage_root"]:
            return "the attempt has no complete RCP-owned session and stage"
        return _ExperimentResumeBinding(
            native_session_id=str(task["native_session_id"]),
            stage_machine_id=str(task["stage_machine_id"] or ""),
            stage_root=str(task["stage_root"]),
        )

    def _commit_experiment_episode_turn(
        self,
        connection: sqlite3.Connection,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        provider: str,
        machine_id: str,
        native_session_id: str,
        stage_machine_id: str | None,
        stage_root: str,
        chat_id: str,
        operation_id: str,
        invocation: int,
        graph_result: str,
        watcher_ids: list[str],
        context_baseline: dict[str, object],
        ending_signal: dict[str, object] | None = None,
        replace_binding: bool = False,
        replacement_provenance: dict[str, object] | None = None,
    ) -> None:
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
        ending_payload_json: str | None = None
        if ending_signal is not None:
            ending = ending_signal.get("ending")
            if (
                ending_signal.get("episode_id") != episode_id
                or ending not in {"completed", "human_pause"}
                or not isinstance(ending_signal.get("partial"), bool)
                or ending_signal.get("partial") != (ending == "human_pause")
                or not isinstance(ending_signal.get("receipt"), dict)
            ):
                raise ValueError("The Experiment ending receipt is inconsistent with its episode.")
            try:
                ending_payload_json = json.dumps(
                    ending_signal,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("The Experiment ending receipt is not valid JSON.") from exc
            if len(ending_payload_json.encode("utf-8")) > EPISODE_RECEIPT_MAX_BYTES:
                raise ValueError("The compact Experiment ending receipt exceeds its storage limit.")
        now = self.now()
        existing = self._experiment_episode_row(connection, episode_id)
        if (
            existing is None
            or existing["project_id"] != project_id
            or existing["control_node_id"] != control_node_id
        ):
            raise ValueError("This episode id belongs to a different Experiment.")
        turn = connection.execute(
            """
            SELECT project_id, episode_id, request_json
            FROM graph_runs WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        turn_request = json.loads(turn["request_json"]) if turn is not None else None
        if (
            turn is None
            or turn["project_id"] != project_id
            or turn["episode_id"] != episode_id
            or not isinstance(turn_request, dict)
            or turn_request.get("control_node_id") != control_node_id
            or turn_request.get("control_invocation") != invocation
        ):
            raise ValueError("The accepted handoff does not match its exact Experiment task.")
        if existing["native_session_id"] is not None:
            fixed = {
                "machine_id": machine_id,
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
                "stage_machine_id": stage_machine_id or "",
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
            UPDATE experiment_episode_state
            SET provider = ?, machine_id = ?,
                native_session_id = ?, stage_machine_id = ?, stage_root = ?, chat_id = ?,
                last_turn_operation_id = ?, last_turn_invocation = ?,
                last_graph_result = ?, last_watcher_ids_json = ?,
                context_baseline_json = ?, session_diagnostic = NULL, updated_at = ?
            WHERE episode_id = ?
            """,
            (
                provider,
                machine_id,
                native_session_id,
                stage_machine_id,
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
        if ending_payload_json is not None:
            committed_ending = connection.execute(
                """
                SELECT payload_json FROM graph_run_receipts
                WHERE operation_id = ? AND category = 'experiment_loop_exit'
                ORDER BY receipt_id DESC LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if committed_ending is not None:
                if committed_ending["payload_json"] != ending_payload_json:
                    raise ValueError("The Experiment task already has another ending receipt.")
            else:
                self._insert_agent_task_receipt(
                    connection,
                    operation_id,
                    "experiment_loop_exit",
                    ending_payload_json,
                    tier="summary",
                    created_at=now,
                )

    def commit_experiment_episode_turn(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        provider: str,
        machine_id: str,
        native_session_id: str,
        stage_machine_id: str | None,
        stage_root: str,
        chat_id: str,
        operation_id: str,
        invocation: int,
        graph_result: str,
        watcher_ids: list[str],
        context_baseline: dict[str, object],
        ending_signal: dict[str, object] | None = None,
        replace_binding: bool = False,
        replacement_provenance: dict[str, object] | None = None,
    ) -> ExperimentEpisodeRecord:
        """Bind this episode to the session a later automatic wake resumes."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._commit_experiment_episode_turn(
                connection,
                episode_id=episode_id,
                project_id=project_id,
                control_node_id=control_node_id,
                provider=provider,
                machine_id=machine_id,
                native_session_id=native_session_id,
                stage_machine_id=stage_machine_id,
                stage_root=stage_root,
                chat_id=chat_id,
                operation_id=operation_id,
                invocation=invocation,
                graph_result=graph_result,
                watcher_ids=watcher_ids,
                context_baseline=context_baseline,
                ending_signal=ending_signal,
                replace_binding=replace_binding,
                replacement_provenance=replacement_provenance,
            )
        stored = self.experiment_episode(episode_id)
        assert stored is not None
        return stored

    def commit_experiment_episode_handoff(
        self,
        *,
        binding: WatcherBinding,
        episode_id: str,
        control_node_id: str,
        invocation: int,
        provider: str,
        operation_id: str,
        native_session_id: str,
        stage_machine_id: str | None,
        stage_root: str,
        graph_result: str,
        context_baseline: dict[str, object],
        folder_change: WatcherFolderChange | None = None,
        ending_signal: dict[str, object] | None = None,
        replace_binding: bool = False,
        replacement_provenance: dict[str, object] | None = None,
    ) -> tuple[list[WatcherRecord], ExperimentEpisodeRecord]:
        """Atomically apply the turn's watcher folder and commit its episode handoff.

        A handoff that lands after Stop was requested leaves its watchers cancelled.
        """

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored_watchers: list[WatcherRecord] = []
            if folder_change is not None:
                if (
                    folder_change.binding.agent_id != binding.agent_id
                    or folder_change.binding.project_id != binding.project_id
                    or folder_change.binding.origin_turn_id != operation_id
                ):
                    raise ValueError("Watcher folder does not belong to this episode turn.")
                stored_watchers = folder_change.commit(cast("AppStore", self), connection)
            episode = self._experiment_episode_row(connection, episode_id)
            live = [item.watcher_id for item in stored_watchers if item.open]
            if live and episode is not None and episode["stop_requested_at"] is not None:
                cast("AppStore", self).cancel_watchers(
                    live, by="rcp", reason=_STOP_LOOP_CANCEL_REASON, connection=connection
                )
                stored_watchers = _watcher_records(
                    connection,
                    f"w.watcher_id IN ({','.join('?' * len(stored_watchers))})",
                    [item.watcher_id for item in stored_watchers],
                )
            self._commit_experiment_episode_turn(
                connection,
                episode_id=episode_id,
                project_id=binding.project_id,
                control_node_id=control_node_id,
                provider=provider,
                machine_id=binding.machine_id,
                native_session_id=native_session_id,
                stage_machine_id=stage_machine_id,
                stage_root=stage_root,
                chat_id=binding.agent_id,
                operation_id=operation_id,
                invocation=invocation,
                graph_result=graph_result,
                watcher_ids=[item.watcher_id for item in stored_watchers],
                context_baseline=context_baseline,
                ending_signal=ending_signal,
                replace_binding=replace_binding,
                replacement_provenance=replacement_provenance,
            )
        stored_episode = self.experiment_episode(episode_id)
        assert stored_episode is not None
        return stored_watchers, stored_episode

    def record_experiment_episode_diagnostic(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        diagnostic: str | None,
    ) -> None:
        """Persist why an automatic wake could not use this episode's session."""

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode = self._experiment_episode_row(connection, episode_id)
            if (
                episode is None
                or episode["project_id"] != project_id
                or episode["control_node_id"] != control_node_id
            ):
                raise ValueError("This diagnostic belongs to another Experiment episode.")
            connection.execute(
                "UPDATE experiment_episode_state "
                "SET session_diagnostic = ?, updated_at = ? WHERE episode_id = ?",
                (diagnostic, now, episode_id),
            )

    def request_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
        *,
        initiated_by: str | None = None,
        episode_id: str | None = None,
        graph_target: GraphTargetRef | None = None,
    ) -> ExperimentEpisodeRecord | None:
        """Persist a durable stop for one resolved episode before a new claim can win.

        The intent is written under the same write lock a watcher claim takes, so
        a claim that committed first becomes the current turn and anything later
        finds the loop already stopped. A caller supplies an exact episode or
        target so another branch cannot win between validation and mutation.
        """

        with self.connection() as connection:
            selected = self._experiment_episode_for_stop(
                connection,
                project_id,
                control_node_id,
                episode_id=episode_id,
                graph_target=graph_target,
            )
        if selected is None:
            return None
        selected_episode_id, selected_target = selected
        self.request_episode_stop(selected_episode_id, initiated_by=initiated_by)
        return self.settle_experiment_loop_stop(
            project_id,
            control_node_id,
            episode_id=selected_episode_id,
            graph_target=selected_target,
        )

    def settle_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
        *,
        episode_id: str | None = None,
        graph_target: GraphTargetRef | None = None,
    ) -> ExperimentEpisodeRecord | None:
        """Reconcile a persisted stop once its authorized turn is no longer live."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            selected = self._experiment_episode_for_stop(
                connection,
                project_id,
                control_node_id,
                episode_id=episode_id,
                graph_target=graph_target,
            )
            if selected is None:
                return None
            selected_episode_id = selected[0]
            quiescent = self._settle_experiment_loop_stop(
                connection,
                project_id,
                control_node_id,
                selected_episode_id,
            )
            if quiescent:
                self._mark_episode_stop_skipped_in_connection(
                    connection,
                    selected_episode_id,
                    diagnostic=None,
                    now=self.now(),
                )
            stored = self._experiment_episode_row(connection, selected_episode_id)
            return self._experiment_episode_record(stored) if stored is not None else None

    @staticmethod
    def _experiment_stop_unresolved_tasks(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> list[sqlite3.Row]:
        # A superseded attempt does not count: only the newest attempt of each
        # invocation is the turn the human can still act on, which is exactly what
        # `experiment_loop_runtime` reports as `task_active`.
        return connection.execute(
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

    def _settle_experiment_loop_stop(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> bool:
        """Terminalize this episode's observers once its authorized turn is no longer live.

        Stop waits only for a turn that is queued, running, or pausing, or one a
        restart interrupted while Stop waited on it that startup recovery has not
        ruled on yet. Any other turn that already paused, failed, or was
        interrupted has nothing in flight to finish, and nobody resumes a turn they
        asked to stop, so Stop abandons its recovery (history stays inspectable)
        and settles. A claimed watcher keeps its notification provenance, but
        becomes stopped once the task it woke has finished successfully.
        """

        requested = self._experiment_episode_row(connection, episode_id)
        if (
            requested is None
            or requested["project_id"] != project_id
            or requested["control_node_id"] != control_node_id
            or requested["stop_requested_at"] is None
        ):
            return False
        unresolved = self._experiment_stop_unresolved_tasks(
            connection, project_id, control_node_id, episode_id
        )
        if unresolved:
            pending = {
                str(row["operation_id"])
                for row in self._stopping_experiment_recovery_rows(connection, episode_id)
            }
            if any(
                row["status"] not in {"paused", "failed", "interrupted"}
                or str(row["operation_id"]) in pending
                for row in unresolved
            ):
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
                    "Stop loop abandoned recovery of this stopped task. The task and all "
                    "history remain inspectable."
                )
                self._insert_agent_task_receipt(
                    connection,
                    str(row["operation_id"]),
                    "experiment_recovery_abandoned",
                    self._bounded_receipt_payload({"episode_id": episode_id, "reason": "stopped"}),
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
        agent_id = requested["chat_id"] or (root_request or {}).get("chat_id")
        # A later reconciliation of a historical Stop never sweeps the watchers
        # of a continuation that now runs on the same chat.
        continued = connection.execute(
            "SELECT 1 FROM episodes WHERE continues_episode_id = ? LIMIT 1", (episode_id,)
        ).fetchone()
        if isinstance(agent_id, str) and agent_id and continued is None:
            cast("AppStore", self)._cancel_where(
                "agent_id = ?",
                (agent_id,),
                by="rcp",
                reason=_STOP_LOOP_CANCEL_REASON,
                connection=connection,
            )
        return True

    def settle_ready_experiment_loop_stops(self) -> int:
        """Reconcile every durable stop that no longer has a recoverable turn."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT episode_id, project_id, control_node_id, graph_target_json
                FROM episodes
                WHERE mode = 'experiment_loop'
                  AND stop_requested_at IS NOT NULL AND stop_settled_at IS NULL
                ORDER BY created_at, episode_id
                """
            ).fetchall()
        settled = 0
        for row in rows:
            before = self.episode(str(row["episode_id"]))
            self.settle_experiment_loop_stop(
                str(row["project_id"]),
                str(row["control_node_id"]),
                episode_id=str(row["episode_id"]),
                graph_target=GraphTargetRef.model_validate_json(row["graph_target_json"]),
            )
            after = self.episode(str(row["episode_id"]))
            if before is not None and before.stop_settled_at is None and after is not None:
                settled += int(after.stop_settled_at is not None)
        return settled

    def stopping_experiment_recovery_candidates(self) -> list[AgentTaskRecord]:
        """Return each durable Stop's unresolved leaf that may finish in recovery.

        These are already-authorized turns, not new Experiment invocations.  The
        background adapter still proves the exact native session and stage before
        it creates a recovery child; this query only identifies the leaves whose
        Stop would otherwise remain unresolved after a process restart.
        """

        with self.connection() as connection:
            rows = self._stopping_experiment_recovery_rows(connection)
        return [self._agent_task_record(row) for row in rows]

    @staticmethod
    def _stopping_experiment_recovery_rows(
        connection: sqlite3.Connection, episode_id: str | None = None
    ) -> list[sqlite3.Row]:
        """Each unresolved leaf a restart interrupted behind its Stop, optionally for one episode."""

        return connection.execute(
            """
                SELECT task.*,
                       EXISTS (
                           SELECT 1 FROM graph_run_receipts AS receipt
                           WHERE receipt.operation_id = task.operation_id
                             AND receipt.category IN (
                                 'experiment_recovery_abandoned',
                                 'auto_research_recovery_abandoned'
                             )
                       ) AS recovery_abandoned
                FROM episodes AS episode
                JOIN experiment_episode_state AS state
                  ON state.episode_id = episode.episode_id
                JOIN graph_runs AS task
                  ON task.episode_id = episode.episode_id
                WHERE episode.mode = 'experiment_loop'
                  AND episode.stop_requested_at IS NOT NULL
                  AND episode.stop_settled_at IS NULL
                  AND episode.ending IS NULL
                  AND state.session_diagnostic IS NULL
                  AND task.visible = 1
                  -- Only a turn the restart interrupted after Stop was still live
                  -- then; one already paused or failed settles instead (Stop). Both
                  -- stamps are AppStoreBase.now() UTC ISO text, which orders losslessly;
                  -- julianday() would round microseconds away.
                  AND task.status = 'interrupted'
                  AND task.finished_at >= episode.stop_requested_at
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_runs AS child
                      WHERE child.parent_operation_id = task.operation_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_run_receipts AS receipt
                      WHERE receipt.operation_id = task.operation_id
                        AND receipt.category = 'experiment_recovery_abandoned'
                  )
                  AND (? IS NULL OR episode.episode_id = ?)
                ORDER BY episode.created_at, episode.episode_id,
                         task.created_at, task.operation_id
                """,
            (episode_id, episode_id),
        ).fetchall()

    @staticmethod
    def _experiment_episode_for_stop(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        *,
        episode_id: str | None = None,
        graph_target: GraphTargetRef | None = None,
    ) -> tuple[str, GraphTargetRef] | None:
        if episode_id is None and graph_target is None:
            raise ValueError("Stop requires an exact episode or graph target.")
        row = connection.execute(
            """
            SELECT episode_id, graph_target_json FROM episodes
            WHERE project_id = ? AND mode = 'experiment_loop' AND control_node_id = ?
              AND (? IS NULL OR episode_id = ?)
              AND (
                  ? = 0
                  OR (
                      json_extract(graph_target_json, '$.kind') = ?
                      AND json_extract(graph_target_json, '$.branch_id') IS ?
                  )
              )
            ORDER BY created_at DESC, episode_id DESC
            LIMIT 1
            """,
            (
                project_id,
                control_node_id,
                episode_id,
                episode_id,
                int(graph_target is not None),
                graph_target.kind if graph_target is not None else "main",
                graph_target.branch_id if graph_target is not None else None,
            ),
        ).fetchone()
        if row is None or not isinstance(row["episode_id"], str):
            return None
        return row["episode_id"], GraphTargetRef.model_validate_json(row["graph_target_json"])

    def experiment_loop_runtime(
        self,
        project_id: str,
        control_node_id: str,
        *,
        graph_target: GraphTargetRef,
    ) -> ExperimentLoopRuntime:
        """Derive the newest episode on one exact target."""

        return self.experiment_loop_runtimes(
            project_id, [control_node_id], graph_target=graph_target
        )[control_node_id]

    def experiment_loop_runtimes(
        self,
        project_id: str,
        control_node_ids: Iterable[str],
        *,
        graph_target: GraphTargetRef,
    ) -> dict[str, ExperimentLoopRuntime]:
        """Derive current runtimes within one graph target."""

        requested = tuple(dict.fromkeys(control_node_ids))
        if not requested:
            return {}
        with self.connection() as connection:
            connection.execute("BEGIN")
            return self._experiment_loop_runtimes_in_connection(
                connection, project_id, requested, graph_target=graph_target
            )

    def experiment_control_projection_snapshots(
        self,
        project_id: str,
        control_node_ids: Iterable[str] | None = None,
        *,
        graph_target: GraphTargetRef,
    ) -> dict[str, ExperimentControlProjectionSnapshot]:
        """Read target-local control inputs from one coherent SQLite snapshot."""

        requested = None if control_node_ids is None else set(control_node_ids)
        if requested == set():
            return {}
        with self.connection() as connection:
            connection.execute("BEGIN")
            runtimes = self._project_experiment_loop_runtimes(
                project_id, requested, graph_target=graph_target, _connection=connection
            )
            return {
                node_id: self._experiment_control_projection_snapshot_in_connection(
                    connection, project_id, node_id, runtime
                )
                for (node_id, _target_key), runtime in runtimes.items()
            }

    def project_experiment_control_projection_snapshots(
        self, project_id: str
    ) -> dict[tuple[str, str], ExperimentControlProjectionSnapshot]:
        """Read every current (node, target) without collapsing branch identities."""

        with self.connection() as connection:
            connection.execute("BEGIN")
            runtimes = self._project_experiment_loop_runtimes(
                project_id, None, _connection=connection
            )
            return {
                key: self._experiment_control_projection_snapshot_in_connection(
                    connection, project_id, key[0], runtime
                )
                for key, runtime in runtimes.items()
            }

    def _experiment_control_projection_snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        runtime: ExperimentLoopRuntime,
    ) -> ExperimentControlProjectionSnapshot:
        episode_snapshot = (
            self._experiment_episode_projection_snapshot_in_connection(
                connection, project_id, control_node_id, runtime.episode_id
            )
            if runtime.episode_id is not None
            else None
        )
        return ExperimentControlProjectionSnapshot(
            runtime=runtime,
            episode=episode_snapshot,
            latest_report_episode_id=(
                self._latest_experiment_report_episode_id_in_connection(
                    connection, project_id, control_node_id, episode_snapshot.episode.graph_target
                )
                if episode_snapshot is not None
                else None
            ),
        )

    @staticmethod
    def _latest_experiment_report_episode_id_in_connection(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        graph_target: GraphTargetRef,
    ) -> str | None:
        row = connection.execute(
            f"""
            SELECT episodes.episode_id
            FROM episodes
            JOIN {_VISIBLE_EPISODE_REPORTS} AS episode_reports
              ON episode_reports.episode_id = episodes.episode_id
            WHERE episodes.project_id = ?
              AND episodes.mode = 'experiment_loop'
              AND episodes.control_node_id = ?
              AND episodes.graph_target_json = ?
            ORDER BY episode_reports.created_at DESC, episodes.episode_id DESC
            LIMIT 1
            """,
            (project_id, control_node_id, graph_target_json(graph_target)),
        ).fetchone()
        return str(row["episode_id"]) if row is not None else None

    def _experiment_loop_runtimes_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        requested: tuple[str, ...],
        *,
        graph_target: GraphTargetRef,
    ) -> dict[str, ExperimentLoopRuntime]:
        projected = self._project_experiment_loop_runtimes(
            project_id, set(requested), graph_target=graph_target, _connection=connection
        )
        return {
            node_id: projected.get((node_id, graph_target.key), ExperimentLoopRuntime())
            for node_id in requested
        }

    def _experiment_episode_projection_snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> ExperimentEpisodeProjectionSnapshot:
        episode_row = connection.execute(
            "SELECT * FROM episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode_row is None:
            raise ValueError("Experiment runtime identifies a missing durable episode.")
        episode = self._episode_record(episode_row)
        if (
            episode.project_id != project_id
            or episode.mode != "experiment_loop"
            or episode.control_node_id != control_node_id
        ):
            raise ValueError("Experiment runtime does not identify its exact durable episode.")
        task_rows = connection.execute(
            """
            SELECT graph_runs.*,
                   EXISTS (
                       SELECT 1 FROM graph_run_receipts AS receipt
                       WHERE receipt.operation_id = graph_runs.operation_id
                         AND receipt.category IN (
                             'experiment_recovery_abandoned',
                             'auto_research_recovery_abandoned'
                         )
                   ) AS recovery_abandoned
            FROM graph_runs
            WHERE episode_id = ? AND visible = 1 AND kind != 'episode_report'
            ORDER BY created_at, operation_id
            """,
            (episode_id,),
        ).fetchall()
        report_row = connection.execute(
            f"SELECT * FROM {_VISIBLE_EPISODE_REPORTS} WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        usage = connection.execute(
            """
            SELECT
                COALESCE(SUM(agent_usage.processed_input_tokens), 0) AS input_tokens,
                COALESCE(SUM(agent_usage.generated_tokens), 0) AS generated_tokens
            FROM agent_usage
            JOIN graph_runs ON graph_runs.operation_id = agent_usage.operation_id
            WHERE graph_runs.episode_id = ?
              AND graph_runs.kind != 'episode_report'
              AND agent_usage.counted = 1
            """,
            (episode_id,),
        ).fetchone()
        assert usage is not None
        return ExperimentEpisodeProjectionSnapshot(
            episode=episode,
            tasks=[self._agent_task_record(row) for row in task_rows],
            budget=EpisodeBudgetMeter(
                invocation_ceiling=episode.invocation_ceiling,
                invocations_used=episode.invocations_used,
                invocations_remaining=episode.invocations_remaining,
                observed_input_tokens=int(usage["input_tokens"]),
                observed_generated_tokens=int(usage["generated_tokens"]),
            ),
            report=(self._episode_report_record(report_row) if report_row is not None else None),
        )

    def _project_experiment_loop_runtimes(
        self,
        project_id: str,
        requested: set[str] | None,
        *,
        graph_target: GraphTargetRef | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[tuple[str, str], ExperimentLoopRuntime]:
        """Load the generic parents plus mode ledgers and group them in memory."""

        if _connection is None:
            with self.connection() as connection:
                connection.execute("BEGIN")
                return self._project_experiment_loop_runtimes(
                    project_id,
                    requested,
                    graph_target=graph_target,
                    _connection=connection,
                )
        task_rows = _connection.execute(
            """
                SELECT operation_id, parent_operation_id, status, attempt, request_json,
                       created_at, phase, status_message, failure_kind, last_activity_at,
                       rowid AS storage_rowid
                FROM graph_runs
                WHERE project_id = ?
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                """,
            (project_id,),
        ).fetchall()
        active_sessions = {
            (row["provider"], row["stage_machine_id"], row["native_session_id"])
            for row in _connection.execute(
                """
                SELECT json_extract(request_json, '$.provider') AS provider,
                       COALESCE(stage_machine_id, '') AS stage_machine_id, native_session_id
                FROM graph_runs
                WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                  AND native_session_id IS NOT NULL
                """,
                (project_id,),
            )
        }
        receipt_rows = _connection.execute(
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
        watchers_by_agent: dict[str, list[WatcherRecord]] = {}
        for watcher in _watcher_records(
            _connection, f"w.project_id = ? AND {_OPEN}", (project_id,)
        ):
            watchers_by_agent.setdefault(watcher.agent_id, []).append(watcher)
        episode_rows = _connection.execute(
            """
                SELECT state.*, episode.project_id, episode.control_node_id,
                       episode.graph_target_json, episode.browser_requested,
                       episode.stop_requested_at, episode.stop_settled_at
                FROM experiment_episode_state AS state
                JOIN episodes AS episode ON episode.episode_id = state.episode_id
                WHERE episode.project_id = ? AND episode.mode = 'experiment_loop'
                """,
            (project_id,),
        ).fetchall()
        parent_rows = _connection.execute(
            """
                SELECT * FROM episodes
                WHERE project_id = ? AND mode = 'experiment_loop'
                ORDER BY created_at DESC, episode_id DESC
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

        receipt_categories: dict[str, set[str]] = {}
        for row in receipt_rows:
            receipt_categories.setdefault(str(row["operation_id"]), set()).add(str(row["category"]))
        episodes = {
            str(row["episode_id"]): self._experiment_episode_record(row) for row in episode_rows
        }
        parents_by_control: dict[tuple[str, str], EpisodeRecord] = {}
        newest_nodes: set[str] = set()
        completed_since = datetime.fromisoformat(self.now()) - SPACE_RUNS_COMPLETED_TTL
        for row in parent_rows:
            parent = self._episode_record(row)
            assert parent.control_node_id is not None
            if graph_target is not None and parent.graph_target != graph_target:
                continue
            if requested is not None and parent.control_node_id not in requested:
                continue
            newest_on_node = parent.control_node_id not in newest_nodes
            newest_nodes.add(parent.control_node_id)
            if (
                graph_target is None
                and parent.status not in _LIVE_EPISODE_STATUSES
                and not newest_on_node
                and (
                    parent.ended_at is None
                    or datetime.fromisoformat(parent.ended_at) < completed_since
                )
            ):
                continue
            parents_by_control.setdefault((parent.control_node_id, parent.graph_target.key), parent)
        control_keys = set(parents_by_control)
        if requested is not None and graph_target is not None:
            control_keys.update((node_id, graph_target.key) for node_id in requested)
        projected: dict[tuple[str, str], ExperimentLoopRuntime] = {}
        wake_diagnostics = self._auto_research_experiment_wake_diagnostics(_connection, project_id)
        for control_key in control_keys:
            control_node_id = control_key[0]
            parent = parents_by_control.get(control_key)
            try:
                projected[control_key] = self._derive_experiment_loop_runtime(
                    tasks_by_control.get(control_node_id, []),
                    watchers_by_agent,
                    receipt_categories,
                    episodes,
                    parent,
                )
                binding = episodes.get(parent.episode_id) if parent is not None else None
                if binding is not None and binding.session_bound:
                    projected[control_key] = projected[control_key].model_copy(
                        update={
                            "session_active": (
                                binding.provider,
                                binding.stage_machine_id or "",
                                binding.native_session_id,
                            )
                            in active_sessions,
                        }
                    )
                if parent is not None and parent.episode_id in wake_diagnostics:
                    projected[control_key] = projected[control_key].model_copy(
                        update={
                            "watcher_delivery_diagnostic": wake_diagnostics[parent.episode_id],
                        }
                    )
            except ValueError as exc:
                # A malformed durable ledger is a per-control degraded fact. It
                # must remain visible without hiding healthy sibling controls;
                # SQLite/IO failures still escape and fail the whole snapshot.
                if parent is None:
                    raise
                diagnostic = f"Stored Experiment runtime is inconsistent: {exc}"
                projected[control_key] = ExperimentLoopRuntime(
                    episode_id=parent.episode_id,
                    invocations_used=max(parent.invocations_used, 1),
                    invocation_ceiling=parent.invocation_ceiling,
                    control_revision=0,
                    episode_live=parent.status in _LIVE_EPISODE_STATUSES,
                    watcher_degraded=True,
                    decision_bundle=[],
                    completion_criteria=[],
                    stop_requested=parent.stop_requested_at is not None,
                    stop_settled=parent.stop_settled_at is not None,
                    session_diagnostic=diagnostic[:2_000],
                    projection_diagnostic=diagnostic[:2_000],
                )
        return projected

    @classmethod
    def _derive_experiment_loop_runtime(
        cls,
        task_entries: list[tuple[sqlite3.Row, dict[str, object]]],
        watchers_by_agent: dict[str, list[WatcherRecord]],
        receipt_categories: dict[str, set[str]],
        episodes: dict[str, ExperimentEpisodeRecord],
        parent: EpisodeRecord | None,
    ) -> ExperimentLoopRuntime:
        """Purely derive one runtime from an already-loaded project ledger."""

        if parent is None:
            return ExperimentLoopRuntime()
        root_entries = [
            entry
            for entry in task_entries
            if entry[0]["parent_operation_id"] is None
            and entry[1].get("control_episode_id") == parent.episode_id
        ]
        if not root_entries:
            raise ValueError("Stored Experiment episode is missing its paid root task.")
        _, root_request = max(
            root_entries,
            key=lambda entry: (entry[0]["created_at"], entry[0]["storage_rowid"]),
        )
        episode_id = root_request.get("control_episode_id")
        if not isinstance(episode_id, str) or episode_id != parent.episode_id:
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
        agent_id = root_request.get("chat_id")
        watchers = watchers_by_agent.get(agent_id, []) if isinstance(agent_id, str) else []
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
        invocations_used = parent.invocations_used
        if set(latest_by_invocation) != set(range(1, invocations_used + 1)):
            raise ValueError("Stored experiment-loop root invocations are out of sequence.")
        if ceiling != parent.invocation_ceiling:
            raise ValueError("Stored experiment-loop root changed its parent invocation ceiling.")
        unresolved = any(
            row["status"] in {"queued", "running", "pausing", "paused", "failed", "interrupted"}
            and "experiment_recovery_abandoned"
            not in receipt_categories.get(str(row["operation_id"]), set())
            for row, _request in latest_by_invocation.values()
        )
        detached_work_active = any(not watcher.ready for watcher in watchers)
        watcher_degraded = any(watcher.state == "check_failing" for watcher in watchers)
        watcher_completion_pending = any(watcher.ready for watcher in watchers)
        has_watcher = detached_work_active or watcher_completion_pending
        episode_exited = parent.ending is not None or any(
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
            episode_live=parent.status in _LIVE_EPISODE_STATUSES,
            active=parent.status in {"running", "stopping"}
            and (
                unresolved
                or (
                    has_watcher
                    and not at_ceiling
                    and not episode_exited
                    and parent.stop_requested_at is None
                )
            ),
            paused=parent.status == "running"
            and at_ceiling
            and not unresolved
            and not episode_exited
            and parent.stop_requested_at is None,
            decision_bundle=pins,
            completion_criteria=completion_criteria,
            stop_requested=parent.stop_requested_at is not None,
            stop_settled=parent.stop_settled_at is not None,
            browser_requested=episode.browser_requested if episode else False,
            session_bound=episode is not None and episode.session_bound,
            session_diagnostic=episode.session_diagnostic if episode else None,
            provider=(episode.provider if episode is not None else None)
            or _optional_str(binding_request.get("provider")),
            model=(
                binding_request["model"] if isinstance(binding_request.get("model"), str) else None
            ),
            reasoning=_optional_str(binding_request.get("reasoning")),
            workflow_ids=binding_request.get("workflow_ids") or [],
            skill_ids=binding_request.get("skill_ids") or [],
            resolved_skill_packages=binding_request.get("resolved_skill_packages") or [],
            run_on=(episode.machine_id if episode is not None else None)
            or _optional_str(binding_request.get("run_on")),
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
            current_failure_kind=current_row["failure_kind"],
            current_last_activity_at=current_row["last_activity_at"],
            current_invocation=(
                current_invocation if isinstance(current_invocation, int) else None
            ),
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

    def active_experiment_control_ids(
        self,
        project_id: str,
        *,
        graph_target: GraphTargetRef,
    ) -> set[str]:
        """Return controls live on the exact target."""

        projected = self._project_experiment_loop_runtimes(
            project_id, None, graph_target=graph_target
        )
        return {node_id for (node_id, _target_key), runtime in projected.items() if runtime.active}
