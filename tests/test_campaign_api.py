from __future__ import annotations

import hashlib
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.api.campaigns import serialize_campaign
from rcp.background import AgentTaskExecution
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import AuthorizedHuman
from rcp.runs.campaign import CampaignRunRequest
from rcp.runs.campaign_delivery import record_campaign_message
from rcp.runs.campaign_recovery import record_structural_failure
from rcp.skill_registry import official_registry
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    CampaignEnding,
    CampaignNotRunning,
    CampaignRecord,
    CampaignReportRecord,
)

from .helpers import create_named_app, wait_for_task

_RUN_TRUTH_SCOPE = ["repo-a"]


def _campaign_authority(campaign_id: str, role: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="orchestrator" if role == "orchestrator" else "ordinary",
        task_contract="orchestrate" if role == "orchestrator" else "work_auto",
        scope=AgentDispatchScope(
            run_truth_scope=_RUN_TRUTH_SCOPE,
            campaign_id=campaign_id,
            patch_kind="work",
        ),
    )


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _authorizer(store: AppStore) -> AuthorizedHuman:
    owner = store.local_owner
    assert owner is not None
    assert owner.display_name is not None
    return AuthorizedHuman(
        space_id=store.space_id,
        user_id=owner.user_id,
        display_name=owner.display_name,
    )


def _campaign(
    store: AppStore,
    *,
    project_id: str,
    invocation_ceiling: int = 5,
    root_status: str = "succeeded",
    stage_root: str | None = None,
    root_run_on: str = "local",
    root_stage_host: str | None = "execution-host",
    authorizer: AuthorizedHuman | None = None,
) -> tuple[CampaignRecord, AgentTaskRecord]:
    now = store.now()
    authorizer = authorizer or _authorizer(store)
    return store.create_campaign_with_root_task(
        CampaignRecord(
            campaign_id="campaign",
            project_id=project_id,
            status="queued",
            invocation_ceiling=invocation_ceiling,
            authorized_by=authorizer,
            created_at=now,
            updated_at=now,
        ),
        AgentTaskRecord(
            operation_id="root",
            project_id=project_id,
            campaign_id="campaign",
            kind="campaign",
            status=root_status,
            request=CampaignRunRequest(
                campaign_id="campaign",
                role="orchestrator",
                actor_operation_id="root",
                provider="codex",
                model="test-model",
                reasoning="medium",
                run_on=root_run_on,
                run_truth_scope=_RUN_TRUTH_SCOPE,
            ).model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="running" if root_status == "running" else "done",
            native_session_id="orchestrator-session",
            stage_host=root_stage_host if stage_root else None,
            stage_root=stage_root,
            authorized_by=authorizer,
            dispatch_authority=_campaign_authority("campaign", "orchestrator"),
        ),
    )


def _recoverable_report_task(
    store: AppStore,
    campaign: CampaignRecord,
    root: AgentTaskRecord,
    *,
    status: str,
    request_updates: dict[str, object] | None = None,
) -> AgentTaskRecord:
    selection = official_registry().resolve(workflow_ids=[], skill_ids=["campaign-report"])
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="report",
        ending="completed",
        actor_operation_id=root.operation_id,
        provider="codex",
        model="test-model",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        session_id=root.native_session_id,
        workflow_ids=selection.workflow_ids,
        skill_ids=selection.skill_ids,
        invoked_skill_ids=["campaign-report"],
        resolved_skill_packages=selection.resolved_skill_packages,
    )
    if request_updates:
        request = CampaignRunRequest.model_validate(
            {**request.model_dump(mode="json"), **request_updates}
        )
    _, report = store.allocate_campaign_report_task(
        AgentTaskRecord(
            operation_id="report-task",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status=status,
            request=request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message=status,
            error="provider failed" if status == "failed" else None,
            parent_operation_id=root.operation_id,
            native_session_id=root.native_session_id,
            stage_host=root.stage_host,
            stage_root=root.stage_root,
            authorized_by=campaign.authorized_by,
        ),
        ending="completed",
    )
    return report


def _ending_report(
    store: AppStore,
    campaign: CampaignRecord,
    root: AgentTaskRecord,
    ending: CampaignEnding = "exhausted",
) -> CampaignReportRecord:
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, ending)
    _, report_task = store.allocate_campaign_report_task(
        AgentTaskRecord(
            operation_id="report-task",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="report",
                ending=ending,
                actor_operation_id=root.operation_id,
                provider="codex",
                model="test-model",
                reasoning="medium",
                run_on="local",
                run_truth_scope=_RUN_TRUTH_SCOPE,
                session_id=root.native_session_id,
            ).model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="done",
            parent_operation_id=root.operation_id,
            native_session_id=root.native_session_id,
            stage_host=root.stage_host,
            stage_root=root.stage_root,
            authorized_by=campaign.authorized_by,
        ),
        ending=ending,
    )
    html = (
        "<article><h1>Campaign report</h1>"
        "<p>The durable investigation stopped at its budget.</p></article>"
    )
    report = CampaignReportRecord(
        report_id="report-artifact",
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending=ending,
        sha256=hashlib.sha256(html.encode()).hexdigest(),
        html=html,
        created_at=store.now(),
    )
    ended, stored_report = store.finish_campaign_wrapup(report)
    assert wrapping.status == "wrapping_up"
    return stored_report


