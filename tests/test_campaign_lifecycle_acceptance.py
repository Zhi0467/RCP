from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.agents.acceptance import (
    ACCEPTANCE_CAMPAIGN_EXHAUST_MARKER,
    ACCEPTANCE_CAMPAIGN_FAIL_MARKER,
    ACCEPTANCE_CAMPAIGN_INTERRUPT_ACTIVE_FILE,
    ACCEPTANCE_CAMPAIGN_REAUTHORIZED_ACTIVE_FILE,
    ACCEPTANCE_CAMPAIGN_REAUTHORIZED_RELEASE_FILE,
    ACCEPTANCE_CAMPAIGN_SPAWN_THEN_FINISH_MARKER,
    ACCEPTANCE_CAMPAIGN_SPAWN_THEN_INTERRUPT_MARKER,
    ACCEPTANCE_CAMPAIGN_STOP_MARKER,
)
from rcp.core.models import Patch
from rcp.runs.campaign import CampaignRunRequest
from rcp.storage import (
    CampaignNotRunning,
    GraphWatcherRecord,
    WatcherContinuation,
)

from .helpers import append_fixture_patch, create_named_app, wait_for_task


def _wait_for_reported_campaign(
    client: TestClient,
    project_id: str,
    campaign_id: str,
    *,
    status: str,
    ending: str,
) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(f"/api/projects/{project_id}/campaigns")
        assert response.status_code == 200, response.text
        campaign = next(item for item in response.json() if item["campaign_id"] == campaign_id)
        if campaign["status"] == status and campaign["ending"] == ending and campaign["reports"]:
            return campaign
        time.sleep(0.01)
    raise AssertionError("acceptance campaign did not finish with its report")


def _wait_for_path(path: Path, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.01)
    raise AssertionError(f"acceptance fixture path did not appear: {path}")


def _wait_for_task_stage(store, operation_id: str, *, timeout: float = 10) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = store.agent_task(operation_id)
        if task is not None and task.stage_root is not None:
            return Path(task.stage_root)
        time.sleep(0.01)
    raise AssertionError(f"acceptance task did not persist its stage: {operation_id}")


