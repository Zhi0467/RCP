from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from rcp.api.episode_timeline import build_episode_timeline
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.storage import AgentTaskRecord, AppStore, AutoResearchStateRecord, EpisodeRecord

from .helpers import authorized_human, create_named_app


def seed_episode_timeline(store: AppStore, project_id: str) -> EpisodeRecord:
    """A disposable incident-shaped record, shared with the served-app smoke check."""
    episode_id = str(uuid.uuid4())
    now = datetime.now(UTC) - timedelta(hours=1)

    def at(minute: int) -> str:
        return (now + timedelta(minutes=minute)).isoformat()

    author = authorized_human(store)
    target = GraphTargetRef(kind="branch", branch_id=episode_id)
    episode = EpisodeRecord(
        episode_id=episode_id,
        project_id=project_id,
        mode="auto_research",
        status="queued",
        invocation_ceiling=4,
        graph_target=target,
        graph_base_head=GraphHeadRef(revision=0),
        authorized_by=author,
        created_at=at(0),
        updated_at=at(0),
    )
    root = AgentTaskRecord(
        operation_id=f"{episode_id}-root",
        project_id=project_id,
        episode_id=episode_id,
        graph_target=target,
        kind="auto_research",
        status="queued",
        request={
            "episode_id": episode_id,
            "role": "orchestrator",
            "actor_operation_id": f"{episode_id}-root",
            "run_truth_scope": ["repo"],
        },
        created_at=at(0),
        updated_at=at(0),
        status_message="Root turn",
        authorized_by=author,
        dispatch_authority=AgentDispatchAuthority(
            profile="orchestrator",
            task_contract="orchestrate",
            scope=AgentDispatchScope(
                run_truth_scope=["repo"], episode_id=episode_id, patch_kind="work"
            ),
        ),
    )
    store.create_auto_research_episode_with_root_task(
        episode,
        AutoResearchStateRecord(
            episode_id=episode_id,
            starting_instruction="Trace the evidence and report results.",
            created_at=at(0),
            updated_at=at(0),
        ),
        root,
    )
    ids = {name: f"{episode_id}-{name}" for name in ("root", "worker", "wake", "retry")}
    with store.connection() as connection:
        for index, name in enumerate(("worker", "wake", "retry"), 1):
            task = root.model_copy(
                update={
                    "operation_id": ids[name],
                    "created_at": at(index),
                    "updated_at": at(index),
                    "status_message": name.capitalize(),
                    "attempt": 2 if name == "retry" else 1,
                    "parent_operation_id": ids["wake"] if name == "retry" else ids["root"],
                    "dispatch_authority": AgentDispatchAuthority(
                        profile="ordinary",
                        task_contract="work_auto",
                        scope=root.dispatch_authority.scope,
                    )
                    if name == "worker"
                    else root.dispatch_authority,
                    "request": {**root.request, "wake_cause": "watcher_completed"}
                    if name == "wake"
                    else {**root.request, "role": "worker", "actor_operation_id": ids[name]}
                    if name == "worker"
                    else root.request,
                }
            )
            if name == "worker":
                task = task.model_copy(
                    update={
                        "kind": "node_chat",
                        "parent_operation_id": None,
                        "request": {
                            "mode": "work",
                            "trigger": "orchestrator",
                            "node_id": "exp/timeline",
                        },
                        "dispatch_authority": None,
                    }
                )
            store._insert_agent_task(
                connection, task, continuation_cause="watcher_wake" if name == "wake" else "fresh"
            )
            if name != "worker":
                connection.execute(
                    "INSERT INTO auto_research_invocations (episode_id, operation_id, allocation_operation_id, role, actor_operation_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        episode_id,
                        ids[name],
                        ids[name],
                        "worker" if name == "worker" else "orchestrator",
                        ids[name] if name == "worker" else ids["root"],
                        at(index),
                    ),
                )
            connection.execute(
                "INSERT INTO episode_invocations (episode_id, operation_id, invocation_number, created_at) VALUES (?, ?, ?, ?)",
                (episode_id, ids[name], index + 1, at(index)),
            )
        connection.execute(
            "UPDATE graph_runs SET status='succeeded', finished_at=? WHERE episode_id=?",
            (at(4), episode_id),
        )
        connection.execute(
            "INSERT INTO auto_research_child_work (worker_id, episode_id, project_id, control_node_id, root_operation_id, current_operation_id, admitted_by_operation_id, instruction, instruction_sha256, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ids["worker"],
                episode_id,
                project_id,
                "exp/timeline",
                ids["worker"],
                ids["worker"],
                ids["root"],
                "Investigate",
                hashlib.sha256(b"Investigate").hexdigest(),
                at(1),
                at(4),
            ),
        )
        connection.execute(
            "INSERT INTO auto_research_child_work_attempts (operation_id, worker_id, allocation_operation_id, created_at) VALUES (?, ?, ?, ?)",
            (ids["worker"], ids["worker"], ids["worker"], at(1)),
        )
        connection.execute(
            "INSERT INTO auto_research_recoveries (recovery_id, episode_id, operation_id, failure_kind, retry_mode, attempts, max_attempts, status, diagnostic, admitted_operation_id, created_at, updated_at) VALUES (?, ?, ?, 'provider_transient', 'exact', 3, 3, 'exhausted', 'Automatic recovery exhausted', ?, ?, ?)",
            (f"{episode_id}-recovery", episode_id, ids["wake"], ids["retry"], at(3), at(5)),
        )
        for index in range(2):
            connection.execute(
                "INSERT INTO auto_research_lifecycle_notices (notice_id, episode_id, source_kind, source_id, source_event, state, payload_json, created_at, delivered_at, delivery_operation_id) VALUES (?, ?, 'watcher', ?, 'completed', 'delivered', '{}', ?, ?, ?)",
                (f"{episode_id}-notice-{index}", episode_id, str(index), at(1), at(2), ids["wake"]),
            )
        for name, role, body, delivery in (
            ("mail", "worker", "Worker result\n\n" + "Evidence " * 90, ids["wake"]),
            ("human", "human", "Please check the final evidence.", None),
        ):
            connection.execute(
                "INSERT INTO auto_research_messages (message_id, episode_id, sender_role, sender_task_id, authorized_space_id, authorized_user_id, authorized_display_name, recipient_task_id, body, created_at, delivered_at, delivery_operation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{episode_id}-{name}",
                    episode_id,
                    role,
                    ids["worker"] if role == "worker" else None,
                    author.space_id if role == "human" else None,
                    author.user_id if role == "human" else None,
                    author.display_name if role == "human" else None,
                    ids["root"],
                    body,
                    at(1),
                    at(2) if delivery else None,
                    delivery,
                ),
            )
        connection.execute(
            "UPDATE episodes SET status='needs_action', ending='exhausted', wrapup_state='failed', wrapup_error='Report could not be produced', invocations_used=4, stop_requested_at=?, stop_initiated_by=?, ended_at=?, updated_at=? WHERE episode_id=?",
            (at(5), f"human:{author.user_id}", at(6), at(6), episode_id),
        )
    child_id = str(uuid.uuid4())
    store.create_episode(
        EpisodeRecord(
            episode_id=child_id,
            project_id=project_id,
            mode="experiment_loop",
            graph_target=target,
            graph_base_head=GraphHeadRef(revision=0),
            control_node_id="exp/timeline",
            status="queued",
            authorized_by=author,
            invocation_ceiling=1,
            created_at=at(1),
            updated_at=at(4),
        )
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='completed', ending='completed', ended_at=? WHERE episode_id=?",
            (at(4), child_id),
        )
        connection.execute(
            "INSERT INTO auto_research_child_experiments (child_episode_id, auto_research_episode_id, project_id, control_node_id, state, request_json, parent_operation_id, created_at, updated_at) VALUES (?, ?, ?, 'exp/timeline', 'terminal', '{}', ?, ?, ?)",
            (child_id, episode_id, project_id, ids["root"], at(1), at(4)),
        )
    result = store.episode(episode_id)
    assert result is not None
    return result


