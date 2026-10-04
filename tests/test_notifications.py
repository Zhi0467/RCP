from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from rcp.core.models import Patch
from rcp.notifications import NotificationSender
from rcp.server_ops.maintenance import RuntimeAdmissionGate
from rcp.storage import ProjectRecord
from tests.helpers import signed_in_client

from .helpers import append_fixture_patch, authorized_human, create_named_app
from .test_episode_api_serialization import _auto_episode


def _setup(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    device = store.register_notification_device(store.local_owner.user_id)
    return app, store, project_id, device


def _append(app, service, patch):
    # The fixture writer bypasses the production manager's accepted-transition hook.
    append_fixture_patch(service, patch)
    app.state.notification_sender.signal(app.state.default_project_id)


def _blocker_patch(identifier):
    return Patch(
        kind="refresh",
        author="agent",
        summary="fixture",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": identifier,
                        "type": "blocker",
                        "title": "fixture",
                        "description": "fixture",
                        "status": "open",
                    }
                ],
            }
        ],
    )


def test_consolidation_notifications_observed_once_and_closed_items_drop(manifest, tmp_path):
    app, store, project_id, device = _setup(manifest, tmp_path)
    now = datetime.fromisoformat(store.now())
    schedule = store.put_consolidation_schedule(
        project_id,
        local_time="02:00",
        timezone="UTC",
        authorized_by=authorized_human(store),
        next_due_at=(now - timedelta(minutes=1)).isoformat(),
    )
    run = store.claim_consolidation_occurrence(
        schedule,
        occurrence_date=now.date().isoformat(),
        next_due_at=(now + timedelta(days=1)).isoformat(),
        input_head=0,
        error_code="authorizer_not_member",
        error_message="Authorization refused.",
    )
    sender = app.state.notification_sender
    sender.run_pass()
    sender.run_pass()
    assert [row["item_id"] for row in store.notification_outbox()] == [run.run_id]
    assert sender.pending_desktop(device["device_id"])[0]["reason"] == "consolidation"
    store.resolve_consolidation_run(
        project_id, run.run_id, state="dismissed", resolved_by=authorized_human(store)
    )
    assert not store.guard_notification_delivery(
        device["device_id"], store.notification_outbox()[0]["notification_id"]
    )
    assert sender.pending_desktop(device["device_id"]) == []
    sender.run_pass()
    assert store.notification_outbox() == []


def test_consolidation_expiry_notice_observes_offline_expiry_and_rejects_old_generation(
    manifest, tmp_path
):
    from rcp.limits import CONSOLIDATION_AUTHORIZATION_DAYS

    app, store, project_id, device = _setup(manifest, tmp_path)
    now = datetime.fromisoformat(store.now())
    schedule = store.put_consolidation_schedule(
        project_id,
        local_time="02:00",
        timezone="UTC",
        authorized_by=authorized_human(store),
        next_due_at=now.isoformat(),
        now=(now - timedelta(days=CONSOLIDATION_AUTHORIZATION_DAYS + 1)).isoformat(),
    )
    sender = app.state.notification_sender
    sender.run_pass()
    sender.run_pass()
    assert [row["item_id"] for row in store.notification_outbox()] == [schedule.authorization_id]
    renewed = store.put_consolidation_schedule(
        project_id,
        local_time="02:00",
        timezone="UTC",
        authorized_by=authorized_human(store),
        next_due_at=now.isoformat(),
    )
    assert renewed.authorization_id != schedule.authorization_id
    assert sender.pending_desktop(device["device_id"]) == []
    sender.run_pass()
    assert store.notification_outbox() == []


@pytest.mark.parametrize("nonempty", [False, True])
def test_graph_baseline_restart_reopen_and_no_watchers(manifest, tmp_path, nonempty):
    app, store, project_id, device = _setup(manifest, tmp_path)
    service = app.state.catalog.open(project_id)
    if nonempty:
        _append(app, service, _blocker_patch("blk/baseline"))
    sender = app.state.notification_sender
    sender.run_pass()
    assert store.notification_graph_marker(project_id) is not None
    assert store.notification_outbox() == []
    _append(app, service, _blocker_patch("blk/new"))
    sender.run_pass()
    assert [row["item_id"] for row in store.notification_outbox()] == ["blk/new"]
    # Canonical work accepted while the owner is down is replayed on restart.
    _append(app, service, _blocker_patch("blk/down"))
    restarted = NotificationSender(store, app.state.catalog, admission=RuntimeAdmissionGate())
    restarted.run_pass()
    restarted.run_pass()
    assert {row["item_id"] for row in store.notification_outbox()} == {"blk/new", "blk/down"}
    assert store.active_graph_watchers(project_id) == []
    assert len(restarted.pending_desktop(device["device_id"])) == 2


