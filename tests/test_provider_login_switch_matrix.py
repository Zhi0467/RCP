from __future__ import annotations

import pytest

from rcp.core.transition_models import GraphTargetRef

from .helpers import wait_for_task, wait_until
from .provider_login_switch_helpers import PROJECT, switch_background, switched_session
from .test_provider_login_resolver import accounts as accounts


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("direction", ["personal_to_team", "team_to_personal"])
@pytest.mark.parametrize(
    "operation",
    [
        "resume",
        "retry",
        "clean_retry",
        "transport_reattempt",
        "restart_running",
        "restart_paused",
        "fork",
        "graph_repair",
        "artifact_edit",
        "artifact_edit_wait",
        "answer_followup",
        "wrapup_report",
        "consolidation",
    ],
)
def test_operation_after_login_switch(
    accounts, tmp_path, monkeypatch, provider, direction, operation
):
    stage = tmp_path / "stage"
    status = (
        "running"
        if operation == "restart_running"
        else "paused"
        if operation in {"resume", "restart_paused"}
        else "succeeded"
        if operation in {"graph_repair", "artifact_edit", "artifact_edit_wait", "answer_followup"}
        else "failed"
    )
    old, new, member, request = switched_session(
        accounts, stage, provider, direction, status=status, repair=operation == "graph_repair"
    )
    store = accounts.store
    selected = []
    tasks = switch_background(store, stage, selected)
    if operation in {"restart_running", "restart_paused"}:
        tasks.recover_at_startup()
        previous = store.agent_task("previous")
        assert previous is not None
        assert previous.status == ("interrupted" if operation == "restart_running" else "paused")
        child = tasks.resume("previous", authorized_by=member)
    elif operation == "resume":
        child = tasks.resume("previous", authorized_by=member)
    elif operation == "fork":
        fork = store.create_chat_fork(PROJECT, request.chat_id, GraphTargetRef(), "fork-request")
        child = tasks.start(
            PROJECT,
            "project_chat",
            request.model_copy(update={"chat_id": fork.chat_id}),
            authorized_by=member,
        )
    elif operation == "graph_repair":
        child = tasks.repair_graph_update("previous", authorized_by=member)
    elif operation in {"artifact_edit", "artifact_edit_wait"}:
        from types import SimpleNamespace

        from rcp.runs.artifact_edit_admission import admit_artifact_edit
        from rcp.runs.session_master import record_session_master
        from rcp.storage import Artifact

        record_session_master(store, "previous", "Retained master")
        store.create_artifact(
            Artifact(
                artifact_id="a" * 24,
                project_id=PROJECT,
                supplier="turn",
                supplier_id="previous",
                source_name="notes.md",
                media_type="text/markdown",
                created_at=store.now(),
                origin_operation_id="previous",
            ),
            data=b"Existing notes",
        )
        from rcp.service import RunRequest

        edit = admit_artifact_edit(
            store,
            SimpleNamespace(history=SimpleNamespace(graph_target=GraphTargetRef())),
            PROJECT,
            RunRequest(
                message="Update notes",
                artifact_context={
                    "operation_id": "previous",
                    "artifact_id": "a" * 24,
                },
            ),
        )
        child = tasks.start(PROJECT, "project_chat", edit, authorized_by=member)
        if operation == "artifact_edit_wait":
            from rcp.runs.provider_login import resume_provider_account

            initial = wait_for_task(store, child.operation_id, expect="succeeded")
            wait_until(lambda: initial.operation_id not in tasks._workers)
            with store.connection() as connection:
                connection.execute(
                    "UPDATE graph_runs SET status='failed' WHERE operation_id=?",
                    (initial.operation_id,),
                )
            store.record_agent_task_receipt(initial.operation_id, "provider_runtime_selected", {})
            store.mark_provider_login_signed_out(
                new, member_id=member.user_id, source="restore", detail="expired"
            )
            selected.clear()
            child = tasks.retry(initial.operation_id, authorized_by=member)
            assert child.status == "queued"
            assert child.native_session_id == initial.native_session_id
            assert selected == []
            assert child.operation_id not in tasks._workers
            store.mark_provider_login_verified(new, member_id=member.user_id, detail="restored")
            resume_provider_account(tasks, new)

    elif operation == "answer_followup":
        from rcp.runs.chat import reconcile_chat_question_answers
        from rcp.storage.question_models import QuestionOrigin

        previous = store.agent_task("previous")
        question = store.create_or_get_question(
            origin=QuestionOrigin(
                owner_kind="chat",
                project_id=PROJECT,
                owner_id=request.chat_id,
                operation_id="previous",
                provider=provider,
                native_session_id="session",
                stage_root=str(stage),
                stage_host="",
                capability=previous.dispatch_authority.task_contract,
                graph_target=GraphTargetRef(),
            ),
            key="route",
            question="Which route?",
        )
        question = store.answer_question(question.question_id, answer="Route B", resolved_by=member)
        store.mark_question_answer_projected(question.question_id, question.answer_revision)
        reconcile_chat_question_answers(
            store, tasks, lambda _: pytest.fail("Answer was already projected"), project_id=PROJECT
        )
        followup = store.get_question(question.question_id).followup_operation_id
        assert followup is not None
        child = store.agent_task(followup)
    elif operation == "wrapup_report":
        from rcp.runs.episodes.report import start_episode_report
        from rcp.runs.episodes.wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup
        from rcp.storage import EpisodeRecord

        store.create_episode(
            EpisodeRecord(
                episode_id="report-episode",
                project_id=PROJECT,
                mode="experiment_loop",
                control_node_id="exp/report",
                status="queued",
                invocation_ceiling=1,
                authorized_by=member,
                created_at=store.now(),
                updated_at=store.now(),
            )
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET episode_id='report-episode' WHERE operation_id='previous'"
            )
        admission = begin_episode_report_wrapup(
            store,
            EpisodeWrapupSpec(
                episode_id="report-episode",
                ending="completed",
                partial=False,
                continuation_operation_id="previous",
                receipt={"observations": ["Result"]},
            ),
        )
        assert admission.task is not None
        monkeypatch.setattr("rcp.runs.episodes.report.seconds_until_automatic_launch", lambda: 0)
        child = start_episode_report(tasks, "report-episode")
    elif operation == "consolidation":
        from datetime import datetime, timedelta
        from uuid import uuid4

        from rcp.consolidation import ConsolidationPoller
        from rcp.service import resolve_dispatch_authority
        from rcp.storage import AgentTaskRecord

        now = datetime.fromisoformat(store.now())
        schedule = store.put_consolidation_schedule(
            PROJECT,
            local_time="02:00",
            timezone="UTC",
            authorized_by=member,
            next_due_at=(now - timedelta(minutes=1)).isoformat(),
        )
        store.ensure_consolidation_target(schedule)
        scheduled_request = request.model_copy(update={"mode": "work", "trigger": "schedule"})
        queued = AgentTaskRecord(
            operation_id=str(uuid4()),
            project_id=PROJECT,
            kind="project_chat",
            status="queued",
            status_message="queued",
            request=scheduled_request.model_dump(mode="json"),
            authorized_by=member,
            dispatch_authority=resolve_dispatch_authority("project_chat", scheduled_request),
            created_at=store.now(),
            updated_at=store.now(),
        )
        run = store.claim_consolidation_occurrence(
            schedule,
            occurrence_date=now.date().isoformat(),
            next_due_at=(now + timedelta(days=1)).isoformat(),
            input_head=0,
            task=queued,
        )
        assert run is not None and run.operation_id is not None
        poller = ConsolidationPoller(
            store, tasks, service_for=lambda _: pytest.fail("Already admitted")
        )
        poller.dispatch_due(now)
        child = store.agent_task(run.operation_id)
    elif operation == "transport_reattempt":
        store.record_agent_task_receipt("previous", "transport_auto_retry", {"attempt": 1})
        monkeypatch.setattr("rcp.background.seconds_until_automatic_launch", lambda: 0)
        tasks._run_transport_retry("previous", attempt=1)
        child = wait_until(
            lambda: next(
                (
                    row
                    for row in store.all_project_agent_tasks(PROJECT)
                    if row.parent_operation_id == "previous"
                ),
                None,
            )
        )
    else:
        if operation == "clean_retry":
            store.record_agent_task_receipt(
                "previous", "provider_terminal_error", {"classification": "stale_session"}
            )
        child = tasks.retry("previous", authorized_by=member)
    finished = wait_for_task(store, child.operation_id, expect="succeeded")
    if operation == "clean_retry":
        assert store.agent_task_continuation_cause(child.operation_id) == "handoff"
    expected_session = None if operation in {"clean_retry", "fork", "consolidation"} else "session"
    assert selected == [(new, expected_session, "session" if operation == "fork" else None)]
    assert finished.login_id == new.login_id
    assert store.agent_task("previous").login_id == old.login_id
    assert store.provider_login_state(old).state == "signed_out"
    assert store.provider_login_state(new).state == "signed_in"
    if expected_session:
        assert finished.native_session_id == "session"
        switches = [
            receipt.payload
            for receipt in store.agent_task_receipts(child.operation_id)
            if receipt.category == "provider_login_switched"
        ]
        assert switches == (
            []
            if operation == "artifact_edit_wait"
            else [{"from": old.login_id, "to": new.login_id, "reason": "unhealthy"}]
        )
