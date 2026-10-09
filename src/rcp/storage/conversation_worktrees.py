from __future__ import annotations

import sqlite3
from typing import Literal

from rcp.core.models import (
    ConversationWorktreeBinding,
    EpisodeIsolation,
    EpisodeIsolationState,
    EpisodeMergeAttempt,
    EpisodeUnfinishedJob,
)
from rcp.storage.mixin_base import StoreMixinBase


class UnfinishedEpisodeJobs(ValueError):
    """Merge pauses here: the human confirms these jobs before it goes ahead."""

    code = "unfinished_jobs_confirmation_required"

    def __init__(self, jobs: list[EpisodeUnfinishedJob]) -> None:
        super().__init__(self.code)
        self.jobs = jobs


def _unconfirmed(
    jobs: list[EpisodeUnfinishedJob], confirmed: list[EpisodeUnfinishedJob]
) -> list[EpisodeUnfinishedJob]:
    known = {(job.kind, job.id) for job in confirmed}
    return [job for job in jobs if (job.kind, job.id) not in known]


class ConversationWorktreeStoreMixin(StoreMixinBase):
    """One retained worktree binding per conversation, including after removal."""

    def episode_isolation(self, project_id: str, owner_episode_id: str) -> EpisodeIsolation | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT binding_json FROM episode_isolations WHERE project_id = ? AND owner_episode_id = ?",
                (project_id, owner_episode_id),
            ).fetchone()
        return None if row is None else EpisodeIsolation.model_validate_json(row[0])

    def episode_isolation_for_branch(
        self, project_id: str, branch_id: str
    ) -> EpisodeIsolation | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT binding_json FROM episode_isolations WHERE project_id = ? AND graph_branch_id = ?",
                (project_id, branch_id),
            ).fetchone()
        return None if row is None else EpisodeIsolation.model_validate_json(row[0])

    def create_episode_isolation(
        self, project_id: str, binding: EpisodeIsolation
    ) -> EpisodeIsolation:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT binding_json FROM episode_isolations WHERE project_id = ? AND owner_episode_id = ?",
                (project_id, binding.owner_episode_id),
            ).fetchone()
            if row is not None:
                saved = EpisodeIsolation.model_validate_json(row[0])
                if saved != binding:
                    raise ValueError("episode isolation identity cannot be replaced")
                return saved
            connection.execute(
                "INSERT INTO episode_isolations VALUES (?, ?, ?, ?)",
                (
                    project_id,
                    binding.owner_episode_id,
                    binding.graph_branch_id,
                    binding.model_dump_json(),
                ),
            )
            state = EpisodeIsolationState(owner_episode_id=binding.owner_episode_id)
            connection.execute(
                "INSERT INTO episode_isolation_states VALUES (?, ?, ?)",
                (project_id, binding.owner_episode_id, state.model_dump_json()),
            )
        return binding

    def episode_isolation_state(
        self, project_id: str, owner_episode_id: str
    ) -> EpisodeIsolationState | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM episode_isolation_states WHERE project_id = ? AND owner_episode_id = ?",
                (project_id, owner_episode_id),
            ).fetchone()
        return None if row is None else EpisodeIsolationState.model_validate_json(row[0])

    def episode_isolation_states(self, project_id: str) -> dict[str, EpisodeIsolationState]:
        """Every isolation state of one project, keyed by owner episode, in one read."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT owner_episode_id, state_json FROM episode_isolation_states WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {str(row[0]): EpisodeIsolationState.model_validate_json(row[1]) for row in rows}

    def set_episode_isolation_status(
        self, project_id: str, owner_episode_id: str, *, expected_status: str, status: str
    ) -> EpisodeIsolationState:
        # The owner admission lock precedes this short SQLite write transaction.
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state_json FROM episode_isolation_states WHERE project_id = ? AND owner_episode_id = ?",
                (project_id, owner_episode_id),
            ).fetchone()
            if row is None:
                raise ValueError("episode isolation state is unavailable")
            saved = EpisodeIsolationState.model_validate_json(row[0])
            transitions = {
                "creating": {"ready"},
                "ready": {"merging", "removing"},
                "merging": {"ready", "removing"},
                "removing": {"ready", "removed"},
                "removed": set(),
            }
            if saved.status != expected_status or status not in transitions[saved.status]:
                raise ValueError(
                    "episode isolation operation state changed or transition is invalid"
                )
            updated = saved.model_copy(update={"status": status})
            connection.execute(
                "UPDATE episode_isolation_states SET state_json = ? WHERE project_id = ? AND owner_episode_id = ?",
                (updated.model_dump_json(), project_id, owner_episode_id),
            )
        return updated

    @staticmethod
    def require_episode_binding_admission_open(
        connection: sqlite3.Connection,
        project_id: str,
        *,
        episode_id: str | None = None,
        owner_episode_id: str | None = None,
        branch_id: str | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT s.state_json FROM episode_isolation_states s "
            "JOIN episode_isolations i USING (project_id, owner_episode_id) "
            "WHERE s.project_id = ? AND (s.owner_episode_id = ? OR "
            "s.owner_episode_id = (SELECT COALESCE(isolation_owner_episode_id, episode_id) "
            "FROM episodes WHERE episode_id = ?) OR i.graph_branch_id = ?)",
            (project_id, owner_episode_id, episode_id, branch_id),
        ).fetchone()
        if row is not None:
            state = EpisodeIsolationState.model_validate_json(row[0])
            if state.merge_reservation is not None or state.status == "merging":
                raise ValueError("episode_merge_reserved")
            if state.status in {"removing", "removed"}:
                raise ValueError("episode_isolation_unavailable")

    def episode_binding_live_tasks(self, project_id: str, owner_episode_id: str) -> list[str]:
        with self.connection() as connection:
            return self._episode_binding_live_tasks(connection, project_id, owner_episode_id)

    def require_episode_binding_quiescent(
        self,
        project_id: str,
        owner_episode_id: str,
        *,
        confirmed_jobs: list[EpisodeUnfinishedJob] | None = None,
    ) -> None:
        """Live turns refuse; jobs refuse unless the human already merged over them."""
        with self.connection() as connection:
            if self._episode_binding_live_tasks(connection, project_id, owner_episode_id):
                raise ValueError("episode_binding_live_turns")
            jobs = self._episode_binding_unfinished_jobs(connection, project_id, owner_episode_id)
            if unconfirmed := _unconfirmed(jobs, confirmed_jobs or []):
                raise UnfinishedEpisodeJobs(unconfirmed)

    @staticmethod
    def _episode_binding_live_tasks(connection, project_id, owner_episode_id) -> list[str]:
        rows = connection.execute(
            "SELECT g.operation_id FROM graph_runs g LEFT JOIN episodes e "
            "ON e.episode_id = g.episode_id WHERE g.project_id = ? "
            "AND g.status IN ('queued', 'running', 'pausing') AND g.kind != 'branch_merge' "
            "AND (COALESCE(e.isolation_owner_episode_id, e.episode_id) = ? OR "
            "json_extract(g.graph_target_json, '$.branch_id') = "
            "(SELECT graph_branch_id FROM episode_isolations "
            "WHERE project_id = ? AND owner_episode_id = ?))",
            (project_id, owner_episode_id, project_id, owner_episode_id),
        ).fetchall()
        return [row[0] for row in rows]

    def reserve_episode_merge(
        self,
        project_id: str,
        owner_episode_id: str,
        attempt: EpisodeMergeAttempt,
        *,
        confirm_unfinished_jobs: bool = False,
    ) -> EpisodeIsolationState:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state_json FROM episode_isolation_states WHERE project_id = ? AND owner_episode_id = ?",
                (project_id, owner_episode_id),
            ).fetchone()
            if row is None:
                raise ValueError("episode_isolation_unavailable")
            state = EpisodeIsolationState.model_validate_json(row[0])
            if state.merge_reservation is not None:
                raise ValueError("episode_merge_reserved")
            if state.status not in {"ready", "removed"}:
                raise ValueError("episode_isolation_unavailable")
            active_merge = connection.execute(
                "SELECT 1 FROM graph_runs g WHERE g.project_id = ? AND g.kind = 'branch_merge' "
                "AND g.status IN ('queued', 'running', 'pausing') AND (g.episode_id = ? OR "
                "json_extract(g.graph_target_json, '$.branch_id') = "
                "(SELECT graph_branch_id FROM episode_isolations "
                "WHERE project_id = ? AND owner_episode_id = ?)) LIMIT 1",
                (project_id, owner_episode_id, project_id, owner_episode_id),
            ).fetchone()
            if active_merge is not None:
                raise ValueError("episode_merge_reserved")
            if self._episode_binding_live_tasks(connection, project_id, owner_episode_id):
                raise ValueError("episode_binding_live_turns")
            # Unfinished jobs pause Merge for the human; once confirmed they ride on the
            # attempt, and the code merge agent stops them before it merges.
            jobs = self._episode_binding_unfinished_jobs(connection, project_id, owner_episode_id)
            if jobs and not confirm_unfinished_jobs:
                raise UnfinishedEpisodeJobs(jobs)
            attempt = attempt.model_copy(update={"unfinished_jobs": jobs})
            state = state.model_copy(
                update={
                    # A removed worktree stays removed; only its graph side can merge.
                    "status": "removed" if state.status == "removed" else "merging",
                    "merge_reservation": attempt.attempt_id,
                    "merge_attempt": attempt,
                }
            )
            self._write_episode_isolation_state(connection, project_id, state)
        return state

    def update_episode_merge_attempt(
        self,
        project_id: str,
        owner_episode_id: str,
        *,
        expected_attempt_id: str,
        attempt: EpisodeMergeAttempt,
        **state_updates,
    ) -> EpisodeIsolationState:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state_json FROM episode_isolation_states WHERE project_id = ? AND owner_episode_id = ?",
                (project_id, owner_episode_id),
            ).fetchone()
            if row is None:
                raise ValueError("episode_isolation_unavailable")
            state = EpisodeIsolationState.model_validate_json(row[0])
            if (
                state.merge_reservation != expected_attempt_id
                or attempt.attempt_id != expected_attempt_id
            ):
                raise ValueError("episode_merge_reservation_changed")
            allowed = {
                "status",
                "merge_reservation",
                "delivered_source_commit",
                "delivered_target_branch",
                "delivered_target_commit",
                "squash_commit",
                "graph_archived",
            }
            if state_updates.keys() - allowed:
                raise ValueError("episode_merge_state_update_invalid")
            state = EpisodeIsolationState.model_validate(
                {**state.model_dump(), **state_updates, "merge_attempt": attempt.model_dump()}
            )
            self._write_episode_isolation_state(connection, project_id, state)
        return state

    def finish_episode_merge(
        self,
        project_id: str,
        owner_episode_id: str,
        *,
        expected_attempt_id: str,
        attempt: EpisodeMergeAttempt | None = None,
        **state_updates,
    ) -> EpisodeIsolationState:
        saved = self.episode_isolation_state(project_id, owner_episode_id)
        if attempt is None:
            if saved is None or saved.merge_attempt is None:
                raise ValueError("episode_isolation_unavailable")
            attempt = saved.merge_attempt.model_copy(update={"phase": "done"})
        # A worktree removed earlier stays removed after a later graph-only merge.
        removed = "remove_worktree" in attempt.cleanup_completed or (
            saved is not None and saved.status == "removed"
        )
        return self.update_episode_merge_attempt(
            project_id,
            owner_episode_id,
            expected_attempt_id=expected_attempt_id,
            attempt=attempt,
            merge_reservation=None,
            status="removed" if removed else "ready",
            **state_updates,
        )

    @staticmethod
    def _write_episode_isolation_state(connection, project_id, state) -> None:
        connection.execute(
            "UPDATE episode_isolation_states SET state_json = ? WHERE project_id = ? AND owner_episode_id = ?",
            (state.model_dump_json(), project_id, state.owner_episode_id),
        )

    def conversation_worktree(
        self, project_id: str, chat_id: str
    ) -> ConversationWorktreeBinding | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT binding_json FROM conversation_worktrees WHERE project_id = ? AND chat_id = ?",
                (project_id, chat_id),
            ).fetchone()
        return None if row is None else ConversationWorktreeBinding.model_validate_json(row[0])

    def create_conversation_worktree(
        self, binding: ConversationWorktreeBinding
    ) -> ConversationWorktreeBinding:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT binding_json FROM conversation_worktrees WHERE project_id = ? AND chat_id = ?",
                (binding.project_id, binding.chat_id),
            ).fetchone()
            if row is not None:
                saved = ConversationWorktreeBinding.model_validate_json(row[0])
                if saved != binding:
                    raise ValueError("conversation worktree binding cannot be replaced")
                return saved
            if binding.status != "creating":
                raise ValueError("a new conversation worktree binding must be creating")
            connection.execute(
                "INSERT INTO conversation_worktrees(project_id, chat_id, binding_json) VALUES (?, ?, ?)",
                (binding.project_id, binding.chat_id, binding.model_dump_json()),
            )
        return binding

    def set_conversation_worktree_status(
        self,
        project_id: str,
        chat_id: str,
        expected_status: Literal["creating", "ready", "removing", "removed"],
        status: Literal["creating", "ready", "removing", "removed"],
    ) -> ConversationWorktreeBinding:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT binding_json FROM conversation_worktrees WHERE project_id = ? AND chat_id = ?",
                (project_id, chat_id),
            ).fetchone()
            if row is None:
                raise ValueError("conversation worktree binding does not exist")
            saved = ConversationWorktreeBinding.model_validate_json(row[0])
            if saved.status != expected_status:
                raise ValueError("conversation worktree status changed concurrently")
            transitions = {
                "creating": {"ready"},
                "ready": {"removing"},
                "removing": {"ready", "removed"},
                "removed": set(),
            }
            if status not in transitions[saved.status]:
                raise ValueError("invalid conversation worktree status transition")
            updated = ConversationWorktreeBinding.model_validate(
                {**saved.model_dump(), "status": status}
            )
            connection.execute(
                "UPDATE conversation_worktrees SET binding_json = ? WHERE project_id = ? AND chat_id = ?",
                (updated.model_dump_json(), project_id, chat_id),
            )
        return updated

    def chat_has_work_turn(self, project_id: str, chat_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND kind IN ('node_chat', 'project_chat')
                  AND json_extract(request_json, '$.chat_id') = ?
                  AND json_extract(request_json, '$.mode') = 'work'
                LIMIT 1
                """,
                (project_id, chat_id),
            ).fetchone()
        return row is not None
