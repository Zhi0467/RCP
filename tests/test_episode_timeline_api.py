from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rcp.api.episode_timeline import build_episode_timeline
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import EPISODE_TIMELINE_ERROR_MAX_LENGTH, EPISODE_TIMELINE_HEADLINE_MAX_LENGTH
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


@pytest.fixture
def timeline(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    episode = seed_episode_timeline(store, app.state.default_project_id)
    return store, episode, TestClient(app)


def test_actor_kinds_and_recorded_links(timeline):
    store, episode, client = timeline
    prefix = episode.episode_id
    with store.connection() as connection:
        connection.execute(
            "UPDATE auto_research_child_work SET instruction=?, instruction_sha256=?, stop_requested_at=? WHERE worker_id=?",
            (
                "# Evidence check\nInvestigate",
                hashlib.sha256(b"# Evidence check\nInvestigate").hexdigest(),
                episode.updated_at,
                f"{prefix}-worker",
            ),
        )
        connection.execute(
            "UPDATE graph_runs SET error=? WHERE operation_id=?",
            ("E" * (EPISODE_TIMELINE_ERROR_MAX_LENGTH + 1), f"{prefix}-root"),
        )
        before = list(connection.iterdump())
    response = client.get(f"/api/projects/{episode.project_id}/episodes/{prefix}/timeline")
    assert response.status_code == 200
    data = response.json()
    root_span = next(span for span in data["spans"] if span["task_id"] == f"{prefix}-root")
    assert root_span["error"] == "E" * EPISODE_TIMELINE_ERROR_MAX_LENGTH
    assert {actor["kind"] for actor in data["actors"]} == {
        "human",
        "orchestrator",
        "worker",
        "experiment",
    }
    worker = next(actor for actor in data["actors"] if actor["kind"] == "worker")
    assert worker["label"] == "Evidence check"
    assert worker["subtitle"] == "exp/timeline"
    assert worker["started_by_span_id"] == f"span:{prefix}-root"
    stop = next(
        mark
        for mark in data["marks"]
        if mark["actor_id"] == worker["actor_id"] and mark["kind"] == "stop_requested"
    )
    assert stop["by_span_id"] is None
    assert not data["truncated"]
    with store.connection() as connection:
        assert list(connection.iterdump()) == before
    assert client.get(f"/api/projects/foreign/episodes/{prefix}/timeline").status_code == 404


@pytest.mark.parametrize(
    "disposition", ["wake", "harvested", "cleared", "failed_attempt", "undelivered", "unknown"]
)
def test_message_disposition(timeline, disposition):
    store, episode, _ = timeline
    prefix = episode.episode_id
    message_id = f"{prefix}-mail"
    with store.connection() as connection:
        if disposition in {"harvested", "cleared", "undelivered", "unknown"}:
            connection.execute(
                "UPDATE auto_research_messages SET delivered_at=?, delivery_operation_id=NULL WHERE message_id=?",
                (episode.updated_at if disposition == "unknown" else None, message_id),
            )
        if disposition == "failed_attempt":
            connection.execute(
                "UPDATE graph_runs SET status='failed' WHERE operation_id=?", (f"{prefix}-wake",)
            )
    if disposition in {"harvested", "cleared"}:
        store.process_auto_research_lifecycle_inbox(
            prefix,
            effect_id="consume",
            mode="harvest" if disposition == "harvested" else "clear",
            acknowledged_by=f"{prefix}-root",
            delivery_operation_id=f"{prefix}-retry",
        )
    message = next(
        item
        for item in build_episode_timeline(store, episode).messages
        if item.item_id == f"message:{message_id}"
    )
    assert message.disposition == disposition
    assert message.sent_span_id == f"span:{prefix}-worker"
    expected = (
        f"span:{prefix}-retry"
        if disposition in {"harvested", "cleared"}
        else f"span:{prefix}-wake"
        if disposition in {"wake", "failed_attempt"}
        else None
    )
    assert message.delivered_span_id == expected


@pytest.mark.parametrize("landing", ["woke", "harvested", "acknowledged", None])
def test_signal_landing(timeline, landing):
    store, episode, _ = timeline
    prefix = episode.episode_id
    notice_id = f"{prefix}-notice-0"
    with store.connection() as connection:
        connection.execute(
            "UPDATE auto_research_lifecycle_notices SET payload_json=? WHERE notice_id=?",
            (json.dumps({"detail": "verbatim payload"}), notice_id),
        )
        if landing != "woke":
            connection.execute(
                "UPDATE auto_research_lifecycle_notices SET delivered_at=NULL, delivery_operation_id=NULL, state='pending' WHERE notice_id=?",
                (notice_id,),
            )
    if landing in {"harvested", "acknowledged"}:
        store.process_auto_research_lifecycle_inbox(
            prefix,
            effect_id="consume",
            mode="harvest" if landing == "harvested" else "clear",
            acknowledged_by=f"{prefix}-root",
            delivery_operation_id=f"{prefix}-retry",
        )
    signal = next(
        item
        for item in build_episode_timeline(store, episode).signals
        if item.item_id == f"notice:{notice_id}"
    )
    assert signal.landing == landing
    assert signal.payload == {"detail": "verbatim payload"}
    assert signal.landed_span_id == (
        f"span:{prefix}-wake" if landing == "woke" else f"span:{prefix}-retry" if landing else None
    )
    assert signal.source_actor_id is None  # The watcher id does not name a stored watcher.


def _handoff(store, episode, kind="assignment"):
    from rcp.storage import AutoResearchCommandFileRecord

    from .test_auto_research_children_storage import _admission

    prefix = episode.episode_id
    body = "# Check evidence\n" + "Evidence " * 100
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='running', ending=NULL, wrapup_state='not_started', wrapup_error=NULL, stop_requested_at=NULL, ended_at=NULL, invocation_ceiling=20 WHERE episode_id=?",
            (prefix,),
        )
    child_id = (
        f"{prefix}-worker"
        if kind == "assignment"
        else store.auto_research_child_experiments(prefix)[0].child_episode_id
    )
    admission = _admission(
        store,
        episode,
        admission_id="admission",
        child_kind="work" if kind == "assignment" else "experiment",
        child_id=child_id,
    )
    store.start_agent_command(
        operation_id=f"{prefix}-root",
        command_id="spawn-command",
        episode_id=prefix,
        verb="spawn" if kind == "assignment" else "episode",
        idempotency_key="spawn-key",
        payload={
            "planned_worker_id"
            if kind == "assignment"
            else "planned_episode_effect_id": admission.child_id
        },
        child_admission=admission,
        file_snapshot=AutoResearchCommandFileRecord(
            command_id="spawn-command",
            episode_id=prefix,
            operation_id=f"{prefix}-root",
            kind="instruction" if kind == "assignment" else "goal",
            filename="assignment.md",
            sha256=hashlib.sha256(body.encode()).hexdigest(),
            content=body,
            created_at=store.now(),
        ),
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='completed', ending='completed', ended_at=? WHERE episode_id=?",
            (episode.ended_at, prefix),
        )
    return body


