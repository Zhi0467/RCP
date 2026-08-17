from __future__ import annotations

import pytest

from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.episode_report import EpisodeReportRunRequest
from rcp.runs.task_policy import task_experiment_episode_id, task_graph_capable
from rcp.service import CoachRequest, RunRequest


@pytest.mark.parametrize("kind", ["seed", "refresh"])
def test_ingest_tasks_are_graph_capable(kind: str) -> None:
    assert task_graph_capable(kind, RunRequest())
    assert task_graph_capable(kind, {})


@pytest.mark.parametrize("kind", ["node_chat", "project_chat"])
def test_only_work_chats_are_graph_capable(kind: str) -> None:
    assert task_graph_capable(kind, RunRequest(mode="work"))
    assert task_graph_capable(kind, {"mode": "work"})
    assert not task_graph_capable(kind, RunRequest(mode="discuss"))
    assert not task_graph_capable(kind, {"mode": "discuss"})


@pytest.mark.parametrize("role", ["orchestrator", "worker"])
def test_auto_research_graph_capability_uses_explicit_actor_allowlist(role: str) -> None:
    values = {
        "episode_id": "episode-1",
        "role": role,
        **({"control_node_id": "experiment-1"} if role == "worker" else {}),
    }
    request = AutoResearchRunRequest.model_validate(values)

    assert task_graph_capable("auto_research", request)
    assert task_graph_capable("auto_research", values)


@pytest.mark.parametrize(
    "candidate",
    [
        {"episode_id": "episode-1", "role": "report"},
        {"episode_id": "episode-1", "role": "unknown"},
        {"episode_id": "episode-1", "role": []},
        {"episode_id": "episode-1", "role": {}},
        {"role": "orchestrator"},
        object(),
    ],
)
def test_auto_research_unknown_or_invalid_request_shapes_default_deny(candidate: object) -> None:
    assert not task_graph_capable("auto_research", candidate)


def test_episode_report_and_unknown_task_kinds_are_not_graph_capable() -> None:
    report = EpisodeReportRunRequest(
        episode_id="episode-1",
        provider="codex",
        model="gpt-5",
        reasoning="high",
        run_on="local",
        execution_host="local",
        session_id="session-1",
    )

    assert not task_graph_capable("episode_report", report)
    assert not task_graph_capable("auto_research", report)
    assert not task_graph_capable("unknown", {"mode": "work"})
    assert not task_graph_capable("refresh", CoachRequest(message="hello"))


def test_experiment_episode_id_is_selected_only_for_live_experiment_requests() -> None:
    assert (
        task_experiment_episode_id(
            RunRequest(patch_kind="experiment_loop", control_episode_id="episode-1")
        )
        == "episode-1"
    )
    assert task_experiment_episode_id(RunRequest(patch_kind="experiment_loop")) == ""
    assert (
        task_experiment_episode_id(RunRequest(patch_kind="work", control_episode_id="episode-1"))
        is None
    )
    assert (
        task_experiment_episode_id(
            {"patch_kind": "experiment_loop", "control_episode_id": "episode-1"}
        )
        is None
    )
    assert task_experiment_episode_id(CoachRequest(message="hello")) is None
