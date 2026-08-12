from __future__ import annotations

import asyncio
import hashlib
import threading

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import AuthorizedHuman
from rcp.runs.campaign import CampaignRunRequest
from rcp.runs.campaign_delivery import record_campaign_message
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    CampaignRecord,
    CampaignReportRecord,
    ProjectRecord,
)

from .helpers import create_named_app, fabricated_authorizer, wait_for_task

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


def _app_authorizer(store: AppStore) -> AuthorizedHuman:
    owner = store.local_owner
    assert owner is not None
    assert owner.display_name is not None
    return AuthorizedHuman(
        space_id=store.space_id,
        user_id=owner.user_id,
        display_name=owner.display_name,
    )


def _store(tmp_path) -> AppStore:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="/tmp/project/research.yaml",
            name="project",
            state_location="/tmp/project/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    return store


def _exhausted_reported_campaign(
    store: AppStore,
    *,
    project_id: str,
    authorized_by: AuthorizedHuman,
    stage_root: str,
    request_updates: dict[str, object] | None = None,
    with_orchestrator_leaf: bool = False,
    pending_human_message_body: str | None = None,
) -> tuple[CampaignRecord, AgentTaskRecord, CampaignReportRecord]:
    now = store.now()
    root_request = CampaignRunRequest(
        campaign_id="campaign",
        role="orchestrator",
        actor_operation_id="root",
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=_RUN_TRUTH_SCOPE,
    ).model_copy(update=request_updates or {})
    campaign, root = store.create_campaign_with_root_task(
        CampaignRecord(
            campaign_id="campaign",
            project_id=project_id,
            status="queued",
            invocation_ceiling=3 if with_orchestrator_leaf else 2,
            authorized_by=authorized_by,
            created_at=now,
            updated_at=now,
        ),
        AgentTaskRecord(
            operation_id="root",
            project_id=project_id,
            campaign_id="campaign",
            kind="campaign",
            status="succeeded",
            request=root_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            native_session_id="orchestrator-session",
            stage_host="execution-host",
            stage_root=stage_root,
            authorized_by=authorized_by,
            dispatch_authority=_campaign_authority("campaign", "orchestrator"),
        ),
    )
    current_orchestrator = root
    if with_orchestrator_leaf:
        current_orchestrator = store.create_campaign_agent_task(
            AgentTaskRecord(
                operation_id="orchestrator-leaf",
                project_id=project_id,
                campaign_id=campaign.campaign_id,
                kind="campaign",
                status="succeeded",
                request=root_request.model_copy(
                    update={"session_id": root.native_session_id}
                ).model_dump(mode="json"),
                created_at=store.now(),
                updated_at=store.now(),
                status_message="done",
                parent_operation_id=root.operation_id,
                native_session_id=root.native_session_id,
                stage_host=root.stage_host,
                stage_root=root.stage_root,
                authorized_by=authorized_by,
                dispatch_authority=_campaign_authority("campaign", "orchestrator"),
            ),
            role="orchestrator",
        )
    if pending_human_message_body is not None:
        record_campaign_message(
            store,
            campaign_id=campaign.campaign_id,
            sender_role="human",
            sender_task_id=None,
            authorized_by=authorized_by,
            recipient_task_id=root.operation_id,
            body=pending_human_message_body,
        )
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")
    report_operation_id = "exhaustion-report"
    report_task = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id=report_operation_id,
            project_id=project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=root_request.model_copy(
                update={
                    "role": "report",
                    "ending": "exhausted",
                    "actor_operation_id": root.operation_id,
                    "session_id": current_orchestrator.native_session_id,
                }
            ).model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="done",
            parent_operation_id=current_orchestrator.operation_id,
            native_session_id=current_orchestrator.native_session_id,
            stage_host=current_orchestrator.stage_host,
            stage_root=current_orchestrator.stage_root,
            authorized_by=authorized_by,
        ),
        role="report",
    )
    html = "<article><h1>Budget exhausted</h1></article>"
    report = CampaignReportRecord(
        report_id="exhaustion-report-artifact",
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="exhausted",
        sha256=hashlib.sha256(html.encode()).hexdigest(),
        html=html,
        created_at=store.now(),
    )
    exhausted, stored_report = store.finish_campaign_wrapup(report)
    assert wrapping.invocations_used == (2 if with_orchestrator_leaf else 1)
    assert exhausted.status == "needs_action"
    assert exhausted.ending == "exhausted"
    assert (
        exhausted.invocation_ceiling
        == exhausted.invocations_used
        == (3 if with_orchestrator_leaf else 2)
    )
    return exhausted, root, stored_report