@pytest.mark.parametrize("kind", ["assignment", "goal"])
def test_handoff_joins_planned_child_not_neighboring_task(timeline, kind):
    store, episode, _ = timeline
    assert build_episode_timeline(store, episode).handoffs == []  # No recorded goal or assignment.
    body = _handoff(store, episode, kind)
    response = build_episode_timeline(store, episode)
    assert len(response.handoffs) == 1
    handoff = response.handoffs[0]
    assert handoff.item_id == handoff.text_ref == "handoff:spawn-command"
    assert handoff.kind == kind
    assert handoff.from_span_id == f"span:{episode.episode_id}-root"
    child = next(
        actor
        for actor in response.actors
        if actor.kind == ("worker" if kind == "assignment" else "experiment")
    )
    assert handoff.to_actor_id == child.actor_id
    assert body.startswith(handoff.preview.rstrip("…"))
    from rcp.limits import EPISODE_TIMELINE_PREVIEW_MAX_LENGTH

    assert len(handoff.preview) == EPISODE_TIMELINE_PREVIEW_MAX_LENGTH


def test_bound_counts_spans_and_items_newest_first(timeline, monkeypatch):
    import rcp.api.episode_timeline as projection

    store, episode, _ = timeline
    _handoff(store, episode)
    full = build_episode_timeline(store, episode)

    def retained(response):
        result = [(span.started_at, span.span_id) for span in response.spans]
        result += [(item.at, item.item_id) for item in response.handoffs + response.marks]
        result += [(item.sent_at, item.item_id) for item in response.messages]
        result += [(item.recorded_at, item.item_id) for item in response.signals]
        return sorted(result, reverse=True)

    monkeypatch.setattr(projection, "EPISODE_TIMELINE_EVENT_LIMIT", 5)
    bounded = build_episode_timeline(store, episode)
    assert bounded.truncated
    assert retained(bounded) == retained(full)[:5]
    # The retained handoff keeps its worker and issuing orchestrator; older
    # human and Experiment items no longer contribute rows.
    assert {actor.kind for actor in bounded.actors} == {"worker", "orchestrator"}