def _campaign_report_tasks(store: AppStore, campaign_id: str) -> list[AgentTaskRecord]:
    return [
        task for task in store.campaign_tasks(campaign_id) if task.request.get("role") == "report"
    ]


async def _settling_campaign_stream(_project_id, _kind, request, _execution):
    yield _sse(
        AgentEvent(
            event="session",
            session_id=request.session_id or "orchestrator-session",
        )
    )
    yield _sse(AgentEvent(event="done"))


def _wait_for_message_delivery(store: AppStore, message_id: str) -> AgentTaskRecord:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        message = store.campaign_message(message_id)
        assert message is not None
        if message.delivery_operation_id is not None:
            task = store.agent_task(message.delivery_operation_id)
            assert task is not None
            return task
        time.sleep(0.01)
    raise AssertionError(f"campaign message {message_id} was not delivered")


def test_campaign_list_is_empty_then_start_is_durable(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    with TestClient(app) as client:
        assert client.get(f"/api/projects/{project_id}/campaigns").json() == []
        assert store.campaigns(project_id) == []
        assert store.agent_tasks(project_id) == []

        response = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 5,
                "starting_instruction": "Investigate the unresolved evidence.",
            },
        )

        assert response.status_code == 202
        payload = response.json()
        campaign = store.campaign(payload["campaign_id"])
        root = store.agent_task(payload["root_operation_id"])
        assert campaign is not None
        assert root is not None
        assert campaign.root_operation_id == root.operation_id
        assert campaign.invocation_ceiling == 5
        assert root.request["instruction"] == "Investigate the unresolved evidence."
        assert [
            item["campaign_id"]
            for item in client.get(f"/api/projects/{project_id}/campaigns").json()
        ] == [campaign.campaign_id]


def test_campaign_list_and_stop_route_retain_one_idempotent_stop_intent(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id, root_status="running")
    client = TestClient(app)

    listed = client.get(f"/api/projects/{project_id}/campaigns")
    assert listed.status_code == 200
    assert [item["campaign_id"] for item in listed.json()] == [campaign.campaign_id]
    assert listed.json()[0]["status"] == "running"
    assert listed.json()[0]["current_orchestrator_task_id"] == root.operation_id
    assert listed.json()[0]["budget"]["invocations_used"] == 1
    assert [task["operation_id"] for task in listed.json()[0]["tasks"]] == [root.operation_id]

    url = f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/stop"
    first = client.post(url)
    second = client.post(url)

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "stopping"
    assert first.json()["stop_requested_at"] is not None
    assert second.json()["stop_requested_at"] == first.json()["stop_requested_at"]
    assert first.json()["can_stop"] is second.json()["can_stop"] is False
    assert first.json()["current_orchestrator_task_id"] == root.operation_id
    assert first.json()["budget"] == second.json()["budget"]
    assert first.json()["budget"]["invocations_used"] == 1
    assert (
        store.campaign(campaign.campaign_id).stop_requested_at
        == first.json()[  # type: ignore[union-attr]
            "stop_requested_at"
        ]
    )
    assert store.campaign_tasks(campaign.campaign_id) == [root]


