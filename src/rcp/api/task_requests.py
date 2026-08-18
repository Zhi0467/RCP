from __future__ import annotations

from typing import cast

from rcp.config import AgentSurface
from rcp.providers import profile_for
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskKind


def _resolved_graph_request(
    service: ProjectService,
    kind: AgentTaskKind,
    request: RunRequest,
) -> RunRequest:
    surface: AgentSurface = kind
    profile = service.resolve_agent_profile(
        surface,
        provider=request.provider,
        model=request.model,
        reasoning=request.reasoning,
        run_on=request.run_on,
    )
    resolved = request.model_copy(
        update={
            "provider": profile.provider,
            # An empty string is the explicit provider-default sentinel. Once a
            # request is resolved it must not collapse back to None, which means
            # "inherit the current surface setting" on a later continuation.
            "model": profile.model,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
            "run_truth_scope": list(
                request.run_truth_scope or service.manifest.agent.default_run_truth_scope
            ),
        }
    )
    result = service.resolve_skill_request(resolved)
    assert isinstance(result, RunRequest)
    return result


def _resolved_auto_research_request(
    service: ProjectService,
    request: AutoResearchRunRequest,
) -> AutoResearchRunRequest:
    if (
        request.provider is None
        or request.model is None
        or request.reasoning is None
        or request.run_on is None
        or request.run_truth_scope is None
    ):
        raise ValueError("Auto-research recovery requires its exact pinned execution profile.")
    profile_for(request.provider)
    if request.run_on not in service.manifest.machine_map:
        raise ValueError(f"unknown execution machine: {request.run_on}")
    skill_resolved = service.resolve_skill_request(cast(RunRequest, request))
    if not isinstance(skill_resolved, AutoResearchRunRequest):
        raise TypeError("Auto-research skill resolution changed the task request type.")
    return skill_resolved


__all__ = ["_resolved_auto_research_request", "_resolved_graph_request"]