def test_chain_joins_moved_child_to_original_issuer(timeline, monkeypatch):
    import rcp.api.episode_timeline as projection

    store, episode, _ = timeline
    _handoff(store, episode)
    store.process_auto_research_lifecycle_inbox(
        episode.episode_id,
        effect_id="chain-harvest",
        mode="harvest",
        acknowledged_by=f"{episode.episode_id}-root",
        delivery_operation_id=f"{episode.episode_id}-retry",
    )
    newer = episode.model_copy(
        update={
            "episode_id": str(uuid.uuid4()),
            "continues_episode_id": episode.episode_id,
            "root_operation_id": None,
            "created_at": episode.updated_at,
        }
    )
    with store.connection() as connection:
        store._insert_episode(connection, newer)
        connection.execute(
            "UPDATE auto_research_child_work SET episode_id=? WHERE worker_id=?",
            (newer.episode_id, f"{episode.episode_id}-worker"),
        )
        connection.execute(
            "UPDATE auto_research_messages SET episode_id=? WHERE message_id=?",
            (newer.episode_id, f"{episode.episode_id}-human"),
        )
    response = build_episode_timeline(store, newer)
    moved_message = next(
        message
        for message in response.messages
        if message.item_id == f"message:{episode.episode_id}-human"
    )
    assert moved_message.disposition == "harvested"
    assert moved_message.delivered_span_id == f"span:{episode.episode_id}-retry"

    assert [member.episode_id for member in response.members] == [
        episode.episode_id,
        newer.episode_id,
    ]
    worker = next(actor for actor in response.actors if actor.kind == "worker")
    assert worker.started_by_span_id == f"span:{episode.episode_id}-root"
    assert response.handoffs[0].to_actor_id == worker.actor_id
    assert response.handoffs[0].from_span_id == worker.started_by_span_id

    monkeypatch.setattr(projection, "EPISODE_TIMELINE_EVENT_LIMIT", 1)
    bounded = build_episode_timeline(store, newer)
    assert bounded.truncated and bounded.spans == []
    assert bounded.handoffs[0].from_span_id == worker.started_by_span_id


def test_experiment_loop_rows_retries_reports_and_shell_watcher(tmp_path):
    from rcp.storage import WatcherRecord

    from .test_episode_api_serialization import _project
    from .test_experiment_episode_storage import _admit_root, _bind, _continuation, _task

    store = AppStore(tmp_path / "data")
    _project(store)
    episode_id, root = _admit_root(store)
    _bind(store, episode_id, root.operation_id, invocation=1)
    now = store.now()
    retry = _task(
        store,
        "retry",
        episode_id,
        invocation=2,
        attempt=2,
        trigger="watcher",
        session_id="native-session",
        stage_root="/tmp/exact-experiment-stage",
    )
    report = _task(store, "report", episode_id).model_copy(
        update={
            "kind": "episode_report",
            "request": {"provider": "codex"},
            "parent_operation_id": retry.operation_id,
        }
    )
    with store.connection() as connection:
        for task in (retry, report):
            store._insert_agent_task(connection, task, continuation_cause="fresh")
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id="shell",
                project_id="project",
                origin_operation_id=root.operation_id,
                origin_task_kind="node_chat",
                chat_id="episode-chat",
                node_id="exp-one",
                episode_id=episode_id,
                continuation=_continuation(episode_id),
                created_at=now,
                check_command="true",
                log_path="/tmp/check.log",
                cwd="/tmp",
                status="completed",
                completed_at=now,
                notification_operation_id=retry.operation_id,
            )
        ]
    )
    response = build_episode_timeline(store, store.episode(episode_id))
    assert {actor.kind for actor in response.actors} == {"human", "agent", "watcher"}
    spans = {span.task_id: span for span in response.spans}
    assert len({span.actor_id for span in response.spans}) == 1
    assert spans["retry"].attempt == 2
    assert spans["retry"].invocation_number is None
    assert spans["report"].kind == "report"
    assert spans["report"].headline is None
    watcher = next(actor for actor in response.actors if actor.kind == "watcher")
    assert watcher.started_by_span_id == f"span:{root.operation_id}"
    assert watcher.started_at == watcher.ended_at == now
    signal = next(signal for signal in response.signals if signal.kind == "watcher")
    assert signal.landing == "woke"
    assert signal.landed_span_id == "span:retry"


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("First finding. More detail.", "First finding."),
        (None, None),
        (
            "A" * (EPISODE_TIMELINE_HEADLINE_MAX_LENGTH + 1),
            "A" * EPISODE_TIMELINE_HEADLINE_MAX_LENGTH,
        ),
    ],
)
def test_headline_uses_stored_answer_only(timeline, answer, expected):
    store, episode, _ = timeline
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET result_json=?, status_message='Not an answer' WHERE operation_id=?",
            (json.dumps({"messages": [answer] if answer else []}), f"{episode.episode_id}-root"),
        )
    span = next(
        span
        for span in build_episode_timeline(store, episode).spans
        if span.task_id == f"{episode.episode_id}-root"
    )
    assert span.headline == expected