def test_legacy_campaign_without_actor_binding_remains_inspectable(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, _root = _campaign(store, project_id=project_id)

    def missing_binding(_operation_id: str):
        raise ValueError("legacy actor binding is unavailable")

    monkeypatch.setattr(store, "campaign_actor_binding", missing_binding)

    serialized = serialize_campaign(store, project_id, campaign)

    assert serialized.current_orchestrator_task_id is None
    assert serialized.current_control_task_id is None
    assert [task.operation_id for task in serialized.tasks] == ["root"]


def test_wrapping_campaign_never_exposes_failed_operational_root_as_its_control_task(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id, root_status="failed")
    wrapping = store.begin_campaign_wrapup(
        campaign.campaign_id,
        "failed",
        error="The orchestrator cannot continue.",
    )

    serialized = serialize_campaign(store, project_id, wrapping)

    assert serialized.current_orchestrator_task_id == root.operation_id
    assert serialized.current_control_task_id is None
    assert serialized.recovery is None

    pending = store.schedule_campaign_report_reconciliation(
        campaign.campaign_id,
        ending="failed",
        diagnostic="The report turn is waiting for its exact stage.",
    )
    scheduled = serialize_campaign(store, project_id, wrapping)
    assert scheduled.current_control_task_id is None
    assert scheduled.recovery is not None
    assert scheduled.recovery.purpose == "report_admission"
    assert scheduled.recovery.next_attempt_at == pending.next_attempt_at


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_exhausted_campaign_exposes_failed_orchestrator_with_pending_recovery(
    manifest,
    tmp_path,
    status: str,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id, root_status=status)
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")
    pending = store.schedule_campaign_task_recovery(
        root.operation_id,
        failure_kind="provider",
        retry_mode="exact",
        diagnostic="The orchestrator provider is temporarily unavailable.",
    )

    serialized = serialize_campaign(store, project_id, wrapping)

    assert serialized.current_orchestrator_task_id == root.operation_id
    assert serialized.current_control_task_id == root.operation_id
    assert serialized.recovery is not None
    assert serialized.recovery.purpose == "task"
    assert serialized.recovery.status == "pending"
    assert serialized.recovery.operation_id == root.operation_id
    assert serialized.recovery.next_attempt_at == pending.next_attempt_at


@pytest.mark.parametrize("ending_action", ["finish", "stop"])
def test_campaign_parent_controls_the_latest_paused_worker_leaf_during_ending(
    manifest,
    tmp_path,
    ending_action: str,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id)
    worker_request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="worker",
        actor_operation_id="paused-worker",
        control_node_id="experiment/check",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        session_id="worker-session",
    )
    now = store.now()
    worker = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="paused-worker",
            project_id=project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="paused",
            request=worker_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Paused at its checkpoint.",
            parent_operation_id=root.operation_id,
            native_session_id="worker-session",
            stage_root=str(tmp_path / "worker-stage"),
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(campaign.campaign_id, "worker"),
        ),
        role="worker",
    )
    if ending_action == "finish":
        ending = store.begin_campaign_wrapup(campaign.campaign_id, "completed")
        assert ending.status == "wrapping_up"
    else:
        ending = store.request_campaign_stop(campaign.campaign_id)
        assert ending.status == "stopping"

    serialized = serialize_campaign(store, project_id, ending)

    assert serialized.current_orchestrator_task_id == root.operation_id
    assert serialized.current_control_task_id == worker.operation_id
    assert serialized.tasks[-1].operation_id == worker.operation_id
    assert serialized.tasks[-1].can_resume is True


def test_wrapping_campaign_controls_target_latest_report_attempt_with_recovery_state(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "report-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        stage_root=str(stage),
    )
    report = _recoverable_report_task(store, campaign, root, status="failed")
    pending = store.schedule_campaign_task_recovery(
        report.operation_id,
        failure_kind="provider",
        retry_mode="exact",
        diagnostic="The report provider is temporarily unavailable.",
    )
    wrapping = store.campaign(campaign.campaign_id)
    assert wrapping is not None

    serialized = serialize_campaign(store, project_id, wrapping)

    assert serialized.current_orchestrator_task_id == root.operation_id
    assert serialized.current_control_task_id == report.operation_id
    assert serialized.recovery is not None
    assert serialized.recovery.status == "pending"
    assert serialized.recovery.operation_id == report.operation_id
    assert serialized.recovery.next_attempt_at == pending.next_attempt_at

    now = store.now()
    report_retry = store.create_campaign_recovery_task(
        report.model_copy(
            update={
                "operation_id": "report-task-retry",
                "status": "paused",
                "created_at": now,
                "updated_at": now,
                "status_message": "paused",
                "error": None,
                "attempt": report.attempt + 1,
                "parent_operation_id": report.operation_id,
            }
        )
    )
    store.complete_campaign_recovery(
        pending.recovery_id,
        admitted_operation_id=report_retry.operation_id,
        expected_operation_id=report.operation_id,
    )

    latest = serialize_campaign(store, project_id, wrapping)

    assert latest.current_control_task_id == report_retry.operation_id
    assert latest.recovery is not None
    assert latest.recovery.status == "admitted"