def _wait_for_task_message(
    store,
    operation_id: str,
    message: str,
    *,
    timeout: float = 10,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = store.agent_task(operation_id)
        if task is not None and task.status_message == message:
            return task
        time.sleep(0.01)
    raise AssertionError(
        f"acceptance task {operation_id} did not publish its visible message: {message}"
    )


def _wait_for_campaign_role_task(store, campaign_id: str, role: str, *, timeout: float = 10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for task in store.campaign_tasks(campaign_id):
            if store.campaign_invocation_role(task.operation_id) == role:
                return task
        time.sleep(0.01)
    raise AssertionError(f"acceptance campaign did not admit its {role} task")


def _wait_for_campaign_state(
    client: TestClient,
    project_id: str,
    campaign_id: str,
    *,
    status: str,
    ending: str,
    timeout: float = 20,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/projects/{project_id}/campaigns")
        assert response.status_code == 200, response.text
        campaign = next(item for item in response.json() if item["campaign_id"] == campaign_id)
        if campaign["status"] == status and campaign["ending"] == ending:
            return campaign
        time.sleep(0.01)
    raise AssertionError(f"acceptance campaign did not reach {status}/{ending} before its deadline")


def test_acceptance_campaign_spawns_deduplicates_finishes_and_corrects_one_report(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(
        str(manifest.path),
        data_dir=tmp_path / "data",
        acceptance_agent=True,
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    service = app.state.catalog.open(project_id)
    append_fixture_patch(
        service,
        Patch(
            kind="seed",
            author="agent",
            summary="Added the bounded acceptance worker seat.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "exp/campaign-acceptance",
                            "type": "experiment",
                            "title": "Campaign acceptance worker",
                            "objective": "Complete one deterministic bounded worker turn.",
                            "status": "designing",
                        }
                    ],
                }
            ],
        ),
    )
    store = app.state.background_tasks.store

    with TestClient(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 4,
                "starting_instruction": ACCEPTANCE_CAMPAIGN_SPAWN_THEN_FINISH_MARKER,
            },
        )
        assert started.status_code == 202, started.text
        campaign_id = started.json()["campaign_id"]
        campaign = _wait_for_reported_campaign(
            client,
            project_id,
            campaign_id,
            status="succeeded",
            ending="completed",
        )

        assert campaign["ending"] == "completed"
        assert campaign["budget"]["invocation_ceiling"] == 4
        assert campaign["budget"]["invocations_used"] == 4
        assert len(campaign["reports"]) == 1
        report_id = campaign["reports"][0]["report_id"]
        preview = client.get(
            f"/api/projects/{project_id}/campaigns/{campaign_id}/reports/{report_id}/preview"
        )
        assert preview.status_code == 200, preview.text
        assert "Acceptance campaign conclusion" in preview.text

    tasks = store.campaign_tasks(campaign_id)
    roles = [store.campaign_invocation_role(task.operation_id) for task in tasks]
    assert roles.count("orchestrator") == 2
    assert roles.count("worker") == 1
    assert roles.count("report") == 1
    root = next(task for task in tasks if task.parent_operation_id is None)
    report = next(
        task for task in tasks if store.campaign_invocation_role(task.operation_id) == "report"
    )
    assert report.native_session_id == root.native_session_id
    assert report.stage_host == root.stage_host
    assert report.stage_root == root.stage_root
    report_launches = [
        receipt
        for receipt in store.agent_task_receipts(report.operation_id)
        if receipt.category == "agent_launch"
    ]
    assert len(report_launches) == 2
    assert [receipt.payload["continuation_cause"] for receipt in report_launches] == [
        "campaign_continuation",
        "campaign_report_correction",
    ]
    assert all(receipt.payload["resumed"] is True for receipt in report_launches)
    assert store.agent_command_by_key(campaign_id, "acceptance-spawn") is not None
    assert store.agent_command_by_key(campaign_id, "acceptance-finish-after-worker") is not None
    assert len(store.campaign_reports(campaign_id)) == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_acceptance_campaign_restart_retry_reuses_the_successful_spawn(
    manifest,
    tmp_path,
) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(
        str(manifest.path),
        data_dir=data_dir,
        acceptance_agent=True,
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    service = app.state.catalog.open(project_id)
    append_fixture_patch(
        service,
        Patch(
            kind="seed",
            author="agent",
            summary="Added the interrupted-spawn acceptance worker seat.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "exp/campaign-interrupted-spawn",
                            "type": "experiment",
                            "title": "Campaign interrupted spawn",
                            "objective": "Prove a successful worker spawn is never repeated.",
                            "status": "designing",
                        }
                    ],
                }
            ],
        ),
    )
    store = app.state.background_tasks.store

    with TestClient(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 10,
                "starting_instruction": ACCEPTANCE_CAMPAIGN_SPAWN_THEN_INTERRUPT_MARKER,
            },
        )
        assert started.status_code == 202, started.text
        campaign_id = started.json()["campaign_id"]
        root_operation_id = started.json()["root_operation_id"]
        root_stage = _wait_for_task_stage(store, root_operation_id)
        active_path = root_stage / ACCEPTANCE_CAMPAIGN_INTERRUPT_ACTIVE_FILE
        _wait_for_path(active_path)
        worker = _wait_for_campaign_role_task(store, campaign_id, "worker")
        wait_for_task(store, worker.operation_id, expect="succeeded")
        root_before_restart = store.agent_task(root_operation_id)
        assert root_before_restart is not None
        assert root_before_restart.status == "running"
        assert root_before_restart.native_session_id is not None

    abandoned = store.agent_task(root_operation_id)
    assert abandoned is not None and abandoned.status == "pausing"
    assert active_path.is_file()

    restarted = create_named_app(
        str(manifest.path),
        data_dir=data_dir,
        acceptance_agent=True,
    )
    restarted_store = restarted.state.background_tasks.store
    interrupted = restarted_store.agent_task(root_operation_id)
    assert interrupted is not None and interrupted.status == "interrupted"

    with TestClient(restarted) as client:
        retried = client.post(f"/api/projects/{project_id}/tasks/{root_operation_id}/retry")
        assert retried.status_code == 202, retried.text
        retry_operation_id = retried.json()["operation_id"]
        campaign = _wait_for_reported_campaign(
            client,
            project_id,
            campaign_id,
            status="succeeded",
            ending="completed",
        )

    assert not active_path.exists()
    tasks = restarted_store.campaign_tasks(campaign_id)
    roles = [restarted_store.campaign_invocation_role(task.operation_id) for task in tasks]
    assert roles.count("orchestrator") == 2
    assert roles.count("worker") == 1
    assert roles.count("report") == 1
    assert campaign["budget"]["invocations_used"] == 3
    assert campaign["budget"]["invocations_remaining"] == 7
    retry = restarted_store.agent_task(retry_operation_id)
    report = next(
        task
        for task in tasks
        if restarted_store.campaign_invocation_role(task.operation_id) == "report"
    )
    assert retry is not None and retry.status == "succeeded"
    assert retry.parent_operation_id == root_operation_id
    assert retry.native_session_id == interrupted.native_session_id
    assert retry.stage_host == interrupted.stage_host
    assert retry.stage_root == interrupted.stage_root == str(root_stage)
    assert report.native_session_id == interrupted.native_session_id
    assert report.stage_host == interrupted.stage_host
    assert report.stage_root == interrupted.stage_root
    spawn = restarted_store.agent_command_by_key(campaign_id, "acceptance-interrupt-spawn")
    assert spawn is not None and spawn.status == "ok"
    assert spawn.operation_id == root_operation_id
    spawn_events = [
        event
        for event in restarted_store.agent_task_events(root_operation_id)
        if event.command_id == spawn.command_id
    ]
    assert [event.command_phase for event in spawn_events] == ["start", "exit"]
    assert (
        restarted_store.agent_command_by_key(
            campaign_id,
            "acceptance-finish-after-interrupt",
        )
        is not None
    )
    assert len(restarted_store.campaign_reports(campaign_id)) == 1


