"""Browser admission and durable owner cleanup at provider-turn boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from rcp.limits import SSH_REPOSITORY_BROWSER_TIMEOUT_SECONDS
from rcp.providers.browser_grant import BrowserGrant, BrowserOwnerKey, BrowserTurnStatus
from rcp.runs import browser_runtime_seam
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES
from rcp.transport import RemoteRunStage

if TYPE_CHECKING:
    from pydantic import BaseModel

    from rcp.background import AgentTaskExecution
    from rcp.storage import AppStore

logger = logging.getLogger(__name__)


def browser_host_key(host: str) -> str:
    """Resolve SSH aliases before naming an owner; a repointed alias is a new owner."""
    if not host:
        return json.dumps([socket.gethostname(), os.getuid()], separators=(",", ":"))
    result = subprocess.run(
        ["ssh", "-G", host],
        capture_output=True,
        text=True,
        check=True,
        timeout=SSH_REPOSITORY_BROWSER_TIMEOUT_SECONDS,
    )
    config = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    return json.dumps([config["hostname"], config["user"], config["port"]], separators=(",", ":"))


def acquire_turn_browser(
    *,
    requested: bool,
    capability: str,
    store: AppStore | None,
    project_id: str | None,
    stage_root: str,
    execution_host: str,
    execution: RemoteRunStage | None,
    workspace_dir: str,
    chat_id: str | None,
) -> BrowserGrant:
    if not requested:
        return BrowserGrant()
    if capability not in {"work_auto", "orchestrate", "discuss"}:
        return BrowserGrant(requested=True, status="unavailable", reason_code="capability_denied")
    if store is None or project_id is None:
        return BrowserGrant(requested=True, status="unavailable", reason_code="owner_unavailable")
    owner = None
    try:
        owner = BrowserOwnerKey(
            space_id=store.space_id,
            project_id=project_id,
            stage_name=PurePosixPath(stage_root).name,
            host_key=browser_host_key(execution_host),
        )
        store.record_browser_owner(
            owner,
            execution_host=execution_host,
            workspace_dir=workspace_dir,
            stage_root=stage_root,
            chat_id=chat_id,
        )
        return browser_runtime_seam.acquire_browser_grant(
            owner,
            execution=execution,
            workspace_dir=workspace_dir,
            data_dir=store.path.parent,
        )
    except Exception:
        logger.exception("Browser admission failed")
        return BrowserGrant(
            requested=True,
            status="unavailable",
            reason_code="admission_failed",
            owner=owner,
        )


def finish_turn_browser(grant: BrowserGrant) -> BrowserTurnStatus:
    try:
        return browser_runtime_seam.finish_browser_grant(grant)
    except Exception:
        logger.exception("Browser finalization failed")
        return BrowserTurnStatus(status="lost", reason_code="finish_failed")


def close_chat_browser_owners(
    store: AppStore,
    project_id: str,
    chat_id: str | None = None,
    *,
    delete_profile: bool,
) -> None:
    """Persist cleanup before attempting it; an active turn retains its admitted browser."""
    with store.connection() as connection:
        condition = "project_id = ?"
        values: tuple[str, ...] = (project_id,)
        if chat_id is not None:
            condition += " AND chat_id = ?"
            values += (chat_id,)
        connection.execute(
            f"UPDATE browser_owners SET close_requested = 1, "
            f"delete_profile = MAX(delete_profile, ?) WHERE {condition}",
            (int(delete_profile), *values),
        )
    retry_browser_cleanup(store, project_id=project_id, chat_id=chat_id)


def retry_browser_cleanup(
    store: AppStore,
    *,
    project_id: str | None = None,
    chat_id: str | None = None,
    finished_operation_id: str | None = None,
) -> None:
    active_statuses = sorted(ACTIVE_AGENT_TASK_STATUSES)
    placeholders = ",".join("?" for _ in active_statuses)
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM browser_owners WHERE close_requested = 1"
        ).fetchall()
    for row in rows:
        if project_id is not None and row["project_id"] != project_id:
            continue
        if chat_id is not None and row["chat_id"] != chat_id:
            continue
        with store.connection() as connection:
            active = connection.execute(
                "SELECT 1 FROM graph_runs WHERE project_id = ? AND stage_root = ? "
                f"AND (status IN ({placeholders}) OR phase = 'awaiting_remote_result') "
                "AND operation_id != ? LIMIT 1",
                (
                    row["project_id"],
                    row["stage_root"],
                    *active_statuses,
                    finished_operation_id or "",
                ),
            ).fetchone()
        if active is not None or any(
            operation_id != finished_operation_id
            for operation_id, _ in store.unresolved_remote_provider_passes(
                row["execution_host"], row["stage_root"]
            )
        ):
            continue
        try:
            owner = BrowserOwnerKey.model_validate_json(row["owner_json"])
            # Never address an old profile through an alias which now names another account.
            if browser_host_key(row["execution_host"]) != owner.host_key:
                continue
            execution = RemoteRunStage(row["execution_host"]) if row["execution_host"] else None
            if execution is not None:
                execution.root = PurePosixPath(row["stage_root"])
            browser_runtime_seam.close_browser_owner(
                owner,
                execution=execution,
                delete_profile=bool(row["delete_profile"]),
                data_dir=store.path.parent,
            )
            with store.connection() as connection:
                if row["delete_profile"]:
                    connection.execute(
                        "DELETE FROM browser_owners WHERE owner_token = ?", (owner.token(),)
                    )
                else:
                    connection.execute(
                        "UPDATE browser_owners SET close_requested = 0 WHERE owner_token = ?",
                        (owner.token(),),
                    )
        except Exception:
            logger.exception("Browser cleanup remains pending")


def _browser_result_pending(store: AppStore, operation_id: str) -> bool:
    task = store.agent_task(operation_id)
    if task is None:
        return False
    # The stream closes before Background writes awaiting_remote_result. The
    # existing process receipt already protects that interval (and cancellation).
    return task.phase == "awaiting_remote_result" or any(
        pending_id == operation_id
        for pending_id, _ in store.unresolved_remote_provider_passes(
            task.stage_host or "", task.stage_root or ""
        )
    )


def _record_browser_finish(store: AppStore, operation_id: str, status: BrowserTurnStatus) -> None:
    store.set_browser_turn_status(operation_id, status)
    store.record_agent_task_receipt(operation_id, "browser_status", status.model_dump(mode="json"))
    task = store.agent_task(operation_id)
    chat_id = task.request.get("chat_id") if task is not None else None
    if (
        task is not None
        and task.episode_id is None
        and chat_id is not None
        and not store.chat_browser_requested(task.project_id, chat_id)
    ):
        close_chat_browser_owners(store, task.project_id, chat_id, delete_profile=True)
    retry_browser_cleanup(store, finished_operation_id=operation_id)


def finish_recorded_browser(store: AppStore, operation_id: str) -> None:
    """Finish the original lease only after reconciliation proves the provider stopped."""
    status = store.browser_turn_status(operation_id)
    if status.lease_id is not None:
        status = finish_turn_browser(
            BrowserGrant(requested=True, status="granted", lease_id=status.lease_id)
        )
    _record_browser_finish(store, operation_id, status)


@asynccontextmanager
async def browser_turn(
    request: BaseModel,
    *,
    workspace: Path,
    execution_host: str,
    execution: AgentTaskExecution | None,
    remote_stage: RemoteRunStage | None,
    capability: str,
) -> AsyncIterator[BrowserGrant]:
    """Resolve before rendering; release even if rendering or staging fails."""
    from rcp.runs.auto_research import AutoResearchRunRequest
    from rcp.service import RunRequest

    requested = (
        request.browser_requested
        if isinstance(request, (RunRequest, AutoResearchRunRequest))
        else False
    )
    chat_id = request.chat_id if isinstance(request, RunRequest) else None
    task = execution.store.agent_task(execution.operation_id) if execution is not None else None
    grant = await asyncio.to_thread(
        acquire_turn_browser,
        requested=requested,
        capability=capability,
        store=execution.store if execution is not None else None,
        project_id=task.project_id if task is not None else None,
        stage_root=(execution.stage_root if execution is not None else None)
        or str(workspace.parent),
        execution_host=execution_host,
        execution=remote_stage,
        workspace_dir=str(workspace),
        chat_id=chat_id,
    )
    try:
        if execution is not None:
            execution.store.set_browser_turn_status(
                execution.operation_id,
                BrowserTurnStatus(
                    status=grant.status,
                    reason_code=grant.reason_code,
                    detail=grant.detail,
                    lease_id=grant.lease_id,
                ),
            )
        yield grant
    finally:
        if execution is None or not _browser_result_pending(
            execution.store, execution.operation_id
        ):
            status = await asyncio.to_thread(finish_turn_browser, grant)
            if execution is not None:
                await asyncio.to_thread(
                    _record_browser_finish, execution.store, execution.operation_id, status
                )
