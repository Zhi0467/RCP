from __future__ import annotations

import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import Event

import pytest

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphHeadRef
from rcp.storage import Artifact
from rcp.storage.consolidation import target_label
from tests.helpers import signed_in_client

from .helpers import TASK_SETTLE_TIMEOUT, create_named_app
from .test_storage import _project


@pytest.fixture
def setup(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    return signed_in_client(app), store, app.state.default_project_id


def _due(store, project_id, *, target="main"):
    now = datetime.fromisoformat(store.now())
    owner = store.local_owner
    human = AuthorizedHuman(
        space_id=store.space_id, user_id=owner.user_id, display_name=owner.display_name
    )
    schedule = store.put_consolidation_schedule(
        project_id,
        local_time="00:00",
        timezone="UTC",
        authorized_by=human,
        next_due_at=(now - timedelta(minutes=1)).isoformat(),
    )
    store.ensure_consolidation_target(schedule, target=target)
    return schedule


def _failure(store, project_id, *, target="main"):
    schedule = _due(store, project_id, target=target)
    now = datetime.fromisoformat(store.now())
    return store.claim_consolidation_occurrence(
        schedule,
        occurrence_date=now.date().isoformat(),
        next_due_at=(now + timedelta(days=1)).isoformat(),
        input_head=0,
        target=target,
        error_code="authorizer_not_member",
        error_message="departed",
    )


def _report(store, project_id, *, settle=True):
    run = _failure(store, project_id)
    # Exercise outcome persistence independently of provider launch.
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET outcome_settled_at=NULL WHERE run_id=?", (run.run_id,)
        )
    # A turn artifact is readable until its stage retention lapses.
    expires_at = (datetime.fromisoformat(store.now()) + timedelta(days=1)).isoformat()
    store.create_artifact(
        Artifact(
            artifact_id="report",
            project_id=project_id,
            supplier="turn",
            supplier_id="turn",
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=store.now(),
            expires_at=expires_at,
        ),
        data=b"<p>Report</p>",
    )
    if not settle:
        return run
    return _report_settlement(store, run)


def _report_settlement(store, run):
    return store.settle_consolidation_run(
        run.run_id,
        kind="report",
        report_artifact_id="report",
        report_title="Report",
        applied_revisions=[{"revision": 1, "summary": "change"}],
        revisions_verified=True,
        proposals_created=1,
        covered_head=1,
    )


def test_schedule_contract_renewal_validation_and_delete(setup):
    client, store, project = setup
    base = f"/api/projects/{project}/consolidation"
    assert client.get(base).json() == {
        "schedule": None,
        "inbox": [],
        "can_write": True,
        "recent_nights": [],
        "evidence_flags": [],
    }
    body = {"local_time": "02:30", "timezone": "America/New_York"}
    first = client.put(base + "/schedule", json=body)
    assert first.status_code == 200
    item = first.json()["schedule"]
    assert set(item) == {
        "authorization_id",
        "local_time",
        "timezone",
        "authorized_by",
        "authorized_at",
        "expires_at",
        "expired",
        "next_due_at",
        "last_run_at",
        "last_outcome",
    }
    assert item["authorized_by"]["user_id"] == store.local_owner.user_id
    assert datetime.fromisoformat(item["expires_at"]) - datetime.fromisoformat(
        item["authorized_at"]
    ) == timedelta(days=30)
    assert (
        client.put(base + "/schedule", json=body).json()["schedule"]["authorization_id"]
        != item["authorization_id"]
    )
    assert (
        client.put(base + "/schedule", json={**body, "operation_id": "forged"}).status_code == 422
    )
    assert (
        client.put(base + "/schedule", json={**body, "timezone": "No/Such_Zone"}).status_code == 422
    )
    assert client.delete(base + "/schedule").json() == {"schedule": None}


@pytest.mark.parametrize("on_branch", [False, True])
def test_failure_is_dismiss_only_and_dismiss_closes_shared_inbox(setup, on_branch):
    client, store, project = setup
    target = str(uuid.uuid4()) if on_branch else "main"
    assert target_label(target, {}) == (target[:8] if on_branch else "main")
    if on_branch:
        store.create_graph_branch(
            project_id=project,
            branch_id=target,
            owner_episode_id=None,
            parent_head=GraphHeadRef(revision=0),
            created_by=store.local_owner.user_id,
            name="source-audit",
        )
    run = _failure(store, project, target=target)
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET error_code='history_unavailable',revisions_verified=0 WHERE run_id=?",
            (run.run_id,),
        )
    base = f"/api/projects/{project}/consolidation"
    assert client.post(f"{base}/runs/{run.run_id}/keep", json={}).status_code == 409
    response = client.get(base).json()
    assert response["can_write"]
    inbox = response["inbox"]
    assert len(inbox) == 1 and inbox[0]["operation_id"] is None
    assert inbox[0]["graph_target"] == run.graph_target.model_dump(mode="json")
    assert inbox[0]["graph_branch_name"] == ("source-audit" if on_branch else "main")
    assert not inbox[0]["revisions_verified"]
    assert inbox[0]["error"]["code"] == "history_unavailable"
    assert (
        client.post(f"{base}/runs/{run.run_id}/dismiss", json={}).json()["item"]["state"]
        == "dismissed"
    )
    assert client.get(base).json()["inbox"] == []