def test_acceptance_campaign_exhausts_one_pot_after_admitted_work_then_reports(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(
        str(manifest.path),
        data_dir=tmp_path / "data",
        acceptance_agent=True,
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    service = app.state.catalog.open(project_id)
    append_fixture_patch(
        service,
        Patch(
            kind="seed",
            author="agent",
            summary="Added the exhaustion admission probe seat.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "exp/campaign-exhaustion",
                            "type": "experiment",
                            "title": "Campaign exhaustion probe",
                            "objective": "Prove no worker is admitted after the pot is empty.",
                            "status": "designing",
                        }
                    ],
                }
            ],
        ),
    )
    store = app.state.background_tasks.store

    with TestClient(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 2,
                "starting_instruction": ACCEPTANCE_CAMPAIGN_EXHAUST_MARKER,
            },
        )
        assert started.status_code == 202, started.text
        campaign_id = started.json()["campaign_id"]
        campaign = _wait_for_reported_campaign(
            client,
            project_id,
            campaign_id,
            status="needs_action",
            ending="exhausted",
        )

        assert campaign["budget"]["invocations_used"] == 2
        assert campaign["budget"]["invocations_remaining"] == 0
        assert campaign["can_reauthorize"] is True
        report_id = campaign["reports"][0]["report_id"]
        preview = client.get(
            f"/api/projects/{project_id}/campaigns/{campaign_id}/reports/{report_id}/preview"
        )
        assert preview.status_code == 200, preview.text
        assert "shared invocation pot" in preview.text

    tasks = store.campaign_tasks(campaign_id)
    assert [store.campaign_invocation_role(task.operation_id) for task in tasks].count(
        "orchestrator"
    ) == 1
    assert [store.campaign_invocation_role(task.operation_id) for task in tasks].count(
        "report"
    ) == 1
    root = next(task for task in tasks if task.parent_operation_id is None)
    report = next(
        task for task in tasks if store.campaign_invocation_role(task.operation_id) == "report"
    )
    assert root.status == "succeeded"
    assert report.native_session_id == root.native_session_id
    assert report.stage_root == root.stage_root
    report_launches = [
        receipt
        for receipt in store.agent_task_receipts(report.operation_id)
        if receipt.category == "agent_launch"
    ]
    assert [receipt.payload["correction_round"] for receipt in report_launches] == [0, 1]
    exhaustion_probe = store.agent_command_by_key(campaign_id, "acceptance-exhaustion-probe")
    assert exhaustion_probe is not None and exhaustion_probe.status == "invalid"
    assert len(store.campaign_reports(campaign_id)) == 1


