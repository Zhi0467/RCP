"""Resolve an agent's placement and enforce its member and turn boundaries."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import HTTPException, Request

from rcp.agents.agent_profile import OFFER, AgentProfile, Placement, resolve_agent_profile
from rcp.core.models import AuthorizedHuman
from rcp.keyed_locks import KeyedLocks
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.agents import AgentRecord
from rcp.storage.models import AgentTaskKind


@dataclass(frozen=True)
class AgentOwner:
    agent: AgentRecord
    profile: AgentProfile

    @property
    def project_id(self) -> str | None:
        return self.agent.project_id if self.profile.placement.entry == "project" else None

    @property
    def scope_id(self) -> str:
        return self.project_id or self.agent.space_id

    def task(self, store: AppStore, operation_id: str) -> AgentTaskRecord:
        task = store.agent_task(operation_id)
        if (
            task is None
            or not task.visible
            or task.project_id != self.scope_id
            or task.request.get("chat_id") != self.agent.agent_id
        ):
            raise HTTPException(status_code=404, detail="Agent task not found")
        return task

    @contextmanager
    def write_admission(self, request: Request) -> Iterator[None]:
        from rcp.api.dependencies import get_store, project_write_admission

        thread = get_store(request).agent_thread(self.agent.agent_id)
        if thread is not None and thread.closed_at is not None:
            raise HTTPException(status_code=404, detail="Agent not found")
        if self.project_id is None:
            yield
        else:
            with project_write_admission(self.project_id, request):
                yield

    def turn_kind(self, body: dict[str, object]) -> AgentTaskKind:
        return chat_turn_kind(body.get("chat_scope"))


def chat_turn_kind(chat_scope: object) -> AgentTaskKind:
    """The task kind of a project agent's turn in this chat scope."""
    return "node_chat" if chat_scope == "node" else "project_chat"


def is_chat_turn(task: AgentTaskRecord) -> bool:
    return task.kind in {"project_chat", "node_chat", "space_chat"}


def resolve_agent(
    store: AppStore, agent_id: str, user_id: str, project_id: str | None = None
) -> AgentOwner:
    agent = store.agent(agent_id)
    if agent is None and project_id is not None:
        try:
            valid_id = str(uuid.UUID(agent_id)) == agent_id
        except ValueError:
            valid_id = False
        if not valid_id or not store.is_project_member(project_id, user_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        agent = AgentRecord(
            agent_id=agent_id,
            space_id=store.space_id,
            project_id=project_id,
            visibility="shared",
            entry="project",
            created_at=store.now(),
        )
    if agent is not None and project_id is not None and project_id != agent.project_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if (
        agent is None
        or (agent.visibility == "private" and agent.owner_user_id != user_id)
        or (agent.project_id is not None and not store.is_project_member(agent.project_id, user_id))
    ):
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentOwner(
        agent,
        resolve_agent_profile(Placement(agent.entry, agent.visibility), None, OFFER),
    )


def resolve_agent_task(
    store: AppStore,
    operation_id: str,
    user_id: str,
    *,
    project_id: str | None,
    thread_id: str | None,
) -> AgentTaskRecord:
    if thread_id is not None:
        owner = resolve_agent(store, thread_id, user_id)
        if project_id is not None and project_id != owner.project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        return owner.task(store, operation_id)
    task = store.agent_task(operation_id)
    if task is None or task.project_id != project_id or not task.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    return task


def task_retry_overrides(
    task: AgentTaskRecord, overrides: dict[str, str], store: AppStore
) -> dict[str, str]:
    agent = store.agent(str(task.request.get("chat_id") or ""))
    if agent is not None and agent.entry == "single_entry":
        return {key: value for key, value in overrides.items() if key != "run_on"}
    return overrides


@contextmanager
def task_write_admission(task: AgentTaskRecord, request: Request) -> Iterator[None]:
    from rcp.api.dependencies import get_store, project_write_admission

    store = get_store(request)
    agent = store.agent(str(task.request.get("chat_id") or ""))
    if agent is not None and agent.entry == "single_entry":
        yield
    else:
        with project_write_admission(task.project_id, request):
            yield


def require_comment_writable(scope_id: str, request: Request) -> None:
    from rcp.api.dependencies import get_catalog, get_project_writer, get_store

    store = get_store(request)
    if scope_id != store.space_id:
        store.require_project_accepts_new_work(scope_id)
        get_project_writer(get_catalog(request), scope_id).history.require_writable()


@contextmanager
def comment_write_admission(scope_id: str, request: Request) -> Iterator[None]:
    from rcp.api.dependencies import get_store, project_write_admission

    if scope_id == get_store(request).space_id:
        yield
    else:
        with project_write_admission(scope_id, request):
            require_comment_writable(scope_id, request)
            yield


class LimitResumeMemberGone(ValueError):
    pass


@contextmanager
def task_write_admission_as(
    task: AgentTaskRecord,
    authorized_by: AuthorizedHuman,
    *,
    store: AppStore,
    operation_lock: KeyedLocks,
) -> Iterator[None]:
    with operation_lock(task.project_id):
        member = store.space_user(authorized_by.user_id)
        expected_kind = "local_owner" if store.space_kind == "personal" else "team_member"
        if (
            member is None
            or member.identity_kind != expected_kind
            or not member.display_name
            or member.removal_started_at is not None
            or member.removed_at is not None
            or authorized_by.space_id != store.space_id
        ):
            raise LimitResumeMemberGone("The authorizing member no longer has access.")
        agent = store.agent(str(task.request.get("chat_id") or ""))
        if (
            agent is not None
            and agent.visibility == "private"
            and agent.owner_user_id != member.user_id
        ):
            raise LimitResumeMemberGone("The authorizing member no longer owns this conversation.")
        thread = store.agent_thread(str(task.request.get("chat_id") or ""))
        if thread is not None and thread.closed_at is not None:
            raise ValueError("The conversation is closed.")
        if task.project_id != store.space_id:
            if not store.is_project_member(task.project_id, member.user_id):
                raise LimitResumeMemberGone("The authorizing member is no longer a project member.")
            if agent is None or agent.entry != "single_entry":
                store.require_project_accepts_new_work(task.project_id)
        yield