def test_timeline_facts_route_ownership_and_read_only(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    episode = seed_episode_timeline(store, project_id)
    with store.connection() as connection:
        before = list(connection.iterdump())
    client = TestClient(app)
    response = client.get(f"/api/projects/{project_id}/episodes/{episode.episode_id}/timeline")
    assert response.status_code == 200, response.text
    assert (
        client.get(f"/api/projects/foreign/episodes/{episode.episode_id}/timeline").status_code
        == 404
    )
    with store.connection() as connection:
        after = list(connection.iterdump())
    assert before == after
    data = response.json()
    events = data["events"]
    assert events == sorted(events, key=lambda event: (event["at"], event["event_id"]))
    by_id = {event["event_id"]: event for event in events}
    prefix = episode.episode_id
    root = f"turn:{prefix}-root"
    wake = f"wake:{prefix}-wake"
    assert by_id[f"turn:{prefix}-worker"]["parent_event_id"] == root
    assert by_id[wake]["cause"] == "watcher_completed"
    assert by_id[f"retry:{prefix}-retry"]["parent_event_id"] == wake
    assert by_id[f"retry:{prefix}-retry"]["cause"] == "automatic recovery"
    assert by_id[f"mail:{prefix}-mail"]["parent_event_id"] == wake
    assert len(by_id[f"mail:{prefix}-mail"]["detail"]) == 500
    assert by_id[f"mail:{prefix}-human"]["actor"]["member"] == episode.authorized_by.model_dump()
    assert by_id[f"mail:{prefix}-human"]["status"] == "pending"
    for index in range(2):
        assert by_id[f"notice:{prefix}-notice-{index}"]["parent_event_id"] == wake
    child = next(event for event in events if event["title"] == "Experiment created")
    assert child["parent_event_id"] == root
    ended = next(event for event in events if event["title"] == "Experiment ended")
    assert ended["parent_event_id"] == child["event_id"]
    assert ended["status"] == "completed"
    stop = next(event for event in events if event["title"] == "Stopped")
    assert stop["actor"]["member"] == episode.authorized_by.model_dump()
    assert by_id["lifecycle:ending_fenced"]["status"] == "exhausted"
    assert any(event["title"] == "Recovery exhausted" for event in events)
    assert not data["truncated"]
    # The requested project exists and the caller is its member; the episode
    # itself belongs elsewhere, so the ownership check must still refuse it.
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET project_id=? WHERE episode_id=?",
            (str(uuid.uuid4()), episode.episode_id),
        )
    assert (
        client.get(f"/api/projects/{project_id}/episodes/{episode.episode_id}/timeline").status_code
        == 404
    )