def test_disabled_kinds_advance_the_marker_and_dirty_projects_hold(manifest, tmp_path):
    app, store, project_id, device = _setup(manifest, tmp_path)
    service = app.state.catalog.open(project_id)
    sender = app.state.notification_sender
    owner = store.local_owner.user_id
    off = {"proposal": False, "decision": False, "blocker": False}
    sender.run_pass()
    store.set_notification_preferences(project_id, owner, off)
    _append(app, service, _blocker_patch("blk/while-off"))
    sender.run_pass()
    store.set_notification_preferences(project_id, owner, {"blocker": True})
    sender.run_pass()
    # Attention from while every kind was off is not replayed as new.
    assert store.notification_outbox() == []
    _append(app, service, _blocker_patch("blk/on"))
    sender.run_pass()
    assert [row["item_id"] for row in store.notification_outbox()] == ["blk/on"]
    _append(
        app,
        service,
        Patch(
            kind="refresh",
            author="agent",
            summary="fixture",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [{"id": "blk/on", "changes": {"status": "resolved"}}],
                }
            ],
        ),
    )
    # The marker predates the resolution, so a pull before the pass holds the item,
    # and so does a pull while the pass is still reconciling that project.
    assert sender.pending_desktop(device["device_id"]) == []
    assert len(store.notification_outbox()) == 1
    reconcile, pulled = sender.reconcile_project, []
    sender.reconcile_project = lambda project: (
        pulled.append(sender.pending_desktop(device["device_id"])),
        reconcile(project),
    )
    sender.run_pass()
    assert pulled == [[]]
    assert sender.pending_desktop(device["device_id"]) == []
    assert store.notification_outbox() == []


def test_posted_receipts_expire_after_the_delivery_window(manifest, tmp_path):
    app, store, project_id, device = _setup(manifest, tmp_path)
    service = app.state.catalog.open(project_id)
    sender = app.state.notification_sender
    sender.run_pass()
    _append(app, service, _blocker_patch("blk/posted"))
    sender.run_pass()
    [row] = sender.pending_desktop(device["device_id"])
    store.acknowledge_notification(device["device_id"], row["notification_id"], posted=True)
    sender.run_pass()
    assert len(store.notification_outbox()) == 1
    store.expire_notification_receipts("9999-01-01T00:00:00+00:00")
    assert store.notification_outbox() == []


def test_unreachable_graph_does_not_advance_or_enqueue(manifest, tmp_path, monkeypatch):
    app, store, project_id, _ = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    sender.run_pass()
    marker = store.notification_graph_marker(project_id)
    opened = []

    def offline(project):
        opened.append(project)
        raise OSError("offline")

    monkeypatch.setattr(app.state.catalog, "open", offline)
    # A pass with no accepted change does not replay history.
    sender.run_pass()
    assert opened == []
    sender.signal(project_id)
    sender.run_pass()
    assert store.notification_graph_marker(project_id) == marker
    assert store.notification_outbox() == []
    # A failed replay stays pending for the next pass.
    sender.run_pass()
    assert opened == [project_id, project_id]


def test_episode_baseline_and_finishes_between_passes_or_while_down(tmp_path):
    from rcp.storage import AppStore

    from .test_episode_api_serialization import _project

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    store.seat_project_member("project", store.local_owner.user_id)
    store.register_notification_device(store.local_owner.user_id)
    store.set_notification_preferences(
        "project", store.local_owner.user_id, {"episode_finished": True}
    )
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    old, _ = _auto_episode(store, "old")
    store.end_episode_without_report(old.episode_id, ending="completed")
    sender.run_pass()
    assert store.notification_outbox() == []
    between, _ = _auto_episode(store, "between")
    store.end_episode_without_report(between.episode_id, ending="completed")
    sender.run_pass()
    down, _ = _auto_episode(store, "down")
    sender.run_pass()
    store.end_episode_without_report(down.episode_id, ending="failed")
    restarted = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    restarted.run_pass()
    restarted.run_pass()
    assert {row["item_id"] for row in store.notification_outbox()} == {
        between.episode_id,
        down.episode_id,
    }
    assert len(store.notification_outbox()) == 2