def test_recent_nights_merge_skipped_occurrences_with_run_outcomes(setup):
    client, store, project = setup
    run = _failure(store, project)
    url = f"/api/projects/{project}/consolidation"
    assert client.get(url).json()["recent_nights"] == [
        {"occurrence_date": run.occurrence_date, "outcome": "failed"}
    ]
    now = datetime.fromisoformat(store.now())
    target = str(uuid.uuid4())
    store.claim_consolidation_occurrence(
        _due(store, project, target=target),
        target=target,
        occurrence_date=run.occurrence_date,
        next_due_at=(now + timedelta(days=1)).isoformat(),
        input_head=0,
        skipped=True,
    )
    assert client.get(url).json()["schedule"]["last_outcome"] == "failed"
    renewed = client.put(url + "/schedule", json={"local_time": "02:30", "timezone": "UTC"})
    assert renewed.json()["schedule"]["last_outcome"] == "failed"
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET outcome_settled_at=NULL WHERE run_id=?", (run.run_id,)
        )
    assert client.get(url).json()["schedule"]["last_outcome"] is None
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET outcome_settled_at=? WHERE run_id=?",
            (store.now(), run.run_id),
        )
    for days in range(1, 8):
        due = (now - timedelta(minutes=1)).isoformat()
        with store.connection() as conn:
            conn.execute("UPDATE consolidation_schedules SET next_due_at=?", (due,))
            conn.execute("UPDATE consolidation_targets SET next_due_at=?", (due,))
        store.claim_consolidation_occurrence(
            store.consolidation_schedule(project),
            occurrence_date=(now + timedelta(days=days)).date().isoformat(),
            next_due_at=(now + timedelta(days=days + 1)).isoformat(),
            input_head=0,
            skipped=True,
        )
    nights = client.get(url).json()["recent_nights"]
    # Seven skips after one failure: the oldest night falls out of the strip.
    assert [night["outcome"] for night in nights] == ["skipped"] * 7
    assert run.occurrence_date < nights[0]["occurrence_date"]
    assert nights == sorted(nights, key=lambda night: night["occurrence_date"])


def test_open_report_outlives_stale_expiry_metadata(setup):
    _, store, project = setup
    _report(store, project)
    # Old/stale metadata must never overrule the open Inbox row's retention.
    with store.connection() as conn:
        conn.execute(
            "UPDATE artifacts SET metadata=json_set(metadata, '$.expires_at', ?) WHERE artifact_id='report'",
            (store.now(),),
        )
    assert (
        store.expire_artifacts(as_of=datetime.fromisoformat(store.now()) + timedelta(days=30)) == 0
    )