@pytest.mark.parametrize("kind", ["handoff", "message"])
def test_text_endpoint_immutable_body_and_chain_ownership(timeline, kind):
    store, episode, client = timeline
    body = _handoff(store, episode) if kind == "handoff" else "Please check the final evidence."
    ref = "handoff:spawn-command" if kind == "handoff" else f"message:{episode.episode_id}-human"
    base = f"/api/projects/{episode.project_id}/episodes"
    response = client.get(f"{base}/{episode.episode_id}/timeline/text/{ref}")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "text_ref": ref,
        "kind": "assignment" if kind == "handoff" else "message",
        "owner_episode_id": episode.episode_id,
        "body": body,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    newer = episode.model_copy(
        update={
            "episode_id": str(uuid.uuid4()),
            "continues_episode_id": episode.episode_id,
            "root_operation_id": None,
            "created_at": store.now(),
        }
    )
    with store.connection() as connection:
        store._insert_episode(connection, newer)
    assert client.get(f"{base}/{newer.episode_id}/timeline/text/{ref}").json() == response.json()
    other = seed_episode_timeline(store, episode.project_id)
    assert client.get(f"{base}/{other.episode_id}/timeline/text/{ref}").status_code == 404
    assert (
        client.get(f"{base}/{episode.episode_id}/timeline/text/message:missing").status_code == 404
    )
    assert (
        client.get(
            f"/api/projects/foreign/episodes/{episode.episode_id}/timeline/text/{ref}"
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "state,event", [("active", "armed"), ("completed", "fired"), ("stopped", "stopped")]
)
def test_graph_watcher_names_node_without_inventing_episode(timeline, state, event):
    from rcp.storage import GraphWatcherRecord
    from rcp.storage.models import NodeStatusGraphCondition

    from .test_experiment_episode_storage import _continuation

    store, episode, _ = timeline
    prefix = episode.episode_id
    now = store.now()
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='running', ending=NULL, wrapup_state='not_started', wrapup_error=NULL, stop_requested_at=NULL, ended_at=NULL WHERE episode_id=?",
            (prefix,),
        )
    store.create_watchers(
        [
            GraphWatcherRecord(
                watcher_id="graph",
                project_id=episode.project_id,
                origin_operation_id=f"{prefix}-root",
                origin_task_kind="auto_research",
                chat_id="episode-chat",
                episode_id=prefix,
                continuation=_continuation(prefix),
                created_at=now,
                graph_target=episode.graph_target,
                armed_revision=1,
                condition=NodeStatusGraphCondition(node_id="exp/timeline", status_in=["completed"]),
                status=state,
                completed_at=now if state == "completed" else None,
                stopped_at=now if state == "stopped" else None,
                notification_operation_id=f"{prefix}-wake" if state == "completed" else None,
            )
        ]
    )
    response = build_episode_timeline(store, episode)
    signal = next(item for item in response.signals if item.item_id == "watcher:graph")
    assert signal.event == event
    assert signal.source_actor_id is None
    assert signal.source_row_key == "node:exp/timeline"
    assert signal.armed_span_id == f"span:{prefix}-root"
    assert signal.armed_at == now
    assert signal.landed_span_id == (f"span:{prefix}-wake" if state == "completed" else None)
