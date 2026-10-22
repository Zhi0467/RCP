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
    chat_id = request.chat_id
    kind = "node_chat" if request.chat_scope == "node" else "project_chat"
    with conversation_worktree_locks(f"{store.path}:{project_id}:{chat_id}"):
        require_chat_graph_target(service, store, project_id, chat_id)
        fork = store.chat_fork(project_id, chat_id)
        if fork is not None and store.current_chat_session(project_id, kind, chat_id) is None:
            request = request.model_copy(
                update={
                    "provider": fork.origin.provider,
                    "model": fork.origin.model,
                    "reasoning": fork.origin.reasoning,
                    "run_on": fork.origin.run_on,
                    "chat_scope": "node" if fork.kind == "node_chat" else "project",
                    "node_id": fork.node_id,
                    "session_id": None,
                    "fork_session_id": fork.native_session_id,
                    "fork_source_cwd": fork.source_cwd,
                }
            )
        if store.has_resumable_paused_chat_task(project_id, kind, chat_id):
            raise AgentTaskAdmissionConflict(
                "This conversation has a paused turn. Resume or retry it before starting a new turn."
            )
        request = request.model_copy(
            update={"browser_requested": store.chat_browser_requested(project_id, chat_id)}
        )
        yield admit_conversation_worktree(service, store, project_id, request)


def validate_chat_fork_host(
    store: AppStore, project_id: str, request: RunRequest, host: str
) -> None:
    """Every fork attempt, Retry included, executes where its source session lives."""
    if request.fork_session_id is None:
        return
    fork = store.chat_fork(project_id, request.chat_id) if request.chat_id is not None else None
    if fork is None or fork.source_host != host:
        raise ValueError("The source session's execution machine has changed.")