@pytest.mark.parametrize("action", ["settle", "dismiss"])
def test_report_settlement_serializes_with_version_publication(setup, monkeypatch, action):
    _, store, project = setup
    run = _report(store, project, settle=action == "dismiss")
    artifact = store.artifact("report")
    human = store.consolidation_schedule(project).authorized_by
    original_lock = store.artifact_lock
    settlement_waiting = Event()

    @contextmanager
    def observed_lock(artifact_id):
        settlement_waiting.set()
        with original_lock(artifact_id):
            yield

    monkeypatch.setattr(store, "artifact_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with original_lock("report"):
            if action == "settle":
                future = executor.submit(
                    store.settle_consolidation_run,
                    run.run_id,
                    kind="report",
                    report_artifact_id="report",
                    applied_revisions=[],
                    revisions_verified=True,
                    proposals_created=0,
                )
            else:
                future = executor.submit(
                    store.resolve_consolidation_run,
                    project,
                    run.run_id,
                    state="dismissed",
                    resolved_by=human,
                )
            assert settlement_waiting.wait(timeout=TASK_SETTLE_TIMEOUT)
            version = store.publish_artifact_version(
                "report",
                base_version=artifact.current_version,
                operation_id="report-edit",
                data=b"<p>Edited report</p>",
            )
        assert future.result(timeout=TASK_SETTLE_TIMEOUT).state == (
            "open" if action == "settle" else "dismissed"
        )
    artifact = store.artifact("report")
    assert artifact.current_version == version.version_id
    assert store.read_artifact_bytes("report") == b"<p>Edited report</p>"
    if action == "settle":
        assert artifact.expires_at is None
    else:
        assert datetime.fromisoformat(artifact.expires_at) > datetime.fromisoformat(store.now())
    assert store.expire_artifacts(
        as_of=datetime.fromisoformat(store.now()) + timedelta(days=30)
    ) == (0 if action == "settle" else 1)


@pytest.mark.parametrize("route", ["inbox", "viewer", "unnamed_viewer"])
def test_keeping_report_closes_inbox_from_either_route(setup, route):
    client, store, project = setup
    run = _report(store, project)
    viewer = route != "inbox"
    if route == "unnamed_viewer":
        # Keep writes no history, so a member without a display name may keep.
        with store.connection() as connection:
            connection.execute(
                "UPDATE space_users SET display_name=NULL WHERE user_id=?",
                (store.local_owner.user_id,),
            )
    artifact_base = f"/api/projects/{project}/artifacts"
    state = client.get(artifact_base + "/report/state").json()
    assert state["can_keep"] and store.artifact("report").kept_at is None
    assert client.get(artifact_base).json() == []
    url = (
        f"/api/projects/{project}/artifacts/report/keep"
        if viewer
        else f"/api/projects/{project}/consolidation/runs/{run.run_id}/keep"
    )
    assert client.post(url, json={}).status_code == 200
    assert store.consolidation_run(run.run_id).state == "kept"
    resolved_by = store.consolidation_run(run.run_id).resolved_by
    if route == "unnamed_viewer":
        assert resolved_by is None
    else:
        assert resolved_by.user_id == store.local_owner.user_id
    assert store.artifact("report").kept_at
    assert not client.get(artifact_base + "/report/state").json()["can_keep"]
    assert [item["artifact_id"] for item in client.get(artifact_base).json()] == ["report"]


@pytest.mark.parametrize("settled", [False, True])
def test_a_linked_report_is_saved_but_stays_open_for_review(setup, settled):
    _, store, project = setup
    run = _report(store, project, settle=settled)
    store.mutate_bulletin_post(
        project, "main", text="See <artifact:report>", user_id=store.local_owner.user_id, key="k"
    )
    if not settled:
        run = _report_settlement(store, run)
    assert store.artifact("report").kept_at
    assert store.consolidation_run(run.run_id).state == "open"


def test_restore_disables_authorizations_and_fails_only_unresolved_runs(setup):
    _, store, project = setup
    closed = _failure(store, project)
    closed = store.resolve_consolidation_run(
        project,
        closed.run_id,
        state="dismissed",
        resolved_by=store.consolidation_schedule(project).authorized_by,
    )
    schedule = _due(store, project)
    now = datetime.fromisoformat(store.now())
    run = store.claim_consolidation_occurrence(
        schedule,
        occurrence_date=(now + timedelta(days=1)).date().isoformat(),
        next_due_at=(now + timedelta(days=2)).isoformat(),
        input_head=0,
        error_code="authorizer_not_member",
        error_message="departed",
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET outcome_settled_at=NULL WHERE run_id=?", (run.run_id,)
        )
        store.detach_consolidation_for_restore(conn, diagnostic="restored", now=store.now())
    assert store.consolidation_schedule(project) is None
    detached = store.consolidation_run(run.run_id)
    assert detached.error_code == "restored_run_detached" and detached.outcome_settled_at
    assert store.consolidation_run(closed.run_id) == closed


@pytest.mark.parametrize("guard", ["membership", "admission", "identity"])
def test_route_guards(setup, monkeypatch, guard):
    from fastapi import HTTPException

    from rcp.api.identity import IdentityAccess

    client, store, project = setup
    if guard == "membership":
        monkeypatch.setattr(store, "is_project_member", lambda *args: False)
        status = 404
    elif guard == "admission":

        def refuse(*args):
            raise ValueError("admission closed")

        monkeypatch.setattr(store, "require_project_accepts_new_work", refuse)
        status = 409
    else:

        def refuse(*args):
            raise HTTPException(status_code=428, detail="identity_name_required")

        monkeypatch.setattr(IdentityAccess, "require_patch_capable_identity", refuse)
        status = 428
    assert (
        client.put(
            f"/api/projects/{project}/consolidation/schedule",
            json={"local_time": "01:00", "timezone": "UTC"},
        ).status_code
        == status
    )
    assert store.consolidation_schedule(project) is None
    if guard != "membership":
        response = client.get(f"/api/projects/{project}/consolidation")
        assert response.json()["can_write"] is (guard != "admission")


def test_captured_report_survives_expiry_before_outcome_recovery(setup):
    _, store, project = setup
    run = _failure(store, project)
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET outcome_settled_at=NULL,operation_id=? WHERE run_id=?",
            ("turn", run.run_id),
        )
    store.create_artifact(
        Artifact(
            artifact_id="pending-report",
            project_id=project,
            supplier="turn",
            supplier_id="turn",
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=store.now(),
            expires_at=store.now(),
        ),
        data=b"<p>Pending</p>",
    )
    assert (
        store.expire_artifacts(as_of=datetime.fromisoformat(store.now()) + timedelta(days=30)) == 0
    )
    assert store.artifact("pending-report") is not None


