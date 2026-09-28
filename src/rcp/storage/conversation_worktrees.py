from __future__ import annotations

from typing import Literal

from rcp.core.models import ConversationWorktreeBinding, EpisodeIsolation, EpisodeIsolationState


class ConversationWorktreeStoreMixin:
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
            updated = EpisodeIsolationState(owner_episode_id=owner_episode_id, status=status)
            connection.execute(
                "UPDATE episode_isolation_states SET state_json = ? WHERE project_id = ? AND owner_episode_id = ?",
                (updated.model_dump_json(), project_id, owner_episode_id),
            )
        return updated

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