def test_proposal_and_reopened_attention_have_distinct_occurrences(manifest, tmp_path):
    app, store, project_id, _ = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    service = app.state.catalog.open(project_id)
    sender.run_pass()
    proposal_patch = Patch(
        kind="refresh",
        author="agent",
        summary="fixture",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "hyp/proposed",
                        "type": "hypothesis",
                        "title": "fixture",
                        "statement": "fixture",
                    }
                ],
            },
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": "prop/new",
                        "title": "fixture",
                        "card": {"decision_needed": "fixture"},
                        "ops": [
                            {
                                "op": "remove_nodes",
                                "intent": "removal",
                                "node_ids": ["hyp/proposed"],
                            }
                        ],
                    }
                ],
            },
        ],
    )
    _append(app, service, proposal_patch.model_copy(update={"ops": proposal_patch.ops[:1]}))
    _append(app, service, proposal_patch.model_copy(update={"ops": proposal_patch.ops[1:]}))
    _append(app, service, _blocker_patch("blk/reopen"))
    sender.run_pass()
    for status in ("resolved", "open"):
        _append(
            app,
            service,
            Patch(
                kind="refresh",
                author="agent",
                summary="fixture",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [{"id": "blk/reopen", "changes": {"status": status}}],
                    }
                ],
            ),
        )
        sender.run_pass()
    rows = store.notification_outbox()
    assert len([row for row in rows if row["item_id"] == "prop/new"]) == 1
    reopened = [row for row in rows if row["item_id"] == "blk/reopen"]
    assert len(reopened) == 2
    assert len({row["notification_id"] for row in reopened}) == 2


def test_wrapup_sign_in_changes_observation_without_changing_health(tmp_path, monkeypatch):
    from rcp.api.episodes import serialize_episode
    from rcp.storage import AppStore

    from .test_episode_api_serialization import _begin_report, _branch_summary, _project

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    store.seat_project_member("project", store.local_owner.user_id)
    store.register_notification_device(store.local_owner.user_id)
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    episode, root = _auto_episode(store, "wrapup")
    _begin_report(store, episode, root, ending="completed")
    blocked = [False]

    def refusal(*args):
        return "blocked" if blocked[0] else None

    monkeypatch.setattr("rcp.agents.provider_accounts.account_login_refusal", refusal)
    monkeypatch.setattr("rcp.api.episodes.account_login_refusal", refusal)
    observed_at = store.now()
    monkeypatch.setattr(store, "now", lambda: observed_at)
    sender.run_pass()
    assert (
        store.notification_episode_observations("project")[episode.episode_id]["health"]
        == "wrapping_up"
    )
    blocked[0] = True
    sender.run_pass()
    sender.run_pass()
    response = serialize_episode(
        store, "project", store.episode(episode.episode_id), branch_summary=_branch_summary
    )
    assert (response.health, response.blocked_reason) == ("wrapping_up", "sign_in")
    rows = store.notification_outbox()
    assert [(row["item_id"], row["kind"]) for row in rows] == [
        (episode.episode_id, "episode_needs_action")
    ]
    blocked[0] = False
    sender.run_pass()
    blocked[0] = True
    sender.run_pass()
    assert len({row["notification_id"] for row in store.notification_outbox()}) == 2


def test_delivery_drops_changed_observation_even_within_same_kind(tmp_path):
    from rcp.storage import AppStore

    from .test_episode_api_serialization import _project

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    store.seat_project_member("project", store.local_owner.user_id)
    device = store.register_notification_device(store.local_owner.user_id)
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    sender.run_pass()
    episode, root = _auto_episode(store, "changed-observation", root_status="failed")
    sender.run_pass()
    before = store.notification_outbox()
    assert len(before) == 1
    assert (before[0]["observed_health"], before[0]["observed_blocked_reason"]) == (
        "needs_action",
        None,
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET failure_kind='provider_auth' WHERE operation_id=?",
            (root.operation_id,),
        )
    # Delivery itself checks current health, before the next reconciliation pass.
    assert sender.pending_desktop(device["device_id"]) == []
    assert store.notification_outbox() == []
    sender.run_pass()
    after = store.notification_outbox()
    assert len(after) == 1
    assert (after[0]["observed_health"], after[0]["observed_blocked_reason"]) == (
        "needs_action",
        "sign_in",
    )
    assert after[0]["notification_id"] != before[0]["notification_id"]
    assert [row["notification_id"] for row in sender.pending_desktop(device["device_id"])] == [
        after[0]["notification_id"]
    ]


