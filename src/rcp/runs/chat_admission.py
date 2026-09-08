"""Admission for fresh human turns in an ordinary graph-bound conversation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rcp.conversation_worktrees import admit_conversation_worktree, conversation_worktree_locks
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskAdmissionConflict, AppStore


def require_chat_graph_target(
    service: ProjectService, store: AppStore, project_id: str, chat_id: str
) -> None:
    target = store.chat_graph_target(project_id, chat_id)
    if target is not None and target != service.history.graph_target:
        raise ValueError(
            "This conversation belongs to another graph target and cannot continue here."
        )


@contextmanager
def admit_fresh_chat_turn(
    service: ProjectService,
    store: AppStore,
    project_id: str,
    request: RunRequest,
) -> Iterator[RunRequest]:
    """Serialize stable chat admission and keep graph and repository bindings fixed."""

    assert request.chat_id is not None
    kind = "node_chat" if request.chat_scope == "node" else "project_chat"
    with conversation_worktree_locks(f"{store.path}:{project_id}:{request.chat_id}"):
        require_chat_graph_target(service, store, project_id, request.chat_id)
        if store.has_resumable_paused_chat_task(project_id, kind, request.chat_id):
            raise AgentTaskAdmissionConflict(
                "This conversation has a paused turn. Resume or retry it before starting a new turn."
            )
        yield admit_conversation_worktree(service, store, project_id, request)
