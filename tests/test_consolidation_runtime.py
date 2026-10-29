from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from rcp.consolidation import ConsolidationPoller, next_occurrence, occurrence_at, operation_outcome
from rcp.history.queries import StepRecord
from rcp.storage.artifact_models import Artifact

from .helpers import append_fixture_patch, authorized_human, create_named_app, seed_patch


@pytest.fixture
def runtime(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    now = datetime.fromisoformat(store.now())
    schedule = store.put_consolidation_schedule(
        app.state.default_project_id,
        local_time="02:00",
        timezone="UTC",
        authorized_by=authorized_human(app),
        next_due_at=(now - timedelta(days=3)).isoformat(),
    )
    dispatched = []

    def dispatch(operation_id):
        if store.mark_agent_task_running(operation_id):
            dispatched.append(operation_id)
        return store.agent_task(operation_id)

    monkeypatch.setattr(app.state.background_tasks, "launch_admitted", dispatch)
    monkeypatch.setattr("rcp.consolidation.seconds_until_automatic_launch", lambda: 0)
    poller = ConsolidationPoller(
        store, app.state.background_tasks, service_for=app.state.catalog.open
    )
    return SimpleNamespace(
        app=app,
        store=store,
        service=service,
        poller=poller,
        schedule=schedule,
        dispatched=dispatched,
        now=now,
    )


def test_due_admission_is_atomic_and_catchup_collapses_missed_days(runtime):
    other = ConsolidationPoller(
        runtime.store,
        runtime.app.state.background_tasks,
        service_for=runtime.app.state.catalog.open,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda poller: poller.poll_once(), [runtime.poller, other]))
    runs = runtime.store.consolidation_runs()
    assert len(runs) == len(runtime.dispatched) == 1
    task = runtime.store.agent_task(runs[0].operation_id)
    assert (
        runtime.store.agent_name_state(task.project_id, task.request["chat_id"])["name"]
        == "Graph consolidation"
    )
    runtime.store.set_chat_title(
        task.project_id, task.request["chat_id"], "human", "My graph review"
    )
    runtime.poller._launch(task.operation_id)
    assert (
        runtime.store.agent_name_state(task.project_id, task.request["chat_id"])["name"]
        == "My graph review"
    )
    assert task.request["trigger"] == "schedule"
    assert task.request["workflow_ids"] == ["graph-consolidation"]
    assert task.request["session_id"] is None
    assert task.dispatch_authority.task_contract == "work_auto"
    schedule = runtime.store.consolidation_schedule(runtime.schedule.project_id)
    assert datetime.fromisoformat(schedule.next_due_at) > runtime.now
    runtime.poller.poll_once()
    assert len(runtime.dispatched) == 1


def test_expired_authorization_starts_nothing(runtime):
    runtime.poller.clock = lambda: (
        datetime.fromisoformat(runtime.schedule.expires_at) + timedelta(seconds=1)
    ).isoformat()
    runtime.poller.poll_once()
    assert runtime.store.consolidation_runs() == []
    assert runtime.dispatched == []


def test_unresolved_run_leaves_next_occurrence_owed(runtime):
    runtime.poller.poll_once()
    due = (runtime.now - timedelta(minutes=1)).isoformat()
    with runtime.store.connection() as conn:
        conn.execute("UPDATE consolidation_schedules SET next_due_at=?", (due,))
        conn.execute("UPDATE consolidation_targets SET next_due_at=?", (due,))
    runtime.poller.poll_once()
    assert runtime.store.consolidation_schedule(runtime.schedule.project_id).next_due_at == due
    assert len(runtime.dispatched) == 1


def test_unchanged_covered_head_skips(runtime):
    runtime.store.ensure_consolidation_target(runtime.schedule)
    with runtime.store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_targets SET covered_head=?",
            (runtime.service.history.state().revision,),
        )
    runtime.poller.poll_once()
    assert runtime.dispatched == []
    assert (
        runtime.store.consolidation_schedule(runtime.schedule.project_id).last_outcome == "skipped"
    )