def test_human_message_remains_pending_when_root_has_no_checkpointed_stage(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id)
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages",
        json={"body": "  Preserve this until the orchestrator can resume.  "},
    )

    assert response.status_code == 201
    message = response.json()
    assert message["sender_role"] == "human"
    assert message["sender_task_id"] is None
    assert message["authorized_by"] == _authorizer(store).model_dump(mode="json")
    assert message["recipient_task_id"] == root.operation_id
    assert message["body"] == "Preserve this until the orchestrator can resume."
    assert message["delivered_at"] is None
    assert message["delivery_operation_id"] is None
    pending = store.pending_campaign_messages(campaign.campaign_id, root.operation_id)
    assert [item.model_dump(mode="json") for item in pending] == [message]
    listed = client.get(f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages")
    assert listed.status_code == 200
    assert listed.json() == [message]
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 1
    assert store.campaign_tasks(campaign.campaign_id) == [root]


def test_human_message_reports_its_durable_write_when_immediate_delivery_fails(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id)

    def fail_delivery(*_args, **_kwargs):
        raise RuntimeError("delivery unavailable")

    monkeypatch.setattr("rcp.api.app.deliver_pending_campaign_mail", fail_delivery)
    response = TestClient(app).post(
        f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages",
        json={"body": "Keep this durable even when delivery is unavailable."},
    )

    assert response.status_code == 201
    message = response.json()
    assert message["delivered_at"] is None
    assert message["delivery_operation_id"] is None
    assert store.campaign_messages(campaign.campaign_id) == [
        store.campaign_message(message["message_id"])
    ]
    assert store.pending_campaign_messages(campaign.campaign_id, root.operation_id) == [
        store.campaign_message(message["message_id"])
    ]


@pytest.mark.parametrize(
    ("ending", "terminal_status"),
    [
        ("completed", "succeeded"),
        ("stopped", "stopped"),
        ("failed", "failed"),
    ],
)
def test_human_message_rejects_a_terminal_campaign_before_persistence(
    manifest,
    tmp_path,
    ending,
    terminal_status,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id)
    _ending_report(store, campaign, root, ending)
    terminal = store.campaign(campaign.campaign_id)
    assert terminal is not None and terminal.status == terminal_status
    tasks_before = store.campaign_tasks(campaign.campaign_id)
    budget_before = store.campaign_budget_meter(campaign.campaign_id)

    response = TestClient(app).post(
        f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages",
        json={"body": "This must not become permanently pending."},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Campaign has already ended"}
    assert store.campaign_messages(campaign.campaign_id) == []
    assert store.campaign_tasks(campaign.campaign_id) == tasks_before
    assert store.campaign_budget_meter(campaign.campaign_id) == budget_before


@pytest.mark.parametrize("ending", ["completed", "exhausted", "stopped", "failed"])
def test_human_message_rejects_every_fenced_campaign_ending_before_persistence(
    manifest,
    tmp_path,
    ending,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, _ = _campaign(store, project_id=project_id)
    wrapping = store.begin_campaign_wrapup(
        campaign.campaign_id,
        ending,
        error="typed failure" if ending == "failed" else None,
    )
    assert wrapping.status == "wrapping_up"
    assert wrapping.ending == ending
    tasks_before = store.campaign_tasks(campaign.campaign_id)
    budget_before = store.campaign_budget_meter(campaign.campaign_id)

    response = TestClient(app).post(
        f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages",
        json={"body": "This must not cross the campaign ending fence."},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Campaign is not accepting new mail"}
    assert store.campaign_messages(campaign.campaign_id) == []
    assert store.campaign_tasks(campaign.campaign_id) == tasks_before
    assert store.campaign_budget_meter(campaign.campaign_id) == budget_before


def test_human_message_keeps_the_current_sender_snapshot_across_rename_and_restart(
    manifest,
    tmp_path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    sender = _authorizer(store)
    starter = AuthorizedHuman(
        space_id=store.space_id,
        user_id=str(uuid.uuid4()),
        display_name="Campaign starter",
    )
    campaign, _ = _campaign(
        store,
        project_id=project_id,
        authorizer=starter,
    )

    with TestClient(app) as client:
        sent = client.post(
            f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages",
            json={"body": "Steer using my current identity."},
        )
        assert sent.status_code == 201, sent.text
        assert sent.json()["authorized_by"] == sender.model_dump(mode="json")
        assert sent.json()["authorized_by"] != campaign.authorized_by.model_dump(mode="json")
        renamed = client.patch(
            "/api/identity",
            json={"display_name": "Renamed researcher"},
        )
        assert renamed.status_code == 200, renamed.text

    restarted = create_named_app(str(manifest.path), data_dir=data_dir)
    with TestClient(restarted) as client:
        listed = client.get(f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages")

    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["authorized_by"] == sender.model_dump(mode="json")


def test_human_message_remains_pending_when_pinned_root_actor_is_busy(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        root_status="running",
        stage_root=str(stage),
    )
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages",
        json={"body": "Deliver after the current turn settles."},
    )

    assert response.status_code == 201
    message = response.json()
    assert message["delivered_at"] is None
    assert message["delivery_operation_id"] is None
    pending = store.pending_campaign_messages(campaign.campaign_id, root.operation_id)
    assert [item.model_dump(mode="json") for item in pending] == [message]
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 1
    assert store.campaign_tasks(campaign.campaign_id) == [root]


def test_human_message_response_reloads_its_atomic_delivery_state(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        root_status="succeeded",
        stage_root=str(stage),
    )

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/messages",
            json={"body": "Continue from this durable steering note."},
        )

    assert response.status_code == 201
    message = response.json()
    assert message["delivered_at"] is not None
    assert message["delivery_operation_id"] is not None
    current = store.campaign_message(message["message_id"])
    assert current is not None
    assert current.model_dump(mode="json") == message
    delivery = store.agent_task(message["delivery_operation_id"])
    assert delivery is not None
    assert delivery.parent_operation_id == root.operation_id


def test_startup_reconciles_durable_mail_after_campaign_callbacks_are_wired(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        root_status="succeeded",
        stage_root=str(stage),
    )
    message = record_campaign_message(
        store,
        campaign_id=campaign.campaign_id,
        sender_role="human",
        sender_task_id=None,
        authorized_by=campaign.authorized_by,
        recipient_task_id=root.operation_id,
        body="Resume this durable message after startup.",
    )

    async def stream(_project_id, _kind, request, _execution):
        yield _sse(AgentEvent(event="session", session_id=request.session_id))
        yield _sse(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    with TestClient(app):
        delivery = _wait_for_message_delivery(store, message.message_id)

    assert delivery.parent_operation_id == root.operation_id
    assert delivery.request["wake_cause"] == "message"


def test_startup_fences_a_depleted_crashed_turn_but_waits_for_its_recovery(
    manifest,
    tmp_path,
) -> None:
    data_dir = tmp_path / "data"
    first = create_named_app(str(manifest.path), data_dir=data_dir)
    project_id = first.state.default_project_id
    assert project_id is not None
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        first.state.background_tasks.store,
        project_id=project_id,
        invocation_ceiling=2,
        root_status="running",
        stage_root=str(stage),
    )

    restarted = create_named_app(str(manifest.path), data_dir=data_dir)
    tasks = restarted.state.background_tasks
    tasks.stream = _settling_campaign_stream
    with TestClient(restarted):
        pass

    interrupted = tasks.store.agent_task(root.operation_id)
    assert interrupted is not None and interrupted.status == "interrupted"
    current = tasks.store.campaign(campaign.campaign_id)
    assert current is not None
    assert current.status == "wrapping_up"
    assert current.ending == "exhausted"
    recovery = tasks.store.campaign_control_recovery(
        campaign.campaign_id,
        interrupted.operation_id,
    )
    assert recovery is not None and recovery.status == "pending"
    assert _campaign_report_tasks(tasks.store, campaign.campaign_id) == []


def test_startup_fences_orphaned_structural_failure_before_pending_worker_mail(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=5,
        root_status="running",
        stage_root=str(stage),
    )
    now = store.now()
    worker = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="idle-worker",
            project_id=project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="worker",
                actor_operation_id="idle-worker",
                control_node_id="blk/check-result",
                provider="codex",
                run_on="local",
                run_truth_scope=_RUN_TRUTH_SCOPE,
                session_id="worker-session",
            ).model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            parent_operation_id=root.operation_id,
            native_session_id="worker-session",
            stage_host="execution-host",
            stage_root=str(stage),
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(campaign.campaign_id, "worker"),
        ),
        role="worker",
    )
    message = record_campaign_message(
        store,
        campaign_id=campaign.campaign_id,
        sender_role="orchestrator",
        sender_task_id=root.operation_id,
        authorized_by=None,
        recipient_task_id=worker.operation_id,
        body="This must remain pending behind the terminal campaign fence.",
    )
    record_structural_failure(
        tasks,
        operation_id=root.operation_id,
        diagnostic="typed structural failure persisted before settlement",
    )
    store.fail_agent_task(root.operation_id, "typed structural failure persisted before settlement")
    before = store.campaign_budget_meter(campaign.campaign_id)

    async def stream(_project_id, _kind, request, _execution):
        assert request.role == "report"
        yield _sse(AgentEvent(event="session", session_id=request.session_id))
        yield _sse(AgentEvent(event="done"))

    tasks.stream = stream
    with TestClient(app):
        pass

    fenced = store.campaign(campaign.campaign_id)
    assert fenced is not None
    assert fenced.status == "wrapping_up"
    assert fenced.ending == "failed"
    assert fenced.error == "typed structural failure persisted before settlement"
    pending = store.campaign_message(message.message_id)
    assert pending is not None
    assert pending.delivered_at is None
    assert pending.delivery_operation_id is None
    campaign_tasks = store.campaign_tasks(campaign.campaign_id)
    assert not any(task.request.get("wake_cause") == "message" for task in campaign_tasks)
    after = store.campaign_budget_meter(campaign.campaign_id)
    assert after.invocations_used == before.invocations_used + 1
    assert len(_campaign_report_tasks(store, campaign.campaign_id)) == 1


def test_any_campaign_task_settlement_reconciles_other_pending_recipients(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=7,
        root_status="succeeded",
        stage_root=str(stage),
    )
    now = store.now()
    worker = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="worker",
            project_id=project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="worker",
                actor_operation_id="worker",
                control_node_id="blk/check-result",
                provider="codex",
                run_on="local",
                run_truth_scope=_RUN_TRUTH_SCOPE,
                session_id="worker-session",
            ).model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            parent_operation_id=root.operation_id,
            native_session_id="worker-session",
            stage_host="execution-host",
            stage_root=str(stage),
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(campaign.campaign_id, "worker"),
        ),
        role="worker",
    )
    message = record_campaign_message(
        store,
        campaign_id=campaign.campaign_id,
        sender_role="orchestrator",
        sender_task_id=root.operation_id,
        authorized_by=None,
        recipient_task_id=worker.operation_id,
        body="Continue after any campaign actor settles.",
    )

    async def stream(_project_id, _kind, request, _execution):
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks.stream = stream
    root_turn = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest.model_validate(root.request),
        parent_operation_id=root.operation_id,
        operation_id="root-continuation",
    )
    assert root_turn is not None

    delivery = _wait_for_message_delivery(store, message.message_id)

    assert delivery.parent_operation_id == worker.operation_id
    assert delivery.request["actor_operation_id"] == worker.operation_id
    assert delivery.request["wake_cause"] == "message"