def test_unknown_stop_and_truncation(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    episode = seed_episode_timeline(store, app.state.default_project_id)
    unknown = episode.model_copy(update={"stop_initiated_by": None})
    stop = next(
        event for event in build_episode_timeline(store, unknown).events if event.title == "Stopped"
    )
    assert stop.provenance == "unknown"
    with store.connection() as connection:
        for index in range(401):
            connection.execute(
                "INSERT INTO auto_research_lifecycle_notices (notice_id, episode_id, source_kind, source_id, source_event, state, payload_json, created_at) VALUES (?, ?, 'watcher', ?, 'completed', 'pending', '{}', ?)",
                (f"extra-{index:04}", episode.episode_id, f"extra-{index}", episode.updated_at),
            )
    response = build_episode_timeline(store, episode)
    assert response.truncated
    assert len(response.events) == 400
    assert response.events[-1].event_id == "notice:extra-0400"
    assert all(event.at == episode.updated_at for event in response.events)


def test_experiment_timeline_watcher_history(tmp_path):
    from rcp.storage import GraphWatcherRecord, WatcherRecord
    from rcp.storage.models import NodeStatusGraphCondition

    from .test_episode_api_serialization import _project
    from .test_experiment_episode_storage import _admit_root, _bind, _continuation, _task

    store = AppStore(tmp_path / "data")
    _project(store)
    episode_id, root = _admit_root(store)
    _bind(store, episode_id, root.operation_id, invocation=1)
    now = store.now()
    wake = _task(
        store,
        "watcher-wake",
        episode_id,
        trigger="watcher",
        invocation=2,
        session_id="native-session",
        stage_root="/tmp/exact-experiment-stage",
    )
    with store.connection() as connection:
        store._insert_agent_task(connection, wake, continuation_cause="watcher_wake")
    common = dict(
        project_id="project",
        origin_operation_id=root.operation_id,
        origin_task_kind="node_chat",
        chat_id="episode-chat",
        node_id="exp-one",
        episode_id=episode_id,
        continuation=_continuation(episode_id),
        created_at=now,
    )
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id="shell",
                check_command="true",
                log_path="/tmp/check.log",
                cwd="/tmp",
                status="completed",
                completed_at=now,
                notification_operation_id=wake.operation_id,
                **common,
            ),
            GraphWatcherRecord(
                watcher_id="graph",
                armed_revision=1,
                condition=NodeStatusGraphCondition(node_id="exp-one", status_in=["completed"]),
                status="stopped",
                stopped_at=now,
                stop_operation_id=root.operation_id,
                stop_reason="Human stopped observing",
                **common,
            ),
        ]
    )
    episode = store.episode(episode_id)
    assert episode is not None
    response = build_episode_timeline(store, episode)
    by_id = {event.event_id: event for event in response.events}
    assert response.mode == "experiment_loop"
    assert by_id["wake:watcher-wake"].cause == "watcher_wake"
    assert by_id["notice:shell:armed"].parent_event_id == "turn:loop-root"
    assert by_id["notice:shell:completed"].parent_event_id == "wake:watcher-wake"
    assert by_id["notice:graph:stopped"].parent_event_id == "turn:loop-root"