def test_acceptance_exhausted_campaign_reauthorizes_stops_and_reports_again(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(
        str(manifest.path),
        data_dir=tmp_path / "data",
        acceptance_agent=True,
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    service = app.state.catalog.open(project_id)
    append_fixture_patch(
        service,
        Patch(
            kind="seed",
            author="agent",
            summary="Added the reauthorized exhaustion acceptance seat.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "exp/campaign-exhaustion-stop",
                            "type": "experiment",
                            "title": "Campaign exhaustion then Stop",
                            "objective": "Exercise one reauthorized campaign-level Stop.",
                            "status": "designing",
                        }
                    ],
                }
            ],
        ),
    )
    store = app.state.background_tasks.store

    with TestClient(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 2,
                "starting_instruction": ACCEPTANCE_CAMPAIGN_EXHAUST_MARKER,
            },
        )
        assert started.status_code == 202, started.text
        campaign_id = started.json()["campaign_id"]
        root_operation_id = started.json()["root_operation_id"]
        first = _wait_for_reported_campaign(
            client,
            project_id,
            campaign_id,
            status="needs_action",
            ending="exhausted",
        )
        assert first["budget"]["invocations_used"] == 2
        assert len(first["reports"]) == 1

        reauthorized = client.post(
            f"/api/projects/{project_id}/campaigns/{campaign_id}/reauthorize",
            json={"additional_invocations": 2},
        )
        assert reauthorized.status_code == 200, reauthorized.text
        assert reauthorized.json()["status"] == "running"
        root_stage = _wait_for_task_stage(store, root_operation_id)
        active_path = root_stage / ACCEPTANCE_CAMPAIGN_REAUTHORIZED_ACTIVE_FILE
        release_path = root_stage / ACCEPTANCE_CAMPAIGN_REAUTHORIZED_RELEASE_FILE
        binding = store.campaign_actor_binding(root_operation_id)
        continuation = _wait_for_task_message(
            store,
            binding.current_operation_id,
            "Agent task is running.",
        )
        assert continuation.status == "running"
        assert active_path.is_file()

        try:
            stopped = client.post(f"/api/projects/{project_id}/campaigns/{campaign_id}/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "stopping"
            assert store.agent_task(continuation.operation_id).status == "running"  # type: ignore[union-attr]
        finally:
            release_path.write_text("release reauthorized turn after Stop\n", encoding="utf-8")

        campaign = _wait_for_reported_campaign(
            client,
            project_id,
            campaign_id,
            status="stopped",
            ending="stopped",
        )
        assert campaign["campaign_id"] == campaign_id
        assert campaign["budget"]["invocation_ceiling"] == 4
        assert campaign["budget"]["invocations_used"] == 4
        assert campaign["budget"]["invocations_remaining"] == 0
        assert campaign["budget"]["report_units_reserved"] == 1
        assert [report["ending"] for report in campaign["reports"]] == [
            "exhausted",
            "stopped",
        ]
        second_preview = client.get(
            f"/api/projects/{project_id}/campaigns/{campaign_id}/reports/"
            f"{campaign['reports'][1]['report_id']}/preview"
        )
        assert second_preview.status_code == 200, second_preview.text
        assert "Human Stop was persisted" in second_preview.text

    tasks = store.campaign_tasks(campaign_id)
    roles = [store.campaign_invocation_role(task.operation_id) for task in tasks]
    assert roles.count("orchestrator") == 2
    assert roles.count("report") == 2
    root = store.agent_task(root_operation_id)
    assert root is not None
    reports = [
        task for task in tasks if store.campaign_invocation_role(task.operation_id) == "report"
    ]
    assert continuation.parent_operation_id == root.operation_id
    assert continuation.native_session_id == root.native_session_id
    assert continuation.stage_host == root.stage_host
    assert continuation.stage_root == root.stage_root
    assert all(report.native_session_id == root.native_session_id for report in reports)
    assert all(report.stage_host == root.stage_host for report in reports)
    assert all(report.stage_root == root.stage_root for report in reports)
    assert not active_path.exists()
    assert not release_path.exists()
    assert [report.ending for report in store.campaign_reports(campaign_id)] == [
        "exhausted",
        "stopped",
    ]


def test_acceptance_campaign_stop_persists_while_turn_active_then_settles_and_reports(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(
        str(manifest.path),
        data_dir=tmp_path / "data",
        acceptance_agent=True,
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store

    with TestClient(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 10,
                "starting_instruction": ACCEPTANCE_CAMPAIGN_STOP_MARKER,
            },
        )
        assert started.status_code == 202, started.text
        started_payload = started.json()
        campaign_id = started_payload["campaign_id"]
        root_operation_id = started_payload["root_operation_id"]
        root_stage = _wait_for_task_stage(store, root_operation_id)
        active_path = root_stage / ".rcp-acceptance-campaign-active"
        release_path = root_stage / ".rcp-acceptance-campaign-release"
        _wait_for_path(active_path)
        assert store.agent_task(root_operation_id).status == "running"  # type: ignore[union-attr]

        try:
            stopped = client.post(f"/api/projects/{project_id}/campaigns/{campaign_id}/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "stopping"
            assert stopped.json()["stop_requested_at"] is not None
            assert store.agent_task(root_operation_id).status == "running"  # type: ignore[union-attr]
        finally:
            release_path.write_text("release after durable Stop\n", encoding="utf-8")

        campaign = _wait_for_reported_campaign(
            client,
            project_id,
            campaign_id,
            status="stopped",
            ending="stopped",
        )
        assert campaign["budget"]["invocations_used"] == 2
        assert campaign["stop_requested_at"] == stopped.json()["stop_requested_at"]
        report_id = campaign["reports"][0]["report_id"]
        preview = client.get(
            f"/api/projects/{project_id}/campaigns/{campaign_id}/reports/{report_id}/preview"
        )
        assert preview.status_code == 200, preview.text
        assert "Human Stop was persisted" in preview.text

    tasks = store.campaign_tasks(campaign_id)
    root = store.agent_task(root_operation_id)
    assert root is not None and root.status == "succeeded"
    report = next(
        task for task in tasks if store.campaign_invocation_role(task.operation_id) == "report"
    )
    assert report.native_session_id == root.native_session_id
    assert report.stage_root == root.stage_root
    report_launches = [
        receipt
        for receipt in store.agent_task_receipts(report.operation_id)
        if receipt.category == "agent_launch"
    ]
    assert [receipt.payload["correction_round"] for receipt in report_launches] == [0, 1]
    assert not active_path.exists()
    assert not release_path.exists()
    assert len(store.campaign_reports(campaign_id)) == 1


def test_acceptance_campaign_unrecoverable_failure_waits_retains_and_reports_once(
    manifest,
    tmp_path,
) -> None:
    app = create_named_app(
        str(manifest.path),
        data_dir=tmp_path / "data",
        acceptance_agent=True,
    )
    project_id = app.state.default_project_id
    assert project_id is not None
    service = app.state.catalog.open(project_id)
    append_fixture_patch(
        service,
        Patch(
            kind="seed",
            author="agent",
            summary="Added the terminal-failure acceptance worker seat.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "exp/campaign-terminal-failure",
                            "type": "experiment",
                            "title": "Campaign terminal failure worker",
                            "objective": "Settle before the partial campaign report is exposed.",
                            "status": "designing",
                        }
                    ],
                }
            ],
        ),
    )
    background = app.state.background_tasks
    store = background.store
    root_release_path: Path | None = None
    worker_release_path: Path | None = None

    with TestClient(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/campaigns",
            json={
                "invocation_ceiling": 10,
                "starting_instruction": ACCEPTANCE_CAMPAIGN_FAIL_MARKER,
            },
        )
        assert started.status_code == 202, started.text
        campaign_id = started.json()["campaign_id"]
        root_operation_id = started.json()["root_operation_id"]
        root_stage = _wait_for_task_stage(store, root_operation_id)
        root_active_path = root_stage / ".rcp-acceptance-campaign-failure-active"
        root_release_path = root_stage / ".rcp-acceptance-campaign-failure-release"
        _wait_for_path(root_active_path)

        worker = _wait_for_campaign_role_task(store, campaign_id, "worker")
        worker_stage = _wait_for_task_stage(store, worker.operation_id)
        worker_active_path = worker_stage / ".rcp-acceptance-campaign-worker-active"
        worker_release_path = worker_stage / ".rcp-acceptance-campaign-worker-release"
        _wait_for_path(worker_active_path)
        assert store.agent_task(root_operation_id).status == "running"  # type: ignore[union-attr]
        assert store.agent_task(worker.operation_id).status == "running"  # type: ignore[union-attr]

        retained_body = "Retain this human guidance in the partial failure report."
        retained = client.post(
            f"/api/projects/{project_id}/campaigns/{campaign_id}/messages",
            json={"body": retained_body},
        )
        assert retained.status_code == 201, retained.text
        assert retained.json()["delivered_at"] is None

        watcher = store.create_watchers(
            [
                GraphWatcherRecord(
                    watcher_id="acceptance-terminal-failure-watcher",
                    project_id=project_id,
                    origin_operation_id=root_operation_id,
                    origin_task_kind="campaign",
                    chat_id=root_operation_id,
                    continuation=WatcherContinuation(
                        provider="codex",
                        run_on="local",
                        run_truth_scope=["repo-a"],
                        patch_kind="work",
                    ),
                    condition={
                        "node_id": "exp/campaign-terminal-failure",
                        "status_in": ["running"],
                    },
                    armed_revision=1,
                    created_at=store.now(),
                )
            ]
        )[0]
        assert watcher.status == "active"

        try:
            root_release_path.write_text("release terminal failure\n", encoding="utf-8")
            wrapping = _wait_for_campaign_state(
                client,
                project_id,
                campaign_id,
                status="wrapping_up",
                ending="failed",
            )
            assert wrapping["reports"] == []
            assert store.agent_task(worker.operation_id).status == "running"  # type: ignore[union-attr]
            stopped_watcher = store.watcher(watcher.watcher_id)
            assert stopped_watcher is not None
            assert stopped_watcher.status == "stopped"
            assert stopped_watcher.notified is True
            assert stopped_watcher.stopped_by == "loop"

            messages_before = store.campaign_messages(campaign_id)
            rejected_message = client.post(
                f"/api/projects/{project_id}/campaigns/{campaign_id}/messages",
                json={"body": "This must not become new terminal work."},
            )
            rejected_message_status = rejected_message.status_code
            messages_after_terminal_attempt = store.campaign_messages(campaign_id)

            root = store.agent_task(root_operation_id)
            assert root is not None and root.status == "failed"
            denied_request = CampaignRunRequest.model_validate(root.request).model_copy(
                update={
                    "instruction": "This continuation must not be admitted.",
                    "session_id": root.native_session_id,
                    "ending": None,
                }
            )
            meter_before = store.campaign_budget_meter(campaign_id)
            task_ids_before = [task.operation_id for task in store.campaign_tasks(campaign_id)]
            with pytest.raises(CampaignNotRunning, match="not admitting new work"):
                background.start_campaign_turn(
                    campaign_id,
                    denied_request,
                    parent_operation_id=root_operation_id,
                )
            assert store.campaign_budget_meter(campaign_id) == meter_before
            assert [
                task.operation_id for task in store.campaign_tasks(campaign_id)
            ] == task_ids_before

            worker_release_path.write_text("settle admitted worker\n", encoding="utf-8")
            campaign = _wait_for_reported_campaign(
                client,
                project_id,
                campaign_id,
                status="failed",
                ending="failed",
            )
        finally:
            if root_active_path.exists():
                root_release_path.write_text("ensure root released\n", encoding="utf-8")
            if worker_active_path.exists():
                worker_release_path.write_text("ensure worker released\n", encoding="utf-8")

        assert campaign["budget"]["invocation_ceiling"] == 10
        assert campaign["budget"]["invocations_used"] == 3
        assert len(campaign["reports"]) == 1
        report_id = campaign["reports"][0]["report_id"]
        preview = client.get(
            f"/api/projects/{project_id}/campaigns/{campaign_id}/reports/{report_id}/preview"
        )
        assert preview.status_code == 200, preview.text
        assert "partial report" in preview.text
        assert retained_body in preview.text

        listed_messages = client.get(f"/api/projects/{project_id}/campaigns/{campaign_id}/messages")
        assert listed_messages.status_code == 200, listed_messages.text
        listed_message_payload = listed_messages.json()

    tasks = store.campaign_tasks(campaign_id)
    roles = [store.campaign_invocation_role(task.operation_id) for task in tasks]
    assert roles.count("orchestrator") == 1
    assert roles.count("worker") == 1
    assert roles.count("report") == 1
    root = store.agent_task(root_operation_id)
    assert root is not None and root.status == "failed"
    report = next(
        task for task in tasks if store.campaign_invocation_role(task.operation_id) == "report"
    )
    assert report.status == "succeeded"
    assert report.native_session_id == root.native_session_id
    assert report.stage_host == root.stage_host
    assert report.stage_root == root.stage_root
    report_launches = [
        receipt
        for receipt in store.agent_task_receipts(report.operation_id)
        if receipt.category == "agent_launch"
    ]
    assert [receipt.payload["correction_round"] for receipt in report_launches] == [0, 1]
    structural = [
        receipt
        for receipt in store.agent_task_receipts(root_operation_id)
        if receipt.category == "campaign_orchestrator_failure"
    ]
    assert len(structural) == 1
    assert structural[0].payload["classification"] == "structural_unrecoverable"
    assert structural[0].payload["recoverable"] is False
    assert len(store.campaign_reports(campaign_id)) == 1
    assert rejected_message_status == 409
    assert messages_after_terminal_attempt == messages_before
    assert [item["body"] for item in listed_message_payload] == [retained_body]
    assert listed_message_payload[0]["delivered_at"] is None

    def unexpected_report_request(_campaign):
        raise AssertionError("a terminal campaign must not allocate another report")

    assert (
        background.reconcile_campaign_report(
            campaign_id,
            request_factory=unexpected_report_request,
        )
        is None
    )
    assert len(store.campaign_reports(campaign_id)) == 1
    assert root_release_path is not None and not root_release_path.exists()
    assert worker_release_path is not None and not worker_release_path.exists()