def test_campaign_settlement_waits_for_child_then_admits_one_report(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=4,
        stage_root=str(stage),
    )
    now = store.now()
    worker = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="worker",
            project_id=project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="running",
            request=CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="worker",
                actor_operation_id="worker",
                control_node_id="exp/check-child",
                provider="codex",
                run_on="local",
                run_truth_scope=_RUN_TRUTH_SCOPE,
                session_id="worker-session",
            ).model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="running",
            parent_operation_id=root.operation_id,
            native_session_id="worker-session",
            stage_root=str(stage),
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(campaign.campaign_id, "worker"),
        ),
        role="worker",
    )
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "completed")
    assert wrapping.status == "wrapping_up"
    tasks.stream = _settling_campaign_stream
    callback = tasks.on_campaign_task_settled
    assert callback is not None

    callback(
        wrapping,
        CampaignRunRequest.model_validate(root.request),
        AgentTaskExecution(root.operation_id, store, AgentProcessControl()),
    )
    assert _campaign_report_tasks(store, campaign.campaign_id) == []
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2

    store.complete_agent_task(worker.operation_id, applied_revision=None, result={})
    current = store.campaign(campaign.campaign_id)
    assert current is not None
    worker_request = CampaignRunRequest.model_validate(worker.request)
    execution = AgentTaskExecution(worker.operation_id, store, AgentProcessControl())
    callback(current, worker_request, execution)
    callback(current, worker_request, execution)

    reports = _campaign_report_tasks(store, campaign.campaign_id)
    assert len(reports) == 1
    assert reports[0].request["instruction"].startswith("Produce the campaign's concluding report")
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 3


