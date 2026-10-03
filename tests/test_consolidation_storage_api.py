from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rcp.core.models import AuthorizedHuman
from rcp.storage import Artifact

from .helpers import create_named_app


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


def _report(store, project_id):
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
    assert client.get(base).json() == {"schedule": None, "inbox": []}
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
    base = f"/api/projects/{project}/consolidation"
    assert client.post(f"{base}/runs/{run.run_id}/keep", json={}).status_code == 409
    inbox = client.get(base).json()["inbox"]
    assert len(inbox) == 1 and inbox[0]["operation_id"] is None and inbox[0]["revisions_verified"]
    assert (
        client.post(f"{base}/runs/{run.run_id}/dismiss", json={}).json()["item"]["state"]
        == "dismissed"
    )
    assert client.get(base).json()["inbox"] == []


def test_open_report_is_retained_and_dismiss_restarts_retention(setup):
    client, store, project = setup
    run = _report(store, project)
    assert store.artifact("report").expires_at is None
    before = datetime.fromisoformat(store.now())
    response = client.post(
        f"/api/projects/{project}/consolidation/runs/{run.run_id}/dismiss", json={}
    )
    assert response.status_code == 200
    assert datetime.fromisoformat(store.artifact("report").expires_at) > before


@pytest.mark.parametrize("viewer", [False, True])
def test_keeping_report_closes_inbox_from_either_route(setup, viewer):
    client, store, project = setup
    run = _report(store, project)
    url = (
        f"/api/projects/{project}/artifacts/report/keep"
        if viewer
        else f"/api/projects/{project}/consolidation/runs/{run.run_id}/keep"
    )
    assert client.post(url, json={}).status_code == 200
    assert store.consolidation_run(run.run_id).state == "kept"
    assert store.consolidation_run(run.run_id).resolved_by.user_id == store.local_owner.user_id
    assert store.artifact("report").kept_at


def test_restore_disables_authorizations_and_fails_only_unresolved_runs(setup):
    _, store, project = setup
    run = _failure(store, project)
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET outcome_settled_at=NULL WHERE run_id=?", (run.run_id,)
        )
        store.detach_consolidation_for_restore(conn, diagnostic="restored", now=store.now())
    assert store.consolidation_schedule(project) is None
    detached = store.consolidation_run(run.run_id)
    assert detached.error_code == "restored_run_detached" and detached.outcome_settled_at


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


def test_apply_failure_evidence_survives_retry_and_success_clears_it(setup):
    import hashlib

    _, store, project = setup
    run = _failure(store, project)
    with store.connection() as conn:
        conn.execute(
            "UPDATE consolidation_runs SET operation_id=? WHERE run_id=?", ("turn", run.run_id)
        )
    patch = "{}"
    digest = hashlib.sha256(patch.encode()).hexdigest()
    store.reserve_consolidation_apply("turn", "key", digest, patch, "effect")
    store.record_consolidation_apply_failure("turn", "key", "unavailable", "unknown outcome")
    pending = store.list_consolidation_apply_receipts("turn")[0]
    assert pending["result"] is None
    assert pending["last_failure"] == {"status": "unavailable", "message": "unknown outcome"}
    result = {"status": "ok", "result": {"revision": 1}}
    store.finish_consolidation_apply("turn", "key", result)
    store.record_consolidation_apply_failure("turn", "key", "invalid", "late failure")
    completed = store.list_consolidation_apply_receipts("turn")[0]
    assert completed["result"] == result
    assert completed["last_failure"] is None