def test_sleep_gate_defers_new_launch(runtime, monkeypatch):
    monkeypatch.setattr("rcp.consolidation.seconds_until_automatic_launch", lambda: 30)
    runtime.poller.poll_once()
    assert runtime.store.consolidation_runs() == []


def test_dst_occurrence_uses_first_valid_minute_and_first_fold():
    gap = occurrence_at(datetime(2026, 3, 8).date(), "02:30", "America/New_York")
    assert gap == datetime(2026, 3, 8, 7, tzinfo=UTC)
    fold = occurrence_at(datetime(2026, 11, 1).date(), "01:30", "America/New_York")
    assert fold == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_occurrence("01:30", "America/New_York", fold) == datetime(
        2026, 11, 2, 6, 30, tzinfo=UTC
    )


def test_covered_head_preserves_external_interleaving_and_counts_resolved_proposals():
    def state(revision, proposals):
        return SimpleNamespace(revision=revision, proposals=dict.fromkeys(proposals))

    def patch(operation, summary):
        return SimpleNamespace(source_operation_id=operation, summary=summary)

    boundaries = [
        (state(4, []), patch("night", "first"), state(5, ["proposal"])),
        (state(5, ["proposal"]), patch("human", "outside"), state(6, ["proposal"])),
        (state(6, ["proposal"]), patch("night", "second"), state(7, ["proposal"])),
    ]
    steps = [
        SimpleNamespace(
            before_state=before, patch=patch, after_state=after, after_head=after, accepted=True
        )
        for before, patch, after in boundaries
    ]
    steps.append(
        SimpleNamespace(
            before_state=state(7, []),
            after_state=state(8, ["rejected-proposal"]),
            after_head=state(8, []),
            patch=patch("night", "rejected"),
            accepted=False,
        )
    )
    revisions, proposals, covered = operation_outcome(cast(list[StepRecord], steps), "night", 4)
    assert [entry["revision"] for entry in revisions] == [5, 7]
    assert proposals == 1
    assert covered == 5


@pytest.mark.parametrize(
    ("status", "media_type", "graph_status", "kind", "code"),
    [
        ("succeeded", "text/html", "applied", "report", None),
        ("succeeded", "text/plain", "none", "failure", "report_missing"),
        ("succeeded", "text/html", "rejected", "failure", "graph_update_failed"),
        ("failed", "text/html", "none", "failure", "task_failed"),
        ("interrupted", "text/html", "none", "failure", "interrupted"),
    ],
)
def test_settlement_uses_terminal_task_graph_outcome_and_viewable_artifact(
    runtime, status, media_type, graph_status, kind, code
):
    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    runtime.store.create_artifact(
        Artifact(
            artifact_id="nightly-report",
            project_id=run.project_id,
            supplier="turn",
            supplier_id=run.operation_id,
            origin_operation_id=run.operation_id,
            source_name="consolidation-report.html",
            media_type=media_type,
            created_at=runtime.store.now(),
            expires_at=runtime.store.now(),
        ),
        data=b"<!doctype html><html><body>Report</body></html>",
    )
    if status == "succeeded":
        runtime.store.complete_agent_task(
            run.operation_id,
            applied_revision=None,
            result={"graph_update": {"status": graph_status}},
        )
    elif status == "interrupted":
        runtime.store.interrupt_active_agent_tasks()
    else:
        runtime.store.fail_agent_task(run.operation_id, "Provider failed")
    runtime.poller.reconcile_outcomes()
    settled = runtime.store.consolidation_run(run.run_id)
    assert (settled.kind, settled.error_code, settled.revisions_verified) == (kind, code, True)
    assert settled.applied_revisions == []
    if kind == "report":
        assert runtime.store.artifact("nightly-report").expires_at is None
        runtime.poller.reconcile_outcomes()
        assert runtime.store.consolidation_run(run.run_id) == settled


