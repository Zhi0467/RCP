from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from rcp.consolidation import ConsolidationPoller, next_occurrence, occurrence_at, operation_outcome
from rcp.machine_power import demand_snapshot
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
    runtime.poller.poll_once()
    assert runtime.store.consolidation_schedule(runtime.schedule.project_id).next_due_at == due
    assert len(runtime.dispatched) == 1


def test_unchanged_covered_head_skips(runtime):
    with runtime.store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_schedules SET covered_head=?",
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
    revisions, proposals, covered = operation_outcome(boundaries, "night", 4)
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


def test_consolidation_reuses_ordinary_keep_awake_demand(runtime):
    runtime.poller.poll_once()
    assert "task" in demand_snapshot(runtime.store, runtime.app.state.background_tasks)


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
    for method in (
        runtime.app.state.background_tasks.resume,
        runtime.app.state.background_tasks.retry,
    ):
        with pytest.raises(ValueError, match="consolidation_continuation_forbidden"):
            method(run.operation_id)

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


def test_departed_authorizer_creates_occurrence_failure_without_task(runtime):
    with runtime.store.connection() as conn:
        conn.execute(
            "DELETE FROM project_members WHERE project_id=?", (runtime.schedule.project_id,)
        )
    runtime.poller.poll_once()
    runtime.poller.poll_once()
    runs = runtime.store.consolidation_runs()
    assert len(runs) == 1
    assert runs[0].operation_id is None
    assert runs[0].error_code == "authorization_membership_lost"
    assert runs[0].revisions_verified and runs[0].applied_revisions == []
    assert runtime.dispatched == []


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


def test_unavailable_history_is_retried_without_reopening_dismissed_row(runtime, monkeypatch):
    runtime.poller.poll_once()
    run = runtime.store.consolidation_runs()[0]
    runtime.store.fail_agent_task(run.operation_id, "Provider failed")
    history = runtime.app.state.catalog.open(run.project_id).history
    original = history.accepted_patch_boundaries

    def unavailable():
        raise OSError("canonical history unavailable")

    monkeypatch.setattr(history, "accepted_patch_boundaries", unavailable)
    runtime.poller.reconcile_outcomes()
    failed = runtime.store.consolidation_run(run.run_id)
    assert failed.kind == "failure" and not failed.revisions_verified
    runtime.store.resolve_consolidation_run(
        run.project_id,
        run.run_id,
        state="dismissed",
        resolved_by=runtime.schedule.authorized_by,
    )
    monkeypatch.setattr(history, "accepted_patch_boundaries", original)
    runtime.poller.reconcile_outcomes()
    verified = runtime.store.consolidation_run(run.run_id)
    assert verified.revisions_verified
    assert verified.state == "dismissed"
    assert verified.created_at == failed.created_at
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
    assert runtime.store.consolidation_schedule(run.project_id) is None


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
    effect = source_effect_id(run.operation_id, "commit")
    runtime.store.reserve_consolidation_apply(
        run.operation_id, "commit", hashlib.sha256(text.encode()).hexdigest(), text, effect
    )
    if committed_key:
        execution = AgentTaskExecution(run.operation_id, runtime.store, AgentProcessControl())
        result, failure = _apply_work_patch(
            runtime.app.state.catalog.open(run.project_id),
            execution,
            text,
            run_truth_scope=list(runtime.service.manifest.project.truth_scope),
            source_effect_id=source_effect_id(run.operation_id, committed_key),
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
