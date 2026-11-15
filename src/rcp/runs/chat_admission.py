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

    chat_id = request.chat_id
    assert chat_id is not None
    with conversation_worktree_locks(f"{store.path}:{project_id}:{chat_id}"):
        require_chat_graph_target(service, store, project_id, chat_id)
        # Archive is shared, so a tab that has not seen it yet cannot revive the chat.
        if refusal := store.chat_turn_refusal(project_id, chat_id):
            raise AgentTaskAdmissionConflict(refusal)
        focus = store.admit_chat_focus(project_id, chat_id, request.focus)
        if focus != request.focus:
            request = RunRequest.model_validate(request.model_dump() | {"focus": focus})
        request = request.model_copy(
            update={"browser_requested": store.chat_browser_requested(project_id, chat_id)}
        )
        yield admit_conversation_worktree(service, store, project_id, request)