@pytest.mark.parametrize("succeeded", [True, False])
def test_next_night_skips_own_successful_commits_but_retries_failed_run(
    runtime, monkeypatch, succeeded
):
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.work import _apply_work_patch

    from .helpers import agent_patch_json, refresh_patch

    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    execution = AgentTaskExecution(run.operation_id, runtime.store, AgentProcessControl())
    scope = runtime.store.agent_task(run.operation_id).request["run_truth_scope"]
    applied, failure = _apply_work_patch(
        runtime.service,
        execution,
        agent_patch_json(refresh_patch().model_copy(update={"run_truth_scope": scope})),
        run_truth_scope=scope,
        read_scope=None,
        source_effect_id="nightly-commit",
    )
    assert failure is None
    runtime.store.create_artifact(
        Artifact(
            artifact_id="completed-report",
            project_id=run.project_id,
            supplier="turn",
            supplier_id=run.operation_id,
            origin_operation_id=run.operation_id,
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=runtime.store.now(),
        ),
        data=b"<!doctype html><html><body>Report</body></html>",
    )
    if succeeded:
        runtime.store.complete_agent_task(
            run.operation_id,
            applied_revision=applied.applied_revision,
            result={"graph_update": applied.model_dump(mode="json")},
        )
    else:
        runtime.store.fail_agent_task(run.operation_id, "Failed after Apply")
    runtime.poller.reconcile_outcomes()
    next_day = runtime.now + timedelta(days=1)
    monkeypatch.setattr(runtime.store, "now", lambda: next_day.isoformat())
    runtime.poller.clock = runtime.store.now
    runtime.poller.poll_once()
    assert len(runtime.dispatched) == (1 if succeeded else 2)


def test_paused_run_settles_commits_and_releases_chat_and_next_occurrence(runtime, monkeypatch):
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.work import _apply_work_patch

    from .helpers import agent_patch_json, refresh_patch

    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    covered_head = runtime.store.consolidation_schedule(run.project_id).covered_head
    execution = AgentTaskExecution(run.operation_id, runtime.store, AgentProcessControl())
    task = runtime.store.agent_task(run.operation_id)
    scope = task.request["run_truth_scope"]
    applied, failure = _apply_work_patch(
        runtime.service,
        execution,
        agent_patch_json(refresh_patch().model_copy(update={"run_truth_scope": scope})),
        run_truth_scope=scope,
        read_scope=None,
        source_effect_id="commit-before-pause",
    )
    assert failure is None
    runtime.store.checkpoint_agent_task(run.operation_id, native_session_id="paused-session")
    runtime.store.pause_agent_task(run.operation_id)
    runtime.poller.reconcile_outcomes()
    outcome = runtime.store.consolidation_run(run.run_id)
    assert (outcome.kind, outcome.error_code, outcome.revisions_verified) == (
        "failure",
        "paused",
        True,
    )
    assert [entry["revision"] for entry in outcome.applied_revisions] == [applied.applied_revision]
    assert runtime.store.consolidation_schedule(run.project_id).covered_head == covered_head

    ordinary = runtime.poller._task(runtime.schedule, runtime.service)
    ordinary = ordinary.model_copy(update={"request": {**ordinary.request, "trigger": "human"}})
    ordinary = runtime.store.create_agent_task(ordinary)
    assert ordinary.request["chat_id"] == task.request["chat_id"]
    assert runtime.store.consolidation_run_for_operation(ordinary.operation_id) is None
    runtime.store.complete_agent_task(ordinary.operation_id, applied_revision=None, result={})

    next_day = runtime.now + timedelta(days=1)
    monkeypatch.setattr(runtime.store, "now", lambda: next_day.isoformat())
    runtime.poller.clock = runtime.store.now
    runtime.poller.poll_once()
    assert len(runtime.dispatched) == 2
    next_task = runtime.store.agent_task(runtime.dispatched[-1])
    assert next_task.request["session_id"] is None
    assert next_task.native_session_id is None
    assert runtime.store.consolidation_run(run.run_id).error_code == "paused"


