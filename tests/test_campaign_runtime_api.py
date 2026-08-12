from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from rcp.agents import AcceptanceAgentLauncher
from rcp.agents.command_protocol import SpawnArguments
from rcp.api.app import _campaign_worker_request
from rcp.config import write_agent_settings
from rcp.core.models import AuthorizedHuman
from rcp.runs.campaign import CampaignCommandContext, CampaignRunRequest
from rcp.storage import AgentTaskRecord, CampaignRecord

from .helpers import create_named_app, wait_for_task

_EXECUTION_PROFILES = (
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
    "orchestrator",
)


def _distinct_orchestrator_profile(manifest) -> None:
    profiles = {
        name: manifest.agent_profile(name).model_copy(deep=True) for name in _EXECUTION_PROFILES
    }
    profiles["project_chat"] = profiles["project_chat"].model_copy(
        update={"provider": "claude", "model": "chat-only", "reasoning": "low"}
    )
    profiles["orchestrator"] = profiles["orchestrator"].model_copy(
        update={"provider": "codex", "model": "orchestrator-only", "reasoning": "high"}
    )
    write_agent_settings(
        manifest,
        list(manifest.agent.default_run_truth_scope),
        profiles,
    )


def test_start_uses_dedicated_orchestrator_and_reaches_campaign_stream(
    manifest,
    tmp_path,
) -> None:
    _distinct_orchestrator_profile(manifest)
    app = create_named_app(
        str(manifest.path),
        data_dir=tmp_path / "data",
        acceptance_agent=True,
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 5,
                "starting_instruction": "Investigate the unresolved evidence.",
            },
        )
        assert response.status_code == 202
        payload = response.json()
        operation_id = payload["root_operation_id"]
        assert isinstance(operation_id, str)

        root = store.agent_task(operation_id)
        assert root is not None
        request = CampaignRunRequest.model_validate(root.request)
        assert request.role == "orchestrator"
        assert request.provider == "codex"
        assert request.model == "orchestrator-only"
        assert request.reasoning == "high"
        assert request.run_on == "laptop"
        assert request.run_truth_scope == ["repo-a"]
        assert request.instruction == "Investigate the unresolved evidence."
        assert root.dispatch_authority is not None
        assert root.dispatch_authority.profile == "orchestrator"
        assert root.dispatch_authority.task_contract == "orchestrate"
        assert root.dispatch_authority.scope.campaign_id == payload["campaign_id"]
        assert root.dispatch_authority.scope.run_truth_scope == ["repo-a"]

        settled = wait_for_task(store, operation_id)
        assert settled.status == "succeeded"
        assert settled.error is None
        assert settled.result is not None
        assert settled.result["messages"] == [
            "Completed the deterministic acceptance campaign orchestrator turn without a graph "
            "Patch."
        ]
        assert settled.result["graph_update"]["status"] == "none"
        launcher = app.state.launcher
        assert isinstance(launcher, AcceptanceAgentLauncher)
        assert len(launcher.launch_records) == 1
        assert launcher.launch_records[0].scenario == "campaign"
        assert launcher.launch_records[0].action == "turn"
        campaign = store.campaign(payload["campaign_id"])
        assert campaign is not None
        assert campaign.status == "running"
        assert campaign.ending is None
        meter = store.campaign_budget_meter(payload["campaign_id"])
        assert meter.observed_input_tokens == 256
        assert meter.observed_generated_tokens == 32

        duplicate = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={"invocation_ceiling": 5},
        )
        assert duplicate.status_code == 409
        assert len(store.campaigns(project_id)) == 1
        assert len(store.campaign_tasks(payload["campaign_id"])) == 1


def test_campaign_profile_resolution_failure_is_pre_mutation(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    service = app.state.catalog.open(project_id)

    def reject_profile(_surface):
        raise ValueError("orchestrator profile is not launchable")

    monkeypatch.setattr(service, "resolve_agent_profile", reject_profile)
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={"invocation_ceiling": 5},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "orchestrator profile is not launchable"}
    assert store.campaigns(project_id) == []
    assert store.agent_tasks(project_id) == []


def test_worker_request_inherits_the_pinned_campaign_profile() -> None:
    campaign_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    now = "2026-08-12T00:00:00+00:00"
    request = CampaignRunRequest(
        campaign_id=campaign_id,
        role="orchestrator",
        actor_operation_id=operation_id,
        provider="codex",
        model="pinned-model",
        reasoning="high",
        run_on="canonical-machine",
        run_truth_scope=["repo-a", "repo-b"],
        session_id="orchestrator-session",
        instruction="Root instruction",
        workflow_ids=["workflow-a"],
        skill_ids=["skill-a"],
        invoked_workflow_ids=["workflow-a"],
        invoked_skill_ids=["skill-a"],
        invoked_provider_skill_names=["provider-skill"],
        resolved_provider_skills=[
            {
                "provider": "codex",
                "machine": "canonical-machine",
                "provider_version": "1",
                "inventory_hash": "inventory",
                "name": "provider-skill",
                "label": "Provider skill",
                "description": "Pinned provider package.",
            }
        ],
        resolved_skill_packages=[{"id": "skill-a", "kind": "skill", "version": "1.2.3"}],
    )
    authorizer = AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name="Researcher",
    )
    campaign = CampaignRecord(
        campaign_id=campaign_id,
        project_id=str(uuid.uuid4()),
        root_operation_id=operation_id,
        status="running",
        invocation_ceiling=5,
        authorized_by=authorizer,
        created_at=now,
        updated_at=now,
    )
    task = AgentTaskRecord(
        operation_id=operation_id,
        project_id=campaign.project_id,
        campaign_id=campaign_id,
        kind="campaign",
        status="running",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="running",
        authorized_by=authorizer,
    )

    worker = _campaign_worker_request(
        CampaignCommandContext(campaign=campaign, task=task, request=request),
        SpawnArguments(seat_node_id="blocker-1", instruction="Resolve the blocker."),
    )

    for field in (
        "provider",
        "model",
        "reasoning",
        "run_on",
        "run_truth_scope",
        "workflow_ids",
        "skill_ids",
        "invoked_workflow_ids",
        "invoked_skill_ids",
        "invoked_provider_skill_names",
        "resolved_provider_skills",
        "resolved_skill_packages",
    ):
        assert getattr(worker, field) == getattr(request, field)
    assert worker.campaign_id == campaign_id
    assert worker.role == "worker"
    assert worker.control_node_id == "blocker-1"
    assert worker.instruction == "Resolve the blocker."
    assert worker.actor_operation_id is None
    assert worker.session_id is None
    assert worker.wake_cause is None
    assert worker.watcher_ids == []
    assert worker.ending is None
