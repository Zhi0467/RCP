from __future__ import annotations

from typing import Literal

from rcp.core.models import ConversationWorktreeBinding


class ConversationWorktreeStoreMixin:
    """One retained worktree binding per conversation, including after removal."""

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