@pytest.mark.parametrize(
    ("project_status", "error_code"),
    [
        ("source_fenced", "consolidation_project_unavailable"),
        ("archive_bound", "consolidation_project_unavailable"),
        ("retired", "authorization_membership_lost"),
    ],
)
def test_unavailable_project_creates_occurrence_failure_without_task(
    runtime, project_status, error_code
):
    with runtime.store.connection() as conn:
        if project_status == "retired":
            conn.execute(
                "UPDATE projects SET retired_at=? WHERE project_id=?",
                (runtime.store.now(), runtime.schedule.project_id),
            )
        else:
            conn.execute(
                """INSERT INTO project_transfer_requests (
                    request_id, side, phase, project_id, source_space_id,
                    target_space_id, record_json, revision, created_at, updated_at
                ) VALUES ('transfer', 'source', ?, ?, ?, 'target', '{}', 1, ?, ?)""",
                (
                    project_status,
                    runtime.schedule.project_id,
                    runtime.store.space_id,
                    runtime.store.now(),
                    runtime.store.now(),
                ),
            )
    runtime.poller.poll_once()
    runtime.poller.poll_once()
    runs = runtime.store.consolidation_runs()
    assert len(runs) == 1
    assert runs[0].operation_id is None
    assert runs[0].error_code == error_code
    assert runs[0].revisions_verified and runs[0].applied_revisions == []
    assert runtime.dispatched == []
    schedule = runtime.store.consolidation_schedule(runtime.schedule.project_id)
    assert datetime.fromisoformat(schedule.next_due_at) > runtime.now
    assert schedule.last_outcome == "failed"


@pytest.mark.parametrize("dismiss", [False, True])
def test_unavailable_history_is_retried_without_changing_settled_outcome(
    runtime, monkeypatch, dismiss
):
    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    runtime.store.complete_agent_task(run.operation_id, applied_revision=None, result={})
    runtime.store.create_artifact(
        Artifact(
            artifact_id="history-report",
            project_id=run.project_id,
            supplier="turn",
            supplier_id=run.operation_id,
            origin_operation_id=run.operation_id,
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=runtime.store.now(),
        ),
        data=b"<html>Report</html>",
    )
    engine = runtime.app.state.catalog.open(run.project_id).history_engine
    original = engine.steps

    def unavailable(head):
        raise OSError("canonical history unavailable")

    monkeypatch.setattr(engine, "steps", unavailable)
    runtime.poller.reconcile_outcomes()
    failed = runtime.store.consolidation_run(run.run_id)
    assert failed.kind == "failure" and not failed.revisions_verified
    assert failed.error_code == "history_unavailable"
    runtime.store.register_notification_device(runtime.store.local_owner.user_id)
    runtime.app.state.notification_sender.run_pass()
    notified = runtime.store.consolidation_run(run.run_id)
    assert notified.notification_observed_at is not None
    if dismiss:
        runtime.store.resolve_consolidation_run(
            run.project_id,
            run.run_id,
            state="dismissed",
            resolved_by=runtime.schedule.authorized_by,
        )
    next_day = runtime.now + timedelta(days=1)
    monkeypatch.setattr(runtime.store, "now", lambda: next_day.isoformat())
    runtime.poller.clock = runtime.store.now
    runtime.poller.poll_once()
    assert len(runtime.store.consolidation_runs()) == 2
    assert (
        datetime.fromisoformat(runtime.store.consolidation_schedule(run.project_id).next_due_at)
        > next_day
    )
    monkeypatch.setattr(engine, "steps", original)
    runtime.poller.reconcile_outcomes()
    verified = runtime.store.consolidation_run(run.run_id)
    assert verified.revisions_verified
    assert verified.state == ("dismissed" if dismiss else "open")
    assert verified.kind == "failure" and verified.error_code == "history_unavailable"
    assert verified.outcome_settled_at == failed.outcome_settled_at
    assert verified.notification_observed_at == notified.notification_observed_at
    assert verified.report_artifact_id is None
    assert runtime.store.consolidation_schedule(run.project_id).covered_head is None