def test_last_research_turn_fences_exhaustion_and_admits_only_the_reserved_report(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        stage_root=str(stage),
    )
    tasks.stream = _settling_campaign_stream

    last_research_turn = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest.model_validate(root.request),
        parent_operation_id=root.operation_id,
        operation_id="last-research-turn",
    )
    assert last_research_turn is not None
    wait_for_task(store, last_research_turn.operation_id, expect="succeeded")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _campaign_report_tasks(
        store,
        campaign.campaign_id,
    ):
        time.sleep(0.01)
    current = store.campaign(campaign.campaign_id)
    assert current is not None
    assert current.status == "wrapping_up"
    assert current.ending == "exhausted"
    reports = _campaign_report_tasks(store, campaign.campaign_id)
    assert len(reports) == 1
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 3

    with pytest.raises(CampaignNotRunning):
        tasks.start_campaign_turn(
            campaign.campaign_id,
            CampaignRunRequest.model_validate(root.request),
            parent_operation_id=root.operation_id,
            operation_id="extra-research-turn",
        )
    assert store.agent_task("extra-research-turn") is None


@pytest.mark.parametrize("role", ["orchestrator", "report"])
@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_completed_ending_waits_for_recoverable_actor_failure_before_report_reconciliation(
    manifest,
    tmp_path,
    monkeypatch,
    role: str,
    status: str,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        root_status="running" if role == "orchestrator" else "succeeded",
        stage_root=str(stage),
    )
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "completed")
    task = (
        root
        if role == "orchestrator"
        else _recoverable_report_task(store, campaign, root, status="running")
    )
    store.fail_agent_task(
        task.operation_id,
        "The provider is temporarily unavailable after the completion fence.",
        status=status,
    )
    settled = store.agent_task(task.operation_id)
    assert settled is not None
    reconciliations: list[str] = []
    monkeypatch.setattr(
        tasks,
        "reconcile_campaign_report",
        lambda *_args, **_kwargs: reconciliations.append(task.operation_id),
    )
    callback = tasks.on_campaign_task_settled
    assert callback is not None

    callback(
        wrapping,
        CampaignRunRequest.model_validate(settled.request),
        AgentTaskExecution(settled.operation_id, store, AgentProcessControl()),
    )

    recovery = store.campaign_control_recovery(
        campaign.campaign_id,
        settled.operation_id,
    )
    assert recovery is not None
    assert recovery.status == "pending"
    assert reconciliations == []