def test_member_loss_during_skip_claim_records_failure(setup):
    _, store, project = setup
    schedule = _due(store, project)
    now = datetime.fromisoformat(store.now())
    with store.connection() as conn:
        conn.execute(
            "UPDATE space_users SET removal_started_at=? WHERE user_id=?",
            (store.now(), schedule.authorized_by.user_id),
        )
    run = store.claim_consolidation_occurrence(
        schedule,
        occurrence_date=now.date().isoformat(),
        next_due_at=(now + timedelta(days=1)).isoformat(),
        input_head=0,
        skipped=True,
    )
    assert run.kind == "failure" and run.error_code == "authorization_membership_lost"
    assert run.operation_id is None


def test_prelaunch_failure_hides_internal_admission_pointers(setup):
    client, store, project = setup
    run = _failure(store, project)
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET operation_id='internal',chat_id='chat',error_code='consolidation_authorization_expired' WHERE run_id=?",
            (run.run_id,),
        )
    item = client.get(f"/api/projects/{project}/consolidation").json()["inbox"][0]
    assert item["operation_id"] is None and item["chat_id"] is None
    assert store.consolidation_run(run.run_id).operation_id == "internal"


def test_legacy_project_id_rewrite_moves_consolidation_state(setup):
    _client, store, legacy_id = setup
    run = _failure(store, legacy_id)
    canonical_id = str(uuid.uuid4())
    with store.connection() as connection:
        connection.execute("DELETE FROM projects WHERE project_id=?", (legacy_id,))
    store.upsert_project(
        _project(canonical_id).model_copy(update={"home_space_id": store.space_id})
    )
    store.migrate_legacy_project_data(legacy_id, canonical_id)
    assert store.consolidation_schedule(legacy_id) is None
    assert store.consolidation_schedule(canonical_id) is not None
    assert [item.run_id for item in store.consolidation_runs(canonical_id)] == [run.run_id]


def test_old_writer_migration_preserves_main_coverage_and_runs(tmp_path):
    from rcp.storage.consolidation import migrate_consolidation, migrate_consolidation_targets
    from rcp.storage.evidence_flags import migrate_evidence_flags
    from rcp.storage.resource_versions import _bump, _install

    # Use the unchanged historical writer, not the current models or new schema.
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        migrate_consolidation(conn)
        migrate_evidence_flags(conn)
        conn.execute(
            "CREATE TABLE resource_versions(scope TEXT,resource TEXT,entity TEXT,generation INTEGER,PRIMARY KEY(scope,resource,entity))"
        )
        _install(conn, "consolidation_schedules", [_bump("ROW.project_id", "consolidation")])
        conn.execute(
            "INSERT INTO consolidation_schedules(project_id,authorization_id,local_time,timezone,authorized_by_json,authorized_at,expires_at,next_due_at,covered_head,last_occurrence_date) VALUES('project','auth','01:00','UTC','{}','created','expires','due',17,'date')"
        )
        conn.execute(
            "INSERT INTO consolidation_runs(run_id,project_id,occurrence_date,authorization_id,authorized_by_json,input_head,created_at,kind,outcome_settled_at) VALUES('run','project','date','auth','{}',17,'created','failure','settled')"
        )
        old_run = dict(conn.execute("SELECT * FROM consolidation_runs").fetchone())
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        migrate_consolidation_targets(conn)
        coverage = dict(conn.execute("SELECT * FROM consolidation_targets").fetchone())
        assert coverage == {
            "project_id": "project",
            "target": "main",
            "next_due_at": "due",
            "covered_head": 17,
            "last_occurrence_date": "date",
            "skipped_dates_json": None,
        }
        assert dict(conn.execute("SELECT * FROM consolidation_runs").fetchone()) == {
            **old_run,
            "target": "main",
        }
        assert "covered_head" not in {
            row[1] for row in conn.execute("PRAGMA table_info(consolidation_schedules)")
        }