def test_prelaunch_refusal_has_verified_empty_history_without_reading_project(runtime, monkeypatch):
    monkeypatch.setattr(runtime.app.state.background_tasks, "launch_admitted", lambda _: None)
    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    runtime.store.fail_agent_task(run.operation_id, "consolidation_authorization_revoked")

    def unavailable(_):
        raise OSError("canonical history unavailable")

    runtime.poller.service_for = unavailable
    runtime.poller.reconcile_outcomes()
    failure = runtime.store.consolidation_run(run.run_id)
    assert failure.error_code == "consolidation_authorization_revoked"
    assert failure.revisions_verified and failure.applied_revisions == []


def test_restore_detachment_cannot_be_reclassified_as_report(runtime):
    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    runtime.store.complete_agent_task(run.operation_id, applied_revision=None, result={})
    runtime.store.create_artifact(
        Artifact(
            artifact_id="restored-report",
            project_id=run.project_id,
            supplier="turn",
            supplier_id=run.operation_id,
            origin_operation_id=run.operation_id,
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=runtime.store.now(),
        ),
        data=b"<!doctype html><html><body>Report</body></html>",
    )
    with runtime.store.connection() as conn:
        runtime.store.detach_consolidation_for_restore(
            conn, diagnostic="restore", now=runtime.store.now()
        )
    runtime.poller.reconcile_outcomes()
    restored = runtime.store.consolidation_run(run.run_id)
    assert restored.kind == "failure" and restored.error_code == "restored_run_detached"
    assert restored.revisions_verified


def test_scheduled_chat_overlap_keeps_occurrence_owed(runtime):
    pending = runtime.poller._task(
        runtime.schedule, runtime.app.state.catalog.open(runtime.schedule.project_id)
    )
    pending = pending.model_copy(update={"request": {**pending.request, "trigger": "human"}})
    runtime.store.create_agent_task(pending)
    runtime.poller.poll_once()
    assert runtime.store.consolidation_runs() == []
    assert runtime.dispatched == []
    assert (
        runtime.store.consolidation_schedule(runtime.schedule.project_id).next_due_at
        == runtime.schedule.next_due_at
    )


def test_paused_human_chat_creates_occurrence_failure_without_task(runtime):
    pending = runtime.poller._task(runtime.schedule, runtime.service)
    pending = pending.model_copy(update={"request": {**pending.request, "trigger": "human"}})
    runtime.store.create_agent_task(pending)
    runtime.store.mark_agent_task_running(pending.operation_id)
    runtime.store.checkpoint_agent_task(pending.operation_id, native_session_id="human-session")
    runtime.store.pause_agent_task(pending.operation_id)
    runtime.poller.poll_once()
    runtime.poller.poll_once()
    runs = runtime.store.consolidation_runs()
    assert len(runs) == 1
    assert runs[0].operation_id is None
    assert runs[0].error_code == "consolidation_chat_paused"
    assert runs[0].kind == "failure" and runs[0].revisions_verified
    assert runtime.dispatched == []
    schedule = runtime.store.consolidation_schedule(runtime.schedule.project_id)
    assert datetime.fromisoformat(schedule.next_due_at) > runtime.now
    assert schedule.covered_head == runtime.schedule.covered_head
    assert runtime.store.agent_task(pending.operation_id).can_resume


