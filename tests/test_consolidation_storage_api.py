from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient

from rcp.core.models import AuthorizedHuman
from rcp.storage import Artifact

from .helpers import TASK_SETTLE_TIMEOUT, create_named_app
from .test_storage import _project


@pytest.fixture
def setup(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    return TestClient(app), store, app.state.default_project_id


def _due(store, project_id):
    now = datetime.fromisoformat(store.now())
    owner = store.local_owner
    human = AuthorizedHuman(
        space_id=store.space_id, user_id=owner.user_id, display_name=owner.display_name
    )
    return store.put_consolidation_schedule(
        project_id,
        local_time="00:00",
        timezone="UTC",
        authorized_by=human,
        next_due_at=(now - timedelta(minutes=1)).isoformat(),
    )


def _failure(store, project_id):
    schedule = _due(store, project_id)
    now = datetime.fromisoformat(store.now())
    return store.claim_consolidation_occurrence(
        schedule,
        occurrence_date=now.date().isoformat(),
        next_due_at=(now + timedelta(days=1)).isoformat(),
        input_head=0,
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
    artifact = store.create_artifact(
        Artifact(
            artifact_id="report",
            project_id=project_id,
            supplier="turn",
            supplier_id="turn",
            source_name="consolidation-report.html",
            media_type="text/html",
            created_at=store.now(),
            expires_at=store.now(),
        ),
        data=b"<p>Report</p>",
    )
    if not settle:
        return run
    return store.settle_consolidation_run(
        run.run_id,
        kind="report",
        report_artifact_id=artifact.artifact_id,
        report_title="Report",
        applied_revisions=[{"revision": 1, "summary": "change"}],
        revisions_verified=True,
        proposals_created=1,
        covered_head=1,
    )


def test_schedule_contract_renewal_validation_and_delete(setup):
    client, store, project = setup
    base = f"/api/projects/{project}/consolidation"
    assert client.get(base).json() == {"schedule": None, "inbox": [], "can_write": True}
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


def test_failure_is_dismiss_only_and_dismiss_closes_shared_inbox(setup):
    client, store, project = setup
    run = _failure(store, project)
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
    assert not inbox[0]["revisions_verified"]
    assert inbox[0]["error"]["code"] == "history_unavailable"
    assert (
        client.post(f"{base}/runs/{run.run_id}/dismiss", json={}).json()["item"]["state"]
        == "dismissed"
    )
    assert client.get(base).json()["inbox"] == []


def test_open_report_is_retained_and_dismiss_restarts_retention(setup):
    client, store, project = setup
    run = _report(store, project)
    assert store.artifact("report").expires_at is None
    # Old/stale metadata must never overrule the open Inbox row's retention.
    with store.connection() as conn:
        conn.execute(
            "UPDATE artifacts SET metadata=json_set(metadata, '$.expires_at', ?) WHERE artifact_id='report'",
            (store.now(),),
        )
    assert (
        store.expire_artifacts(as_of=datetime.fromisoformat(store.now()) + timedelta(days=30)) == 0
    )
    before = datetime.fromisoformat(store.now())
    response = client.post(
        f"/api/projects/{project}/consolidation/runs/{run.run_id}/dismiss", json={}
    )
    assert response.status_code == 200
    assert datetime.fromisoformat(store.artifact("report").expires_at) > before


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


@pytest.mark.parametrize("verified", [False, True])
def test_restore_disables_authorizations_and_fails_only_unresolved_runs(setup, verified):
    _, store, project = setup
    closed = _failure(store, project)
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET revisions_verified=? WHERE run_id=?",
            (int(verified), closed.run_id),
        )
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
