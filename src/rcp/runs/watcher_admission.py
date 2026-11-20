"""Admission for a watcher wake of an agent outside any episode.

The wake continues the agent's newest turn settings, is authorized by the
human whose turn armed the watchers, spends nothing, and claims the watchers
in the same transaction that queues the turn.

The delivery-admission flag it reads belongs to the engine — ``shutdown`` clears
it and ``accept_watcher_notifications`` sets it. This module reads it under the
engine's own lock rather than copying the fence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from rcp.core.models import AuthorizedHuman
from rcp.runs.provider_login import AdmissionRefused
from rcp.runs.task_policy import resolved_dispatch_authority
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, WatcherRecord
from rcp.watchers import watcher_wake_message

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


def start_plain_watcher_wake(
    tasks: BackgroundAgentTasks,
    agent_id: str,
    ready: list[WatcherRecord],
    authorized_by: AuthorizedHuman,
    fence: Callable[[Callable[[], None]], bool] | None = None,
) -> AgentTaskRecord | None:
    """Queue one ordinary turn that wakes the agent for its ready watchers."""

    store = tasks.store
    project_id = ready[0].project_id
    latest = store.newest_chat_turn(project_id, agent_id)
    if latest is None or latest.kind not in {"node_chat", "project_chat"}:
        return None
    watcher_ids = [watcher.watcher_id for watcher in ready]
    request = RunRequest.model_validate(latest.request).model_copy(
        update={
            "message": watcher_wake_message(ready),
            "session_id": None,
            "mode": "work",
            "trigger": "watcher",
            "patch_kind": "work",
            "invoked_workflow_ids": [],
            "invoked_skill_ids": [],
            "watcher_ids": watcher_ids,
            "attachments": [],
            "browser_requested": store.chat_browser_requested(project_id, agent_id),
        }
    )
    request_data = request.model_dump(mode="json")
    estimate, samples = store.agent_task_estimate(project_id, latest.kind, request_data)
    now = store.now()
    record = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=project_id,
        graph_target=latest.graph_target,
        kind=latest.kind,
        status="queued",
        request=request_data,
        created_at=now,
        updated_at=now,
        status_message="Waiting to deliver a watcher update.",
        estimate_seconds=estimate,
        estimate_samples=samples,
        phase="queued",
        last_activity_at=now,
        authorized_by=authorized_by,
        dispatch_authority=resolved_dispatch_authority(
            store,
            tasks.dispatch_authority_resolver,
            latest.kind,
            request,
            project_id=project_id,
        ),
    )
    started: AgentTaskRecord | None = None

    def claim_and_spawn() -> None:
        nonlocal started
        with tasks._watcher_delivery_lock:
            if not tasks._accepting_watcher_deliveries:
                return
            try:
                tasks.admit_provider_task(
                    project_id, request, authorized_by=authorized_by, automatic=True
                )
            except AdmissionRefused:
                return
            stored = store.create_watcher_wake_task(record, agent_id, watcher_ids)
            if stored is not None:
                started = (
                    tasks.launch_admitted(stored.operation_id)
                    if stored.status == "queued"
                    else stored
                )

    if fence is not None:
        fence(claim_and_spawn)
    else:
        claim_and_spawn()
    return started
