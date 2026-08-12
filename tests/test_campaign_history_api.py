from __future__ import annotations

from fastapi.testclient import TestClient

from rcp.storage import AppStore, CampaignRecord, CampaignReportRecord

from .helpers import authorized_human, create_named_app


def _summary(revision: int, campaign_id: str | None) -> dict[str, object]:
    return {
        "from_revision": revision - 1,
        "to_revision": revision,
        "kind": "work",
        "author": "agent",
        "producer": "agent",
        "authorized_by": None,
        "profile": "ordinary",
        "task_id": f"task-{revision}",
        "campaign_id": campaign_id,
        "created_at": f"2026-08-12T00:00:0{revision}+00:00",
        "sentences": [f"Recorded revision {revision}."],
    }


def _campaign(
    store: AppStore,
    *,
    campaign_id: str,
    project_id: str,
    status: str,
    ending: str | None = None,
) -> CampaignRecord:
    now = store.now()
    return CampaignRecord(
        campaign_id=campaign_id,
        project_id=project_id,
        status=status,
        invocation_ceiling=2,
        authorized_by=authorized_human(store),
        ending=ending,
        created_at=now,
        updated_at=now,
    )


def test_history_campaign_decoration_keeps_missing_and_cross_project_ids(
    manifest,
    monkeypatch,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    history = app.state.catalog.open(project_id).history
    summaries = [
        _summary(1, None),
        _summary(2, "removed-campaign"),
        _summary(3, "foreign-campaign"),
    ]
    foreign = _campaign(
        store,
        campaign_id="foreign-campaign",
        project_id="another-project",
        status="running",
    )
    events: list[str] = []

    def project_history(_from_revision: int, _to_revision: int | None):
        events.append("projection")
        return summaries

    def campaign_lookup(campaign_id: str):
        events.append(f"campaign:{campaign_id}")
        return foreign if campaign_id == foreign.campaign_id else None

    def unexpected_report_lookup(_campaign_id: str):
        raise AssertionError("absent and cross-project campaigns must not load reports")

    monkeypatch.setattr(history, "revision_summaries", project_history)
    monkeypatch.setattr(store, "campaign", campaign_lookup)
    monkeypatch.setattr(store, "campaign_reports", unexpected_report_lookup)

    response = TestClient(app).get(f"/api/projects/{project_id}/history/summaries")

    assert response.status_code == 200
    assert events[0] == "projection"
    assert set(events[1:]) == {"campaign:removed-campaign", "campaign:foreign-campaign"}
    assert [(item["campaign_id"], item["campaign"]) for item in response.json()] == [
        (None, None),
        ("removed-campaign", None),
        ("foreign-campaign", None),
    ]


def test_history_campaign_decoration_maps_live_state_and_latest_report(
    manifest,
    monkeypatch,
    tmp_path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    history = app.state.catalog.open(project_id).history
    campaigns = {
        "completed": _campaign(
            store,
            campaign_id="completed",
            project_id=project_id,
            status="succeeded",
            ending="completed",
        ),
        "exhausted": _campaign(
            store,
            campaign_id="exhausted",
            project_id=project_id,
            status="needs_action",
            ending="exhausted",
        ),
        "stopped": _campaign(
            store,
            campaign_id="stopped",
            project_id=project_id,
            status="stopped",
            ending="stopped",
        ),
        "failed": _campaign(
            store,
            campaign_id="failed",
            project_id=project_id,
            status="failed",
            ending="failed",
        ),
        "running": _campaign(
            store,
            campaign_id="running",
            project_id=project_id,
            status="wrapping_up",
            ending="completed",
        ),
    }
    summaries = [
        _summary(index, campaign_id) for index, campaign_id in enumerate(campaigns, start=1)
    ]
    reports = [
        CampaignReportRecord(
            report_id="older-report",
            campaign_id="exhausted",
            operation_id="older-report-task",
            ending="exhausted",
            sha256="0" * 64,
            html="<p>Older report</p>",
            created_at="2026-08-12T00:00:00+00:00",
        ),
        CampaignReportRecord(
            report_id="latest-report",
            campaign_id="exhausted",
            operation_id="latest-report-task",
            ending="exhausted",
            sha256="1" * 64,
            html="<p>Latest report</p>",
            created_at="2026-08-12T01:00:00+00:00",
        ),
    ]
    events: list[str] = []

    def project_history(_from_revision: int, _to_revision: int | None):
        events.append("projection")
        return summaries

    def campaign_lookup(campaign_id: str):
        events.append(f"campaign:{campaign_id}")
        return campaigns[campaign_id]

    def report_lookup(campaign_id: str):
        return reports if campaign_id == "exhausted" else []

    monkeypatch.setattr(history, "revision_summaries", project_history)
    monkeypatch.setattr(store, "campaign", campaign_lookup)
    monkeypatch.setattr(store, "campaign_reports", report_lookup)

    response = TestClient(app).get(f"/api/projects/{project_id}/history/summaries")

    assert response.status_code == 200
    assert events[0] == "projection"
    decorated = {item["campaign_id"]: item["campaign"] for item in response.json()}
    assert {campaign_id: item["state"] for campaign_id, item in decorated.items()} == {
        "completed": "completed",
        "exhausted": "exhausted",
        "stopped": "stopped",
        "failed": "failed",
        "running": "running",
    }
    assert decorated["exhausted"]["report"] == {
        "report_id": "latest-report",
        "ending": "exhausted",
        "created_at": "2026-08-12T01:00:00+00:00",
    }
    assert all(
        item["report"] is None
        for campaign_id, item in decorated.items()
        if campaign_id != "exhausted"
    )
