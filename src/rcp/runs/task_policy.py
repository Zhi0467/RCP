from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.service import RunRequest

logger = logging.getLogger(__name__)

_StoredRequest = TypeVar("_StoredRequest", bound=BaseModel)

_AUTO_RESEARCH_GRAPH_ROLES = frozenset({"orchestrator", "worker"})
_CHAT_TASK_KINDS = frozenset({"node_chat", "project_chat"})
_INGEST_TASK_KINDS = frozenset({"seed", "refresh"})


def load_stored_request(
    model: type[_StoredRequest],
    stored: Mapping[str, object],
    *,
    operation_id: str | None = None,
) -> _StoredRequest:
    """Parse one request RCP itself persisted, tolerating fields this build dropped.

    Request models forbid unknown fields so a *live* caller cannot smuggle one
    past validation.  A stored request is not a live caller: it is RCP's own
    record of what it already did, and a field removed from the model since it
    was written must not make that task permanently unrecoverable.  Only keys
    the model no longer declares are dropped, and each drop is logged, so this
    stays an observable compatibility read rather than a silent fallback.
    Every remaining field is validated exactly as strictly as before.
    """

    unknown = sorted(set(stored) - set(model.model_fields))
    if not unknown:
        return model.model_validate(dict(stored))
    logger.warning(
        "Dropped %s from a stored %s while reading task %s; this build no longer declares it.",
        ", ".join(unknown),
        model.__name__,
        operation_id or "<unknown>",
    )
    return model.model_validate({k: v for k, v in stored.items() if k not in set(unknown)})


def task_graph_capable(kind: str, request: object) -> bool:
    """Return whether a persisted or live task request may produce a graph patch."""

    if kind in _INGEST_TASK_KINDS:
        return _run_request(request) is not None
    if kind in _CHAT_TASK_KINDS:
        run_request = _run_request(request)
        return run_request is not None and run_request.mode == "work"
    if kind == "auto_research":
        auto_research_request = _auto_research_request(request)
        return (
            auto_research_request is not None
            and auto_research_request.role in _AUTO_RESEARCH_GRAPH_ROLES
        )
    if kind == "branch_merge":
        return _branch_merge_request(request) is not None
    return False


def task_experiment_episode_id(request: object) -> str | None:
    """Return the bounded-experiment episode selected by a live Work request."""

    if isinstance(request, RunRequest) and request.patch_kind == "experiment_loop":
        return request.control_episode_id or ""
    return None


def _run_request(request: object) -> RunRequest | None:
    if isinstance(request, RunRequest):
        return request
    if not isinstance(request, dict):
        return None
    try:
        return RunRequest.model_validate(request)
    except ValidationError:
        return None


def _auto_research_request(request: object) -> AutoResearchRunRequest | None:
    if isinstance(request, AutoResearchRunRequest):
        return request
    if not isinstance(request, dict):
        return None
    role = request.get("role")
    if not isinstance(role, str) or role not in _AUTO_RESEARCH_GRAPH_ROLES:
        return None
    try:
        return AutoResearchRunRequest.model_validate(request)
    except ValidationError:
        return None


def _branch_merge_request(request: object) -> BranchMergeRunRequest | None:
    if isinstance(request, BranchMergeRunRequest):
        return request
    if not isinstance(request, dict):
        return None
    try:
        return BranchMergeRunRequest.model_validate(request)
    except ValidationError:
        return None