def test_signals_and_pulls_do_not_wait_for_a_running_pass(manifest, tmp_path):
    app, _store, project_id, device = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    done = threading.Event()
    with sender._lock:
        # A pass holds this lock across remote reads and push requests.
        threading.Thread(
            target=lambda: (
                sender.signal(project_id),
                sender.pending_desktop(device["device_id"]),
                done.set(),
            ),
            daemon=True,
        ).start()
        assert done.wait(5)


def test_attention_accepted_during_the_first_baseline_read_is_delivered(
    manifest, tmp_path, monkeypatch
):
    app, store, project_id, device = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    open_project = app.state.catalog.open
    entered = threading.Event()
    release = threading.Event()

    def slow_open(identifier):
        entered.set()
        assert release.wait(10), "graph read was not released"
        return open_project(identifier)

    monkeypatch.setattr(app.state.catalog, "open", slow_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_pass = executor.submit(sender.run_pass)
        assert entered.wait(5), "the first pass did not begin its graph read"
        # The API is serving: a human accepts attention before the baseline exists.
        _, result = append_fixture_patch(open_project(project_id), _blocker_patch("blk/serving"))
        sender.signal(project_id, result.state.revision)
        release.set()
        first_pass.result(timeout=10)
    assert [row["item_id"] for row in store.notification_outbox()] == ["blk/serving"]
    assert store.notification_graph_marker(project_id)["revision"] == result.state.revision
    # The signal re-dirtied the project mid-pass, so pulls hold it once more.
    assert sender.pending_desktop(device["device_id"]) == []
    sender.run_pass()
    assert len(store.notification_outbox()) == 1
    assert len(sender.pending_desktop(device["device_id"])) == 1


def test_a_late_signal_recovers_only_a_silent_first_baseline(manifest, tmp_path):
    app, store, project_id, _device = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    service = app.state.catalog.open(project_id)
    # The first read saw the accepted transition before its signal arrived.
    _, result = append_fixture_patch(service, _blocker_patch("blk/raced"))
    sender.run_pass()
    assert store.notification_outbox() == []
    sender.signal(project_id, result.state.revision)
    sender.run_pass()
    assert [row["item_id"] for row in store.notification_outbox()] == ["blk/raced"]
    assert store.notification_graph_marker(project_id)["revision"] == result.state.revision
    # A repeated signal, or one after a restart, never replays a delivered boundary.
    sender.signal(project_id, result.state.revision)
    sender.run_pass()
    restarted = NotificationSender(store, app.state.catalog, admission=RuntimeAdmissionGate())
    restarted.signal(project_id, 1)
    restarted.run_pass()
    assert len(store.notification_outbox()) == 1


@pytest.mark.parametrize("resolved", [False, True])
def test_late_startup_signal_preserves_newer_delivery(manifest, tmp_path, resolved):
    app, store, project_id, device = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    service = app.state.catalog.open(project_id)
    # The canonical write is visible before its post-commit callback runs.
    _, first = append_fixture_patch(service, _blocker_patch("blk/first"))
    sender.run_pass()
    _, second = append_fixture_patch(service, _blocker_patch("blk/second"))
    sender.signal(project_id, second.state.revision)
    sender.run_pass()
    [delivered] = sender.pending_desktop(device["device_id"])
    store.acknowledge_notification(device["device_id"], delivered["notification_id"], posted=True)
    [receipt] = store.notification_outbox()
    if resolved:
        _, resolution = append_fixture_patch(
            service,
            Patch(
                kind="refresh",
                author="agent",
                summary="Resolve first attention before its delayed signal",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [{"id": "blk/first", "changes": {"status": "resolved"}}],
                    }
                ],
            ),
        )
        sender.signal(project_id, resolution.state.revision)
        sender.run_pass()
    marker = store.notification_graph_marker(project_id)
    sender.signal(project_id, first.state.revision)
    sender.run_pass()
    assert store.notification_graph_marker(project_id) == marker
    pending = sender.pending_desktop(device["device_id"])
    assert [item["reason"] for item in pending] == ([] if resolved else ["blocker"])
    rows = store.notification_outbox()
    assert next(row for row in rows if row["item_id"] == "blk/second") == receipt
    assert {row["item_id"] for row in rows} == (
        {"blk/second"} if resolved else {"blk/first", "blk/second"}
    )
    sender.signal(project_id, first.state.revision)
    sender.run_pass()
    assert store.notification_outbox() == rows