@pytest.mark.parametrize("failure", ["incomplete", "history", "profile", "machine", "unexpected"])
def test_due_resolution_failure_creates_occurrence_failure_without_task(
    runtime, monkeypatch, caplog, failure
):
    service = runtime.app.state.catalog.open(runtime.schedule.project_id)

    def unavailable(*args):
        raise ValueError("Unavailable configuration")

    if failure == "unexpected":

        def unexpected(head):
            raise RuntimeError("Unexpected failure")

        monkeypatch.setattr(service.history_engine, "steps", unexpected)
    elif failure == "incomplete":
        state = service.history.state().model_copy(update={"replay_status": "degraded"})
        monkeypatch.setattr(service.history_engine, "state_at", lambda head: state)
    elif failure == "history":

        def history_unavailable(head):
            raise OSError("Unavailable history")

        monkeypatch.setattr(service.history_engine, "steps", history_unavailable)
    elif failure == "machine":
        original = runtime.poller._task

        def missing_machine(schedule, service, *args):
            task = original(schedule, service, *args)
            return task.model_copy(update={"request": {**task.request, "run_on": "missing"}})

        monkeypatch.setattr(runtime.poller, "_task", missing_machine)
    else:
        monkeypatch.setattr(
            service,
            "resolve_agent_profile",
            unavailable,
        )
    runtime.poller.poll_once()
    runtime.poller.poll_once()
    runs = runtime.store.consolidation_runs()
    if failure == "unexpected":
        assert runs == []
        assert (
            runtime.store.consolidation_schedule(runtime.schedule.project_id).next_due_at
            == runtime.schedule.next_due_at
        )
        assert any(
            record.exc_info and record.exc_info[0] is RuntimeError for record in caplog.records
        )
        return
    assert len(runs) == 1
    run = runs[0]
    assert run.kind == "failure" and run.operation_id is None
    assert run.error_code == (
        "history_unavailable"
        if failure in {"incomplete", "history"}
        else "consolidation_launch_unavailable"
    )
    assert run.revisions_verified and run.applied_revisions == []
    assert runtime.dispatched == []
    schedule = runtime.store.consolidation_schedule(runtime.schedule.project_id)
    assert datetime.fromisoformat(schedule.next_due_at) > runtime.now
    assert schedule.covered_head == runtime.schedule.covered_head