@pytest.mark.parametrize("role", ["orchestrator", "report"])
def test_terminal_control_recovery_falls_through_to_one_report_without_restart(
    manifest,
    tmp_path,
    monkeypatch,
    role: str,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        root_status="running" if role == "orchestrator" else "succeeded",
        stage_root=str(stage),
        root_stage_host=None,
    )
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "completed")
    task = (
        root
        if role == "orchestrator"
        else _recoverable_report_task(store, campaign, root, status="failed")
    )
    if role == "orchestrator":
        store.fail_agent_task(task.operation_id, "provider unavailable", status="failed")
    settled = store.agent_task(task.operation_id)
    assert settled is not None
    real_reconcile = tasks.reconcile_campaign_report
    reconciliations: list[str] = []

    def tracked_reconcile(campaign_id, **kwargs):
        reconciliations.append(campaign_id)
        return real_reconcile(campaign_id, **kwargs)

    monkeypatch.setattr(tasks, "reconcile_campaign_report", tracked_reconcile)
    callback = tasks.on_campaign_task_settled
    assert callback is not None
    callback(
        wrapping,
        CampaignRunRequest.model_validate(settled.request),
        AgentTaskExecution(settled.operation_id, store, AgentProcessControl()),
    )

    recovery = store.campaign_control_recovery(campaign.campaign_id, task.operation_id)
    assert recovery is not None and recovery.status == "pending"
    assert reconciliations == []
    for attempt in range(recovery.max_attempts):
        recovery = store.defer_campaign_recovery(
            recovery.recovery_id,
            diagnostic=f"recovery admission failure {attempt + 1}",
        )
    assert recovery.status == "exhausted"

    tasks.stream = _settling_campaign_stream
    callback(
        wrapping,
        CampaignRunRequest.model_validate(settled.request),
        AgentTaskExecution(settled.operation_id, store, AgentProcessControl()),
    )

    reports = _campaign_report_tasks(store, campaign.campaign_id)
    assert len(reports) == 1
    assert reconciliations == [campaign.campaign_id]
    callback(
        wrapping,
        CampaignRunRequest.model_validate(settled.request),
        AgentTaskExecution(settled.operation_id, store, AgentProcessControl()),
    )
    assert len(_campaign_report_tasks(store, campaign.campaign_id)) == 1


@pytest.mark.parametrize("source", ["stop", "exhaustion"])
def test_stop_and_exhaustion_hooks_admit_the_reserved_report_once(
    manifest,
    tmp_path,
    source: str,
) -> None:
    app = create_named_app(
        str(manifest.path),
        data_dir=tmp_path / f"{source}-data",
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / f"{source}-stage"
    stage.mkdir()
    campaign, _root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        stage_root=str(stage),
    )
    tasks.stream = _settling_campaign_stream

    with TestClient(app) as client:
        if source == "stop":
            response = client.post(
                f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/stop"
            )
            assert response.status_code == 200
            ending = "stopped"
        else:
            wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")
            hook = tasks.on_campaign_admission_exhausted
            assert hook is not None
            hook(wrapping)
            hook(wrapping)
            ending = "exhausted"

    reports = _campaign_report_tasks(store, campaign.campaign_id)
    assert len(reports) == 1
    assert reports[0].request["ending"] == ending
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2


def test_stop_after_exhaustion_fence_is_visible_conflict_without_false_stop_event(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id, invocation_ceiling=2)
    _ending_report(store, campaign, root, ending="exhausted")
    events_before = store.agent_task_events(root.operation_id)

    with TestClient(app) as client:
        response = client.post(f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/stop")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "the campaign ending is already durable; Stop was not recorded"
    )
    stored = store.campaign(campaign.campaign_id)
    assert stored is not None
    assert (stored.status, stored.ending) == ("needs_action", "exhausted")
    assert stored.stop_requested_at is None
    assert store.agent_task_events(root.operation_id) == events_before


def test_startup_sweep_reconciles_each_fenced_report_idempotently(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    campaign, _root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        stage_root=str(stage),
    )
    store.begin_campaign_wrapup(campaign.campaign_id, "failed", error="terminal failure")
    tasks.stream = _settling_campaign_stream

    with TestClient(app):
        pass
    with TestClient(app):
        pass

    reports = _campaign_report_tasks(store, campaign.campaign_id)
    assert len(reports) == 1
    assert reports[0].request["ending"] == "failed"
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2