def test_stop_yields_between_projects_after_every_episode_baseline(manifest, tmp_path, monkeypatch):
    app, store, project_id, _device = _setup(manifest, tmp_path)
    other = "other-project"
    store.upsert_project(
        ProjectRecord(
            project_id=other,
            locator=str(tmp_path / other / "research.yaml"),
            name=other,
            state_location=str(tmp_path / other / ".research"),
            state_remote=False,
            added_at=store.now(),
        )
    )
    sender = app.state.notification_sender
    open_project = app.state.catalog.open
    opened = []
    entered = threading.Event()
    release = threading.Event()

    def slow_open(identifier):
        opened.append(identifier)
        entered.set()
        assert release.wait(10), "graph read was not released"
        return open_project(identifier)

    monkeypatch.setattr(app.state.catalog, "open", slow_open)
    sender.start()
    try:
        assert entered.wait(5), "the first pass did not begin its graph read"
        # Local episode observation precedes every remote graph read.
        assert store.notification_project_baseline(project_id) is not None
        assert store.notification_project_baseline(other) is not None
        # An update boundary cannot interrupt the read it finds in flight ...
        sender.stop(timeout=0.2)
        assert sender.is_running()
    finally:
        release.set()
    sender._thread.join(5)
    # ... but the pass yields before the next project, which stays dirty.
    assert not sender.is_running()
    assert len(opened) == 1
    [remaining] = {project_id, other} - set(opened)
    assert remaining in sender._dirty
    sender.run_pass()
    assert store.notification_graph_marker(project_id) is not None


@pytest.mark.parametrize("restarting", [False, True])
def test_startup_serves_while_graph_reconciliation_is_blocked(
    manifest, tmp_path, monkeypatch, restarting
):
    app, store, project_id, device = _setup(manifest, tmp_path)
    service = app.state.catalog.open(project_id)
    if restarting:
        app.state.notification_sender.run_pass()
    _append(app, service, _blocker_patch("blk/before-startup"))
    if restarting:
        app.state.notification_sender.run_pass()
        assert len(store.notification_outbox()) == 1
        _append(
            app,
            service,
            Patch(
                kind="refresh",
                author="agent",
                summary="Resolve attention while the sender is down",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [{"id": "blk/before-startup", "changes": {"status": "resolved"}}],
                    }
                ],
            ),
        )
    # A fresh application must reconcile existing history before delivering it,
    # but a remote graph read must not hold its health endpoint closed.
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    sender = app.state.notification_sender
    entered = threading.Event()
    release = threading.Event()
    serving = threading.Event()
    open_project = app.state.catalog.open

    def slow_open(identifier):
        # Only the sender's read is slow; any other startup or request path
        # that opens a project must not be mistaken for the readiness gate.
        if threading.current_thread().name != "rcp-notifications":
            return open_project(identifier)
        entered.set()
        assert release.wait(10), "graph read was not released"
        return open_project(identifier)

    monkeypatch.setattr(app.state.catalog, "open", slow_open)

    def serve():
        with signed_in_client(app) as client:
            health = client.get("/api/health")
            pending = client.get(f"/api/notifications/devices/{device['device_id']}/pending")
            serving.set()
            assert release.wait(10), "test did not finish checking startup"
            return health, pending

    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(serve)
        try:
            assert entered.wait(5), "startup did not begin graph reconciliation"
            assert serving.wait(5), "API startup waited for the remote graph"
            # Episode health is local, so its baseline precedes serving even
            # while the graph read is still blocked.
            assert store.notification_project_baseline(project_id) is not None
        finally:
            release.set()
        health, pending = request.result(timeout=10)

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert pending.status_code == 200
    assert pending.json() == []
    assert not sender.is_running()
    marker = store.notification_graph_marker(project_id)
    assert marker is not None
    assert marker["revision"] == service.history.materialize(write_outputs=False).state.revision
    # Old attention is a silent first baseline; stale queued attention after a
    # restart is dropped once the resolution has been reconciled.
    assert sender.pending_desktop(device["device_id"]) == []
    assert store.notification_outbox() == []
    _append(app, service, _blocker_patch("blk/after-startup"))
    sender.run_pass()
    [row] = store.notification_outbox()
    assert row["item_id"] == "blk/after-startup"
    assert [item["notification_id"] for item in sender.pending_desktop(device["device_id"])] == [
        row["notification_id"]
    ]