@pytest.mark.parametrize(
    ("failure_status", "committed_key", "expected_kind"),
    [
        ("unavailable", None, "failure"),
        ("unavailable", "commit", "report"),
        ("unavailable", "retry", "report"),
        ("invalid", None, "report"),
    ],
)
def test_keyed_apply_failure_requires_canonical_commit_before_success(
    runtime, failure_status, committed_key, expected_kind
):
    import hashlib

    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.consolidation import source_effect_id
    from rcp.runs.tasks.work import _apply_work_patch

    from .helpers import agent_patch_json, refresh_patch

    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    text = agent_patch_json(refresh_patch("rq/nightly-verified-effect"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    effect = source_effect_id(run.operation_id, digest)
    runtime.store.reserve_consolidation_apply(run.operation_id, "commit", digest, text, effect)
    if committed_key:
        runtime.store.reserve_consolidation_apply(
            run.operation_id, committed_key, digest, text, effect
        )
        execution = AgentTaskExecution(run.operation_id, runtime.store, AgentProcessControl())
        result, failure = _apply_work_patch(
            runtime.app.state.catalog.open(run.project_id),
            execution,
            text,
            run_truth_scope=list(runtime.service.manifest.project.truth_scope),
            read_scope=None,
            source_effect_id=effect,
        )
        assert failure is None and result.status == "applied"
    runtime.store.record_consolidation_apply_failure(
        run.operation_id, "commit", status=failure_status, message="Effect could not settle"
    )
    runtime.store.create_artifact(
        Artifact(
            artifact_id="keyed-report",
            project_id=run.project_id,
            supplier="turn",
            supplier_id=run.operation_id,
            origin_operation_id=run.operation_id,
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=runtime.store.now(),
        ),
        data=b"<!doctype html><html><body>Report</body></html>",
    )
    runtime.store.complete_agent_task(
        run.operation_id, applied_revision=None, result={"graph_update": {"status": "none"}}
    )
    runtime.poller.reconcile_outcomes()
    outcome = runtime.store.consolidation_run(run.run_id)
    assert outcome.kind == expected_kind
    assert outcome.revisions_verified
    assert outcome.error_code == ("keyed_apply_unavailable" if expected_kind == "failure" else None)


def test_consolidation_query_baseline_is_coverage_not_session_observation():
    from rcp.background import AgentTaskExecution
    from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
    from rcp.runs.consolidation import consolidation_covered_head
    from rcp.service import ProjectService

    expected = GraphHeadRef(revision=7, transition_id="a" * 64)
    calls = []

    class Engine:
        def resolve(self, spec, *, default_target):
            calls.append((spec.revision, spec.target, default_target))
            return expected

    store = SimpleNamespace(
        consolidation_run_for_operation=lambda operation: SimpleNamespace(
            project_id="project", target="main", graph_target=GraphTargetRef()
        ),
        consolidation_target=lambda project, target: SimpleNamespace(covered_head=7),
    )
    execution = cast(AgentTaskExecution, SimpleNamespace(operation_id="night", store=store))
    service = cast(ProjectService, SimpleNamespace(history_engine=Engine()))
    assert consolidation_covered_head(service, execution) == expected
    assert calls == [(7, GraphTargetRef(), GraphTargetRef())]
    store.consolidation_target = lambda project, target: SimpleNamespace(covered_head=None)
    assert consolidation_covered_head(service, execution) is None
    assert len(calls) == 1


@pytest.mark.parametrize("unreadable", ["state", "receipts"])
def test_failed_run_settlement_retries_lost_evidence_holds(runtime, monkeypatch, unreadable):
    import hashlib

    from rcp.core.models import Patch
    from rcp.transport import StateUnavailable

    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    evidence = {
        "id": "ev/added-overnight",
        "type": "evidence",
        "title": "Seed 3 shows no spike",
        "observation": "The seed 3 log has no step-200 spike.",
        "origin": "analytic",
    }
    patch = Patch(
        kind="work",
        author="agent",
        summary="Record the seed 3 result",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[{"op": "create_nodes", "nodes": [evidence]}],
    )
    append_fixture_patch(runtime.service, patch)
    # The apply committed and recorded its receipt; the process died before the hold.
    text = patch.model_dump_json()
    digest = hashlib.sha256(text.encode()).hexdigest()
    runtime.store.reserve_consolidation_apply(run.operation_id, "add", digest, text, "effect")
    runtime.store.finish_consolidation_apply(
        run.operation_id,
        "add",
        {"status": "ok", "result": {"disposition": "applied", "revision": 2}},
    )
    runtime.store.fail_agent_task(run.operation_id, "Provider failed after Apply")

    def unavailable(*_args):
        raise StateUnavailable("state host dropped") if unreadable == "state" else OSError

    with monkeypatch.context() as broken:
        if unreadable == "state":
            broken.setattr(runtime.poller, "service_for", unavailable)
        else:
            broken.setattr(runtime.store, "list_consolidation_apply_receipts", unavailable)
        runtime.poller.reconcile_outcomes()
    assert runtime.store.consolidation_run(run.run_id).kind is None
    assert runtime.store.list_evidence_flags(run.project_id) == []
    assert runtime.store.agent_task_events(run.operation_id)[-1].level == "warning"
    runtime.poller.reconcile_outcomes()
    assert runtime.store.consolidation_run(run.run_id).kind == "failure"
    assert [
        (flag["evidence_id"], flag["kind"])
        for flag in runtime.store.list_evidence_flags(run.project_id)
    ] == [("ev/added-overnight", "consolidation_added")]


def _active_branch(runtime, name, *, changed=True):
    from rcp.history import HistoryManager

    from .helpers import unchecked_write_authority
    from .test_branch_history import _branch_metadata, _branch_patch

    history = HistoryManager(
        runtime.service.manifest,
        runtime.service.history.workspace,
        write_authority=unchecked_write_authority(),
    )
    metadata = _branch_metadata(history, authorized_by=runtime.schedule.authorized_by).model_copy(
        update={"project_id": runtime.schedule.project_id}
    )
    branch = history.create_branch(metadata)
    runtime.store.create_graph_branch(
        project_id=runtime.schedule.project_id,
        branch_id=metadata.branch_id,
        owner_episode_id=None,
        parent_head=metadata.effective_parent_head,
        created_by="human:test",
        name=name,
    )
    if changed:
        branch.append(_branch_patch("ev/branch-result"))
    return branch


def test_changed_branch_runs_while_fresh_and_unchanged_branches_skip(runtime):
    changed = _active_branch(runtime, "changed")
    fresh = _active_branch(runtime, "fresh", changed=False)
    unchanged = _active_branch(runtime, "unchanged")
    runtime.store.ensure_consolidation_target(
        runtime.schedule, target=unchanged.branch_id, covered_head=unchanged.state().revision
    )
    runtime.poller.poll_once()
    runs = {run.target: run for run in runtime.store.consolidation_runs()}
    assert set(runs) == {"main", changed.branch_id}
    task = runtime.store.agent_task(runs[changed.branch_id].operation_id)
    assert task.graph_target == changed.graph_target
    assert task.request["graph_target"] == changed.graph_target.model_dump(mode="json")
    assert (
        task.request["chat_id"]
        != runtime.store.agent_task(runs["main"].operation_id).request["chat_id"]
    )
    for branch in (fresh, unchanged):
        coverage = runtime.store.consolidation_target(runtime.schedule.project_id, branch.branch_id)
        assert coverage.covered_head == branch.state().revision
        assert coverage.skipped_dates


def test_archived_branch_never_runs(runtime):
    branch = _active_branch(runtime, "archived")
    with runtime.store.connection() as conn:
        conn.execute("UPDATE graph_branches SET archived=1 WHERE branch_id=?", (branch.branch_id,))
    runtime.poller.poll_once()
    assert [run.target for run in runtime.store.consolidation_runs()] == ["main"]
    assert runtime.store.consolidation_target(runtime.schedule.project_id, branch.branch_id) is None


def test_target_coverage_settles_independently(runtime):
    branch = _active_branch(runtime, "independent")
    runtime.poller.poll_once()
    runs = {run.target: run for run in runtime.store.consolidation_runs()}
    run = runs[branch.branch_id]
    runtime.store.create_artifact(
        Artifact(
            artifact_id="branch-report",
            project_id=run.project_id,
            supplier="turn",
            supplier_id=run.operation_id,
            origin_operation_id=run.operation_id,
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=runtime.store.now(),
        ),
        data=b"<html>Branch report</html>",
    )
    runtime.store.complete_agent_task(run.operation_id, applied_revision=None, result={})
    runtime.store.fail_agent_task(runs["main"].operation_id, "Main failed")
    runtime.poller.reconcile_outcomes()
    assert (
        runtime.store.consolidation_target(run.project_id, branch.branch_id).covered_head
        == branch.state().revision
    )
    assert runtime.store.consolidation_target(run.project_id, "main").covered_head is None
    assert runtime.store.consolidation_run(run.run_id).kind == "report"


def test_unresolved_target_does_not_block_other_targets_on_later_nights(runtime, monkeypatch):
    branch = _active_branch(runtime, "still-running")
    runtime.poller.poll_once()
    for day in (1, 2):
        for run in runtime.store.consolidation_runs():
            if run.target == "main" and run.outcome_settled_at is None:
                runtime.store.fail_agent_task(run.operation_id, "Retry next night")
        now = runtime.now + timedelta(days=day)
        monkeypatch.setattr(runtime.store, "now", lambda now=now: now.isoformat())
        runtime.poller.clock = runtime.store.now
        runtime.poller.poll_once()
    targets = [run.target for run in runtime.store.consolidation_runs()]
    assert targets.count("main") == 3
    assert targets.count(branch.branch_id) == 1