def test_harvested_notice_and_suppressed_wake_provenance(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    episode = seed_episode_timeline(store, app.state.default_project_id)
    prefix = episode.episode_id
    with store.connection() as connection:
        connection.execute(
            "UPDATE auto_research_lifecycle_notices SET delivered_at=NULL, delivery_operation_id=NULL, state='acknowledged', acknowledged_at=?, acknowledged_by=?, acknowledged_operation_id=?, wake_suppressed='self_caused' WHERE notice_id=?",
            (episode.updated_at, f"{prefix}-root", f"{prefix}-wake", f"{prefix}-notice-0"),
        )
        # A notice harvested before the consuming turn was recorded has no proven parent.
        connection.execute(
            "UPDATE auto_research_lifecycle_notices SET delivered_at=NULL, delivery_operation_id=NULL, state='acknowledged', acknowledged_at=?, acknowledged_by=? WHERE notice_id=?",
            (episode.updated_at, f"{prefix}-root", f"{prefix}-notice-1"),
        )
        connection.execute("DELETE FROM auto_research_recoveries WHERE episode_id=?", (prefix,))
    events = {event.event_id: event for event in build_episode_timeline(store, episode).events}
    notice = events[f"notice:{prefix}-notice-0"]
    # The harvesting turn, not the stable actor id that `acknowledged_by` carries.
    assert notice.parent_event_id == f"wake:{prefix}-wake"
    assert events[f"notice:{prefix}-notice-1"].parent_event_id is None
    assert notice.cause == "completed; wake_suppressed=self_caused"
    assert notice.provenance == "recorded"
    assert events[f"turn:{prefix}-worker"].links.control_node_id == "exp/timeline"
    assert events[f"turn:{prefix}-worker"].provenance == "recorded"
    assert events[f"retry:{prefix}-retry"].cause is None
    assert events[f"retry:{prefix}-retry"].provenance == "unknown"


def test_task_degradation_survives_detail_bound(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    episode = seed_episode_timeline(store, app.state.default_project_id)
    note = "Provider ignored the requested reasoning effort and used its own default."
    store.record_agent_task_receipt(
        episode.root_operation_id, "provider_exit", {"degradation": note}
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status_message=? WHERE operation_id=?",
            ("Status " * 100, episode.root_operation_id),
        )
    event = next(
        event
        for event in build_episode_timeline(store, episode).events
        if event.links.task_id == episode.root_operation_id
    )
    assert event.detail.startswith(note)
    assert len(event.detail) == 500


def test_timeline_reads_hydrate_only_the_newest_suffix(tmp_path):
    """The timeline keeps the newest events, so its store reads take a ``newest`` bound.

    Bounded reads return the same records as the tail of the full read, in the
    same order; only the rows that can never reach the wire are left unread.
    """

    from .test_branch_target_storage import _create_auto_episode, _store, _worker_authority

    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    for index in range(3):
        worker_id = f"worker-{index}"
        store.create_auto_research_agent_task(
            root.model_copy(
                update={
                    "operation_id": worker_id,
                    "parent_operation_id": root.operation_id,
                    "request": {
                        **root.request,
                        "role": "worker",
                        "actor_operation_id": worker_id,
                        "control_node_id": f"exp/{index}",
                    },
                    "dispatch_authority": _worker_authority(episode.episode_id),
                }
            ),
            role="worker",
        )
    paid = store.auto_research_tasks(episode.episode_id)
    assert len(paid) == 4
    assert store.auto_research_tasks(episode.episode_id, newest=2) == paid[-2:]
    tasks = store.episode_tasks(episode.episode_id)
    assert store.episode_tasks(episode.episode_id, newest=2) == tasks[-2:]
    invocations = store.episode_invocations(episode.episode_id)
    assert [row.invocation_number for row in invocations] == [1, 2, 3, 4]
    assert store.episode_invocations(episode.episode_id, newest=1) == invocations[-1:]
    with store.connection() as connection:
        for recovery_id, created_at, updated_at in (
            ("recovery-old", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"),
            ("recovery-new", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"),
        ):
            connection.execute(
                "INSERT INTO auto_research_recoveries (recovery_id, episode_id, operation_id,"
                " failure_kind, retry_mode, attempts, max_attempts, status, diagnostic,"
                " created_at, updated_at) VALUES (?, ?, ?, 'provider', 'exact', 1, 3,"
                " 'exhausted', 'diag', ?, ?)",
                (recovery_id, episode.episode_id, root.operation_id, created_at, updated_at),
            )
    recoveries = store.auto_research_recoveries(episode.episode_id)
    assert [row.recovery_id for row in recoveries] == ["recovery-old", "recovery-new"]
    # A recovery is an event at its last update, so the bound keeps the latest-updated row.
    assert store.auto_research_recoveries(episode.episode_id, newest=1) == recoveries[:1]