def test_report_reconciliation_failure_never_replaces_stop_response(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    campaign, root = _campaign(store, project_id=project_id)

    def reject_report(*_args, **_kwargs):
        raise RuntimeError("report admission is temporarily unavailable")

    monkeypatch.setattr(tasks, "reconcile_campaign_report", reject_report)
    with TestClient(app) as client:
        response = client.post(f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/stop")

    assert response.status_code == 200
    assert response.json()["status"] == "wrapping_up"
    receipts = store.agent_task_receipts(root.operation_id)
    diagnostic = [
        receipt
        for receipt in receipts
        if receipt.category == "campaign_report_reconciliation_failed"
    ]
    assert len(diagnostic) == 1
    assert diagnostic[0].payload["source"] == "Stop request"


def test_campaign_report_get_and_head_share_exact_sandbox_headers_and_length(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    campaign, root = _campaign(store, project_id=project_id, invocation_ceiling=2)
    report = _ending_report(store, campaign, root)
    client = TestClient(app)
    url = (
        f"/api/projects/{project_id}/campaigns/{campaign.campaign_id}/reports/"
        f"{report.report_id}/preview"
    )

    preview = client.get(url)
    head = client.head(url)

    assert preview.status_code == head.status_code == 200
    assert preview.content
    assert head.content == b""
    expected_length = str(len(preview.content))
    assert preview.headers["content-length"] == expected_length
    assert head.headers["content-length"] == expected_length
    for response in (preview, head):
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert "frame-src 'self'" in response.headers["content-security-policy"]
    assert 'sandbox="allow-scripts"' in preview.text
    assert "allow-top-navigation" not in preview.text
    assert "rcp-result-view-gesture" not in preview.text
    assert "connect-src &amp;#x27;none&amp;#x27;" in preview.text


@pytest.mark.parametrize(
    ("endpoint", "status"),
    [("resume", "paused"), ("retry", "failed")],
)
def test_report_recovery_keeps_its_exact_package_without_spending_again(
    manifest,
    tmp_path,
    endpoint: str,
    status: str,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / f"{endpoint}-data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    service = app.state.catalog.open(project_id)
    assert "campaign-report" in service.manifest.agent.skill_defaults.skill_ids
    stage = tmp_path / f"{endpoint}-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        stage_root=str(stage),
        root_run_on="laptop",
        root_stage_host=None,
    )
    report = _recoverable_report_task(store, campaign, root, status=status)
    exact_package = report.request["resolved_skill_packages"]
    used_before = store.campaign_budget_meter(campaign.campaign_id).invocations_used
    tasks.stream = _settling_campaign_stream

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/tasks/{report.operation_id}/{endpoint}",
            json={} if endpoint == "retry" else None,
        )

    assert response.status_code == 202, response.text
    recovery = store.agent_task(response.json()["operation_id"])
    assert recovery is not None
    assert recovery.parent_operation_id == report.operation_id
    assert recovery.attempt == report.attempt + 1
    assert recovery.native_session_id == report.native_session_id
    assert recovery.stage_host == report.stage_host
    assert recovery.stage_root == report.stage_root
    assert recovery.request["session_id"] == report.native_session_id
    assert recovery.request["workflow_ids"] == []
    assert recovery.request["skill_ids"] == ["campaign-report"]
    assert recovery.request["invoked_skill_ids"] == ["campaign-report"]
    assert recovery.request["resolved_skill_packages"] == exact_package
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == used_before == 2


@pytest.mark.parametrize(
    "request_updates",
    [
        {"resolved_skill_packages": None},
        {
            "resolved_skill_packages": [
                {"id": "campaign-report", "kind": "skill", "version": "9.9.9"}
            ]
        },
        {"invoked_skill_ids": []},
    ],
)
def test_report_recovery_rejects_missing_stale_or_malformed_package_before_mutation(
    manifest,
    tmp_path,
    request_updates: dict[str, object],
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "invalid-data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "invalid-stage"
    stage.mkdir()
    campaign, root = _campaign(
        store,
        project_id=project_id,
        invocation_ceiling=3,
        stage_root=str(stage),
        root_run_on="laptop",
        root_stage_host=None,
    )
    report = _recoverable_report_task(
        store,
        campaign,
        root,
        status="failed",
        request_updates=request_updates,
    )
    tasks_before = store.campaign_tasks(campaign.campaign_id)
    budget_before = store.campaign_budget_meter(campaign.campaign_id)

    response = TestClient(app).post(
        f"/api/projects/{project_id}/tasks/{report.operation_id}/retry",
        json={},
    )

    assert response.status_code == 409
    assert "exact stored official campaign-report package" in response.json()["detail"]
    assert store.campaign_tasks(campaign.campaign_id) == tasks_before
    assert store.campaign_budget_meter(campaign.campaign_id) == budget_before
