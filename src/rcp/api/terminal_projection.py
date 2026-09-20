"""Project-owned repository and running Work projections for member terminals."""

from __future__ import annotations

from pathlib import Path

from rcp.config import Manifest
from rcp.storage import AppStore
from rcp.terminals import TerminalSession


def running_repository_work(
    store: AppStore, project_id: str, manifest: Manifest
) -> dict[str, list[dict[str, str]]]:
    work: dict[str, list[dict[str, str]]] = {repo.alias: [] for repo in manifest.repositories}
    for task in store.all_project_agent_tasks(project_id):
        if not task.active or task.queued:
            continue
        request = task.request
        if request.get("mode") != "work" and not (
            task.kind == "auto_research" and request.get("role") == "worker"
        ):
            continue
        # Launch receipts name the actual checkout, including worktree binding.
        launches = [
            receipt
            for receipt in store.agent_task_receipts(task.operation_id)
            if "canonical_repository_roots" in receipt.payload
        ]
        roots = launches[-1].payload["canonical_repository_roots"] if launches else None
        for repository in manifest.repositories:
            if manifest.machine_map[repository.machine].host:
                continue
            if roots is not None:
                matches = str(Path(repository.path).resolve()) in roots
            else:
                matches = (
                    repository.alias in (request.get("run_truth_scope") or [])
                    and repository.machine == request.get("run_on")
                    and (not request.get("worktree") or bool(request.get("worktree_integration")))
                )
            if matches:
                work[repository.alias].append(
                    {
                        "operation_id": task.operation_id,
                        "title": task.status_message or "Work turn",
                    }
                )
    return work


def terminal_session_payload(
    session: TerminalSession, work: dict[str, list[dict[str, str]]]
) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "repository_id": session.repository_id,
        "path": session.path,
        "member_id": session.member_id,
        "started_at": session.started_at,
        "state": session.state,
        "running_work": work.get(session.repository_id, []),
    }