def _continuation_task(
    store: AppStore,
    campaign: CampaignRecord,
    root: AgentTaskRecord,
    *,
    operation_id: str,
    native_session_id: str = "orchestrator-session",
    role: str = "orchestrator",
) -> AgentTaskRecord:
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role=role,
        actor_operation_id=(root.operation_id if role == "orchestrator" else operation_id),
        control_node_id=(None if role == "orchestrator" else "exp/invalid-seat"),
        provider="codex",
        run_on="local",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        session_id=native_session_id,
    )
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=campaign.project_id,
        campaign_id=campaign.campaign_id,
        kind="campaign",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="queued",
        parent_operation_id=root.operation_id,
        native_session_id=native_session_id,
        stage_host=root.stage_host,
        stage_root=root.stage_root,
        authorized_by=campaign.authorized_by,
        dispatch_authority=_campaign_authority(campaign.campaign_id, role),
    )


def test_storage_reauthorization_atomically_extends_and_spends_one_new_unit(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, root, report = _exhausted_reported_campaign(
        store,
        project_id="project",
        authorized_by=fabricated_authorizer(),
        stage_root=str(stage),
    )
    task = _continuation_task(
        store,
        exhausted,
        root,
        operation_id="reauthorized-continuation",
    )

    reauthorized, admitted = store.reauthorize_campaign_with_task(
        exhausted.campaign_id,
        3,
        task,
    )

    assert reauthorized.status == "running"
    assert reauthorized.ending is None
    assert reauthorized.invocation_ceiling == 5
    assert reauthorized.invocations_used == 3
    assert reauthorized.research_invocations_remaining == 1
    meter = store.campaign_budget_meter(exhausted.campaign_id)
    assert meter.invocation_ceiling == 5
    assert meter.invocations_used == 3
    assert meter.invocations_remaining == 2
    assert meter.report_units_reserved == 1
    assert admitted.operation_id == task.operation_id
    assert admitted.parent_operation_id == root.operation_id
    assert admitted.request["actor_operation_id"] == root.operation_id
    assert admitted.request["role"] == "orchestrator"
    assert admitted.native_session_id == root.native_session_id
    assert admitted.stage_host == root.stage_host
    assert admitted.stage_root == root.stage_root
    assert store.campaign_invocation_role(admitted.operation_id) == "orchestrator"
    assert store.campaign_reports(exhausted.campaign_id) == [report]


@pytest.mark.parametrize(
    ("role", "native_session_id", "diagnostic"),
    [
        ("orchestrator", "wrong-session", "session and stage"),
        ("worker", "orchestrator-session", "continue the orchestrator"),
    ],
)
def test_storage_reauthorization_rolls_back_extension_when_task_admission_is_invalid(
    tmp_path,
    role: str,
    native_session_id: str,
    diagnostic: str,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, root, _report = _exhausted_reported_campaign(
        store,
        project_id="project",
        authorized_by=fabricated_authorizer(),
        stage_root=str(stage),
    )
    before_campaign = store.campaign(exhausted.campaign_id)
    before_meter = store.campaign_budget_meter(exhausted.campaign_id)
    before_tasks = store.campaign_tasks(exhausted.campaign_id)
    invalid = _continuation_task(
        store,
        exhausted,
        root,
        operation_id="invalid-reauthorization",
        native_session_id=native_session_id,
        role=role,
    )

    with pytest.raises(ValueError, match=diagnostic):
        store.reauthorize_campaign_with_task(exhausted.campaign_id, 3, invalid)

    assert store.campaign(exhausted.campaign_id) == before_campaign
    assert store.campaign_budget_meter(exhausted.campaign_id) == before_meter
    assert store.campaign_tasks(exhausted.campaign_id) == before_tasks
    assert store.agent_task(invalid.operation_id) is None


def test_background_reauthorization_resumes_non_report_orchestrator_leaf_after_report(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, root, report = _exhausted_reported_campaign(
        store,
        project_id="project",
        authorized_by=fabricated_authorizer(),
        stage_root=str(stage),
        with_orchestrator_leaf=True,
    )
    orchestrator_leaf = store.agent_task("orchestrator-leaf")
    assert orchestrator_leaf is not None
    report_binding = store.campaign_actor_binding(report.operation_id)
    assert report_binding.actor_operation_id == root.operation_id
    assert report_binding.role == "orchestrator"
    assert report_binding.current_operation_id == orchestrator_leaf.operation_id
    assert report_binding.native_session_id == orchestrator_leaf.native_session_id
    assert report_binding.stage_host == orchestrator_leaf.stage_host
    assert report_binding.stage_root == orchestrator_leaf.stage_root
    observed: list[tuple[str, str, str | None, str | None, str | None]] = []

    async def stream(_project_id, _kind, request, execution):
        observed.append(
            (
                execution.operation_id,
                execution.continuation,
                request.session_id,
                execution.stage_host,
                execution.stage_root,
            )
        )
        yield _sse(AgentEvent(event="session", session_id=request.session_id))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    before_tasks = store.campaign_tasks(exhausted.campaign_id)
    preflight_requests: list[CampaignRunRequest] = []

    def preflight(request: CampaignRunRequest) -> CampaignRunRequest:
        preflight_requests.append(request)
        return request.model_copy(update={"workflow_ids": []})

    reauthorized, continuation = tasks.reauthorize_campaign(
        exhausted.campaign_id,
        3,
        request_preflight=preflight,
        operation_id="reauthorized-continuation",
    )
    continuation = wait_for_task(store, continuation.operation_id, expect="succeeded")

    assert reauthorized.status == "running"
    assert reauthorized.ending is None
    assert reauthorized.invocation_ceiling == 6
    assert reauthorized.invocations_used == 4
    assert reauthorized.research_invocations_remaining == 1
    meter = store.campaign_budget_meter(exhausted.campaign_id)
    assert meter.invocations_used == 4
    assert meter.invocations_remaining == 2
    assert meter.report_units_reserved == 1
    assert len(store.campaign_tasks(exhausted.campaign_id)) == len(before_tasks) + 1
    assert continuation.parent_operation_id == orchestrator_leaf.operation_id
    assert continuation.request["actor_operation_id"] == root.operation_id
    assert continuation.request["role"] == "orchestrator"
    assert continuation.request["workflow_ids"] == []
    assert continuation.native_session_id == root.native_session_id
    assert continuation.stage_host == root.stage_host
    assert continuation.stage_root == root.stage_root
    assert store.agent_task_continuation_cause(continuation.operation_id) == (
        "campaign_continuation"
    )
    assert store.campaign_reports(exhausted.campaign_id) == [report]
    assert observed == [
        (
            continuation.operation_id,
            "campaign_continuation",
            "orchestrator-session",
            "execution-host",
            str(stage),
        )
    ]
    assert len(preflight_requests) == 1
    assert preflight_requests[0].model == ""
    assert preflight_requests[0].reasoning == "medium"


def test_background_reauthorization_preflight_failure_mutates_nothing(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, _root, _report = _exhausted_reported_campaign(
        store,
        project_id="project",
        authorized_by=fabricated_authorizer(),
        stage_root=str(stage),
    )

    async def stream(_project_id, _kind, _request, _execution):
        raise AssertionError("preflight failure must not spawn")
        yield  # pragma: no cover

    tasks = BackgroundAgentTasks(store, stream)
    before_campaign = store.campaign(exhausted.campaign_id)
    before_meter = store.campaign_budget_meter(exhausted.campaign_id)
    before_tasks = store.campaign_tasks(exhausted.campaign_id)

    def refuse(_request: CampaignRunRequest) -> CampaignRunRequest:
        raise ValueError("saved execution profile is unavailable")

    with pytest.raises(ValueError, match="saved execution profile"):
        tasks.reauthorize_campaign(
            exhausted.campaign_id,
            3,
            request_preflight=refuse,
            operation_id="refused-reauthorization",
        )

    assert store.campaign(exhausted.campaign_id) == before_campaign
    assert store.campaign_budget_meter(exhausted.campaign_id) == before_meter
    assert store.campaign_tasks(exhausted.campaign_id) == before_tasks
    assert store.agent_task("refused-reauthorization") is None


def test_reauthorize_api_starts_pinned_continuation_and_returns_updated_campaign(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, root, report = _exhausted_reported_campaign(
        store,
        project_id=project_id,
        authorized_by=_app_authorizer(store),
        stage_root=str(stage),
    )
    observed: list[tuple[str, str, str | None]] = []

    async def stream(_project_id, _kind, request, execution):
        observed.append((execution.operation_id, execution.continuation, request.session_id))
        yield _sse(AgentEvent(event="session", session_id=request.session_id))
        yield _sse(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/campaigns/{exhausted.campaign_id}/reauthorize",
            json={"additional_invocations": 3},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["campaign_id"] == exhausted.campaign_id
    assert payload["project_id"] == project_id
    assert payload["root_operation_id"] == root.operation_id
    assert payload["status"] == "running"
    assert payload["ending"] is None
    assert payload["can_reauthorize"] is False
    assert payload["can_stop"] is True
    assert payload["budget"] == {
        "invocation_ceiling": 5,
        "invocations_used": 3,
        "invocations_remaining": 2,
        "report_units_reserved": 1,
        "observed_input_tokens": 0,
        "observed_generated_tokens": 0,
    }
    assert payload["reports"] == [
        {
            "report_id": report.report_id,
            "ending": "exhausted",
            "created_at": report.created_at,
        }
    ]
    continuation = next(
        task
        for task in store.campaign_tasks(exhausted.campaign_id)
        if task.operation_id not in {root.operation_id, report.operation_id}
    )
    continuation = wait_for_task(store, continuation.operation_id, expect="succeeded")
    assert continuation.parent_operation_id == root.operation_id
    assert continuation.request["actor_operation_id"] == root.operation_id
    assert continuation.native_session_id == root.native_session_id
    assert continuation.stage_host == root.stage_host
    assert continuation.stage_root == root.stage_root
    assert store.campaign_invocation_role(continuation.operation_id) == "orchestrator"
    assert store.agent_task_continuation_cause(continuation.operation_id) == (
        "campaign_continuation"
    )
    assert observed == [
        (continuation.operation_id, "campaign_continuation", "orchestrator-session")
    ]


@pytest.mark.parametrize(
    ("request_updates", "diagnostic"),
    [
        ({"run_on": "missing-machine"}, "unknown execution machine"),
        ({"reasoning": None}, "exact pinned orchestrator execution profile"),
        ({"invoked_skill_ids": ["missing-skill"]}, "not available"),
    ],
)
def test_reauthorize_api_preflight_refusal_leaves_campaign_and_tasks_unchanged(
    manifest,
    tmp_path,
    request_updates: dict[str, object],
    diagnostic: str,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, _root, _report = _exhausted_reported_campaign(
        store,
        project_id=project_id,
        authorized_by=_app_authorizer(store),
        stage_root=str(stage),
        request_updates=request_updates,
    )
    before_campaign = store.campaign(exhausted.campaign_id)
    before_meter = store.campaign_budget_meter(exhausted.campaign_id)
    before_tasks = store.campaign_tasks(exhausted.campaign_id)

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/campaigns/{exhausted.campaign_id}/reauthorize",
            json={"additional_invocations": 3},
        )

    assert response.status_code == 409
    assert diagnostic in response.text
    assert store.campaign(exhausted.campaign_id) == before_campaign
    assert store.campaign_budget_meter(exhausted.campaign_id) == before_meter
    assert store.campaign_tasks(exhausted.campaign_id) == before_tasks


def test_reauthorize_api_unwritable_history_leaves_campaign_and_tasks_unchanged(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, _root, _report = _exhausted_reported_campaign(
        store,
        project_id=project_id,
        authorized_by=_app_authorizer(store),
        stage_root=str(stage),
    )
    before_campaign = store.campaign(exhausted.campaign_id)
    before_meter = store.campaign_budget_meter(exhausted.campaign_id)
    before_tasks = store.campaign_tasks(exhausted.campaign_id)
    service = app.state.catalog.open(project_id)

    def refuse_writes():
        raise ValueError("canonical history is read-only")

    monkeypatch.setattr(service.history, "require_writable", refuse_writes)
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/campaigns/{exhausted.campaign_id}/reauthorize",
            json={"additional_invocations": 3},
        )

    assert response.status_code == 409
    assert "canonical history is read-only" in response.text
    assert store.campaign(exhausted.campaign_id) == before_campaign
    assert store.campaign_budget_meter(exhausted.campaign_id) == before_meter
    assert store.campaign_tasks(exhausted.campaign_id) == before_tasks


def test_reauthorize_api_reconciles_pending_mail_before_the_root_turn_settles(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    stage = tmp_path / "campaign-stage"
    stage.mkdir()
    exhausted, root, _report = _exhausted_reported_campaign(
        store,
        project_id=project_id,
        authorized_by=_app_authorizer(store),
        stage_root=str(stage),
        pending_human_message_body="Keep this pending while the reauthorized root is busy.",
    )
    [message] = store.campaign_messages(exhausted.campaign_id)
    root_entered = threading.Event()
    release_root = threading.Event()

    async def stream(_project_id, _kind, request, _execution):
        root_entered.set()
        while not release_root.is_set():
            await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="session", session_id=request.session_id))
        yield _sse(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    with TestClient(app) as client:
        import rcp.api.app as app_module

        reconciled: list[str | None] = []
        real_reconcile = app_module.reconcile_pending_campaign_mail

        def observe_reconciliation(background, *, campaign_id=None):
            reconciled.append(campaign_id)
            return real_reconcile(background, campaign_id=campaign_id)

        monkeypatch.setattr(app_module, "reconcile_pending_campaign_mail", observe_reconciliation)
        try:
            response = client.post(
                f"/api/projects/{project_id}/campaigns/{exhausted.campaign_id}/reauthorize",
                json={"additional_invocations": 3},
            )
            assert root_entered.wait(timeout=2)
            assert response.status_code == 200
            assert reconciled == [exhausted.campaign_id]
            assert store.pending_campaign_messages(
                exhausted.campaign_id,
                root.operation_id,
            ) == [message]
        finally:
            release_root.set()

    continuation = next(
        task
        for task in store.campaign_tasks(exhausted.campaign_id)
        if task.operation_id not in {root.operation_id, "exhaustion-report"}
    )
    wait_for_task(store, continuation.operation_id, expect="succeeded")
