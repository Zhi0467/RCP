"""A Work turn raises a plan card for the Experiment its human asked for; a human's Start runs it."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import EpisodeCommandRequest, ExperimentKickoffArguments
from rcp.background import AgentTaskExecution
from rcp.core.models import Patch
from rcp.loop_status import episode_loop_metadata
from rcp.runs.approval_cards import plan_card_episode_id, settle_interrupted_approval_cards
from rcp.runs.experiment_admission import WorkExperimentStart
from rcp.runs.questions import work_command_handler
from rcp.runs.shared import _sse
from rcp.runs.work_episode_controls import intent_sha256, turn_keyed_id
from rcp.runs.work_experiment_commands import raise_pending_experiment_cards
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.storage.notifications import question_recipients
from rcp.transport.workspace_mailbox import RunStageMailbox

from .helpers import (
    append_fixture_patch,
    authorized_human,
    fabricated_authorizer,
    resource_watermark_tokens,
    seed_patch,
    signed_in_client,
    wait_for_task,
)
from .helpers import create_named_app as create_app

EXPERIMENT_ID = "exp/work-started"
LATER_ID = "exp/written-this-turn"
WORK_TURN = "work-turn"
SCOPE = "a" * 64


def _experiment(node_id: str = EXPERIMENT_ID) -> dict[str, object]:
    return {
        "id": node_id,
        "type": "experiment",
        "title": "Work-started loop",
        "objective": "Exercise a Work kickoff.",
        "completion_criteria": ["The comparison finishes."],
        "invocation_ceiling": 3,
    }


def _experiment_patch(node_id: str = EXPERIMENT_ID) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Added an Experiment a Work chat can start.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[{"op": "create_nodes", "nodes": [_experiment(node_id)]}],
    )


def _kickoff(
    key: str,
    *,
    goal_file: str = "goal.md",
    limit: int | None = None,
    node_id: str = EXPERIMENT_ID,
):
    return EpisodeCommandRequest(
        mailbox_id="a" * 32,
        request_id=uuid.uuid4().hex,
        credential="b" * 64,
        verb="episode",
        idempotency_key=key,
        arguments=ExperimentKickoffArguments(
            action="kick_off_experiment",
            node_id=node_id,
            goal_file=goal_file,
            invocation_limit=limit,
        ),
    )


@pytest.fixture
def plan(manifest, tmp_path: Path):
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    append_fixture_patch(app.state.service, _experiment_patch())
    background = app.state.background_tasks

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(AgentEvent(event="done"))

    background.stream = stream
    app.state.reconcile_question_answers = Mock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "goal.md").write_text("Compare warmup schedules at lr 3e-4.", encoding="utf-8")
    (workspace / "other.md").write_text("A different goal.", encoding="utf-8")
    return SimpleNamespace(
        app=app,
        store=background.store,
        project_id=app.state.default_project_id,
        workspace=workspace,
        human=authorized_human(background.store),
        client=signed_in_client(app),
    )


def _turn(
    plan,
    operation_id,
    *,
    chat_id,
    kind="project_chat",
    parent=None,
    authorized_by=None,
    scope=("repo-a",),
):
    """A running Work turn in `chat_id` and its command handler."""

    request = RunRequest(
        provider="codex",
        run_truth_scope=list(scope),
        chat_scope="node" if kind == "node_chat" else "project",
        node_id=EXPERIMENT_ID if kind == "node_chat" else None,
        chat_id=chat_id,
        message="Run the comparison.",
        mode="work",
    )
    now = plan.store.now()
    plan.store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=plan.project_id,
            kind=kind,
            status="running",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Working.",
            parent_operation_id=parent,
            authorized_by=authorized_by or plan.human,
            dispatch_authority=resolve_dispatch_authority(kind, request),
            native_session_id="session",
            stage_root=str(plan.workspace),
            write_scope_fingerprint=SCOPE,
        )
    )
    execution = AgentTaskExecution(
        operation_id=operation_id,
        store=plan.store,
        control=AgentProcessControl(),
        continuation="retry" if parent else None,
        experiment_card_raiser=plan.app.state.background_tasks.experiment_card_raiser,
    )
    handler = work_command_handler(execution, None, mailbox=RunStageMailbox(plan.workspace, None))
    assert "episode" in handler.allowed_verbs
    identity = CommandTurnIdentity(
        episode_id=None, task_id=operation_id, turn_id="turn", authority="broker"
    )
    return execution, lambda command: handler(command, identity)


def _land(plan, execution, *, succeeded=True):
    """End the turn as the background does, then run its post-turn hook."""

    if succeeded:
        plan.store.complete_agent_task(
            execution.operation_id, applied_revision=None, result={"messages": []}
        )
    else:
        plan.store.fail_agent_task(execution.operation_id, "The provider stopped.")
    plan.app.state.background_tasks.experiment_card_raiser(execution)


def _cards(plan, chat_id):
    listed = plan.client.get(f"/api/projects/{plan.project_id}/chats/{chat_id}/questions")
    assert listed.status_code == 200, listed.text
    return [
        row for row in listed.json() if row["card"] and row["card"]["kind"] == "experiment_start"
    ]


def _plan_card(plan, chat_id="shared-chat", *, key="first", **turn):
    execution, call = _turn(plan, f"turn-{key}", chat_id=chat_id, **turn)
    assert call(_kickoff(key)).result["disposition"] == "card_pending"
    _land(plan, execution)
    (card,) = [
        row
        for row in _cards(plan, chat_id)
        if f"turn-{key}" in (row["operation_id"], row["card"]["revised_operation_id"])
    ]
    return card


def _crash_before_card(plan, key, chat_id="shared-chat"):
    """A turn that asked for a card and succeeded, then lost the process before the hook."""
    execution, call = _turn(plan, f"turn-{key}", chat_id=chat_id)
    assert call(_kickoff(key)).result["disposition"] == "card_pending"
    plan.store.complete_agent_task(
        execution.operation_id, applied_revision=None, result={"messages": []}
    )


def _startup_pass(plan):
    background = plan.app.state.background_tasks
    raise_pending_experiment_cards(plan.store, background.recorded_execution)


@pytest.mark.parametrize("choice", ["cosine", None])
def test_a_changed_governing_link_makes_the_card_stale(plan, monkeypatch, choice):
    from rcp.runs import approval_cards

    card = _plan_card(plan)
    monkeypatch.setattr(
        approval_cards, "governing_links", lambda _state, _node_id: (("dec/schedule", choice),)
    )

    assert _start(plan, card).json()["outcome"] == "stale"
    assert _experiment_loops(plan) == []


def test_startup_raises_a_card_a_crash_lost_once(plan):
    _crash_before_card(plan, "lost")
    assert _cards(plan, "shared-chat") == []

    _startup_pass(plan)
    _startup_pass(plan)

    (card,) = _cards(plan, "shared-chat")
    assert (card["operation_id"], card["card"]["state"]) == ("turn-lost", "pending")


def test_a_lost_older_card_never_replaces_a_later_turns_card(plan):
    _crash_before_card(plan, "older")
    newer = _plan_card(plan, key="newer")

    _startup_pass(plan)

    (card,) = _cards(plan, "shared-chat")
    assert (card["question_id"], card["card"]["state"]) == (newer["question_id"], "pending")


def _soon(plan):
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(plan.store.now()) + timedelta(hours=1)).isoformat()


def _tap(plan, card, decision, start=None, **overrides):
    body = {"decision": decision, "pinned_sha256": card["card"]["pinned_sha256"]}
    if start is not None:
        node = plan.app.state.service.history.state().nodes[EXPERIMENT_ID]
        body["start"] = {
            "kind": "experiment_start",
            "goal": start,
            "invocation_ceiling": 2,
            "node_rev": node.updated_rev,
            **overrides,
        }
    return plan.client.post(f"/api/approval-cards/{card['question_id']}", json=body)


def _reread(plan, card, chat_id="shared-chat"):
    return next(row for row in _cards(plan, chat_id) if row["question_id"] == card["question_id"])


def _private_thread(plan):
    thread = str(uuid.uuid4())
    with plan.store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at) "
            "VALUES (?,?,?,?,?)",
            (thread, plan.human.user_id, "project", plan.project_id, plan.store.now()),
        )
    return thread


def test_a_claimed_card_is_not_revised(plan):
    claimed = _plan_card(plan, key="older")
    assert plan.store.claim_approval_card(claimed["question_id"])

    newer = _plan_card(plan, key="newer")

    assert newer["question_id"] != claimed["question_id"]
    assert newer["card"]["state"] == "pending"
    untouched = _reread(plan, claimed)["card"]
    assert (untouched["state"], untouched["revised_operation_id"]) == ("running", None)
    assert untouched["pinned_sha256"] == claimed["card"]["pinned_sha256"]


def test_a_tap_with_the_hash_from_before_a_revision_leaves_the_card_open(plan):
    seen = _plan_card(plan, key="older")
    (plan.workspace / "goal.md").write_text("A revised handoff.", encoding="utf-8")
    revised = _plan_card(plan, key="newer")

    for decision in ("approve", "decline", "park", "save", "run_here"):
        late = _tap(plan, seen, decision, start="Member text.")
        assert (late.status_code, late.json()["detail"]) == (409, {"code": "plan_revised"})
    assert _reread(plan, seen)["card"]["state"] == "pending"
    assert _experiment_loops(plan) == []
    assert _tap(plan, revised, "decline").json()["outcome"] == "declined"


def test_start_after_a_revision_runs_from_the_revising_turn(plan):
    _plan_card(plan, key="older")
    revised = _plan_card(plan, key="newer", scope=("repo-a", "repo-b"))

    assert _start(plan, revised).json()["outcome"] == "approved"
    episode = plan.store.episode(started_episode(plan, revised))
    root = plan.store.agent_task(episode.root_operation_id)
    assert root.request["started_from_operation_id"] == "turn-newer"
    assert root.request["run_truth_scope"] == ["repo-a", "repo-b"]
    wait_for_task(plan.store, root.operation_id)


def test_recovery_of_an_older_request_leaves_a_newer_revision_alone(plan):
    _plan_card(plan, key="first")
    _crash_before_card(plan, "lost")
    (plan.workspace / "goal.md").write_text("The newest handoff.", encoding="utf-8")
    newest = _plan_card(plan, key="newest")

    _startup_pass(plan)

    (card,) = _cards(plan, "shared-chat")
    assert card["card"]["revised_operation_id"] == "turn-newest"
    assert card["card"]["pinned_sha256"] == newest["card"]["pinned_sha256"]
    assert plan.store.pending_experiment_card_tasks() == []


def test_park_saves_the_members_fields_and_start_runs_the_parked_card(plan):
    card = _plan_card(plan)
    before = resource_watermark_tokens(plan.client, plan.project_id, {"name": "questions"})

    parked = _tap(plan, card, "park", start="The member's handoff.", invocation_ceiling=1)

    assert parked.status_code == 200, parked.text
    assert parked.json()["outcome"] == "parked"
    assert resource_watermark_tokens(plan.client, plan.project_id, {"name": "questions"}) != before
    shown = _reread(plan, card)
    stored = shown["card"]
    assert (stored["state"], bool(stored["shelved_at"])) == ("parked", True)
    assert (
        stored["pinned_sha256"] == parked.json()["pinned_sha256"] != card["card"]["pinned_sha256"]
    )
    assert (stored["action"]["goal"], stored["action"]["invocation_ceiling"]) == (
        "The member's handoff.",
        1,
    )
    assert stored["pinned"] == card["card"]["pinned"]
    assert stored["revised_operation_id"] is None
    assert stored["revised_by"]["name"] == plan.human.display_name
    assert plan.store.get_question(card["question_id"]).state == "pending"
    plan.app.state.reconcile_question_answers.assert_not_called()
    assert _tap(plan, card, "approve", start="x").json()["detail"] == {"code": "plan_revised"}

    saved = _tap(plan, shown, "save", start="Saved while parked.")
    assert saved.json()["outcome"] == "saved"
    shown = _reread(plan, card)
    assert shown["card"]["state"] == "parked"
    assert shown["card"]["pinned_sha256"] == saved.json()["pinned_sha256"]

    assert _start(plan, shown).json()["outcome"] == "approved"
    episode = plan.store.episode(started_episode(plan, shown))
    assert episode is not None
    wait_for_task(plan.store, episode.root_operation_id or "")


def test_a_change_to_only_the_park_columns_moves_the_question_list(plan):
    card = _plan_card(plan)
    before = resource_watermark_tokens(plan.client, plan.project_id, {"name": "questions"})
    with plan.store.connection() as connection:
        connection.execute(
            "UPDATE approval_cards SET shelved_at=?, revised_at=? WHERE question_id=?",
            (plan.store.now(), plan.store.now(), card["question_id"]),
        )
    assert resource_watermark_tokens(plan.client, plan.project_id, {"name": "questions"}) != before


def test_a_parked_card_can_be_deleted(plan):
    card = _plan_card(plan)
    assert _tap(plan, card, "park").json()["outcome"] == "parked"
    assert _tap(plan, card, "decline").json()["outcome"] == "declined"
    assert plan.store.get_question(card["question_id"]).answer == "declined"


def test_park_after_a_pin_change_settles_the_card_stale(plan, monkeypatch):
    from rcp.runs import approval_cards

    card = _plan_card(plan)
    monkeypatch.setattr(
        approval_cards, "governing_links", lambda _state, _node_id: (("dec/schedule", "cosine"),)
    )

    assert _tap(plan, card, "park", start="Member text.").json()["outcome"] == "stale"
    settled = _reread(plan, card)["card"]
    assert (settled["result"], settled["shelved_at"]) == ({"outcome": "stale"}, None)


def test_a_parked_card_outlives_its_expiry_in_a_shared_chat(plan):
    parked = _plan_card(plan, key="parked")
    waiting = _plan_card(plan, "elsewhere", key="waiting")
    assert _tap(plan, parked, "park").json()["outcome"] == "parked"
    with plan.store.connection() as connection:
        connection.execute("UPDATE approval_cards SET expires_at=?", (plan.store.now(),))

    plan.store.expire_approval_cards()

    assert _reread(plan, parked)["card"]["state"] == "parked"
    assert _reread(plan, waiting, "elsewhere")["card"]["state"] == "expired"
    assert _start(plan, _reread(plan, parked)).json()["outcome"] == "approved"
    wait_for_task(
        plan.store, plan.store.episode(started_episode(plan, parked)).root_operation_id or ""
    )


def test_closing_a_thread_settles_its_parked_card(plan):
    thread = _private_thread(plan)
    card = _plan_card(plan, thread)
    assert _tap(plan, card, "park").json()["outcome"] == "parked"
    with plan.store.connection() as connection:
        connection.execute("UPDATE agent_threads SET closed_at=?", (plan.store.now(),))

    plan.store.expire_approval_cards()

    assert plan.store.approval_card(card["question_id"])["state"] == "expired"


def test_a_parked_card_is_quiet_and_sorts_last_for_the_agent(plan):
    import json

    from rcp.api.agent_threads import chat_dock
    from rcp.runs.question_snapshots import question_snapshot

    card = _plan_card(plan)
    later = plan.store.create_or_get_question(
        origin=plan.store.get_question(card["question_id"]).origin,
        key="later",
        question="Which seed?",
        choices=[],
    )
    docked = lambda: chat_dock(  # noqa: E731
        plan.store,
        plan.app.state.catalog,
        plan.human.user_id,
        plan.project_id,
        "shared-chat",
        relay_project=False,
    )["asked_cards"]
    assert [row["question_id"] for row in docked()] == [card["question_id"]]

    assert _tap(plan, card, "park").json()["outcome"] == "parked"

    assert docked() == []
    open_ids = [
        item.question_id
        for item in plan.store.list_questions(project_id=plan.project_id, open_only=True)
    ]
    assert open_ids == [later.question_id]
    with plan.store.connection() as connection:
        events = [
            json.loads(row[0])["active"]
            for row in connection.execute(
                "SELECT payload_json FROM digest_events WHERE kind='question_attention' "
                "AND item_id=? ORDER BY seq",
                (card["question_id"],),
            )
        ]
    assert events == [True, False]
    snapshot = json.loads(
        question_snapshot(
            plan.store,
            project_id=plan.project_id,
            owner_kind="chat",
            owner_ids=["shared-chat"],
            operation_id="turn-first",
        ).text
    )["questions"]
    assert [item["question_id"] for item in snapshot] == [later.question_id, card["question_id"]]
    assert snapshot[1]["card"]["shelved"] is True and "card" not in snapshot[0]


def test_a_plan_for_a_node_with_a_live_run_is_refused(plan):
    card = _plan_card(plan)
    assert _start(plan, card).json()["outcome"] == "approved"
    episode_id = started_episode(plan, card)

    execution, call = _turn(plan, "turn-again", chat_id="shared-chat")
    refused = call(_kickoff("again"))

    assert refused.status == "invalid"
    assert refused.result == {
        "code": "experiment_run_live",
        "episode_id": episode_id,
        "node_id": EXPERIMENT_ID,
    }
    _land(plan, execution)
    assert len(_cards(plan, "shared-chat")) == 1
    wait_for_task(plan.store, plan.store.episode(episode_id).root_operation_id or "")


def test_run_here_sends_the_plan_as_one_member_turn_and_starts_no_episode(plan, monkeypatch):
    from tests.helpers import fake_launch_admitted

    fake_launch_admitted(monkeypatch, plan.app.state.background_tasks)
    execution, call = _turn(plan, "turn-first", chat_id="shared-chat")
    assert call(_kickoff("first")).result["disposition"] == "card_pending"
    _land(plan, execution)
    _turn(plan, "turn-busy", chat_id="shared-chat")
    (card,) = _cards(plan, "shared-chat")

    ran = _tap(plan, card, "run_here", start="Pilot at lr 1e-4 only.")

    assert ran.status_code == 200, ran.text
    assert ran.json()["outcome"] == "run_here"
    shown = _reread(plan, card)["card"]
    turn_id = shown["run_here_operation_id"]
    assert (shown["state"], shown["result"]) == ("running_here", {"outcome": "run_here"})
    assert plan.store.agent_task(turn_id) is None
    (queued,) = plan.store.agent_thread_queue("shared-chat")
    assert queued.user_id == plan.human.user_id
    node = plan.app.state.service.history.state().nodes[EXPERIMENT_ID]
    for field in ("Pilot at lr 1e-4 only.", node.design, *node.completion_criteria):
        assert field in queued.message
    asked = plan.store.get_question(card["question_id"])
    assert (asked.answer, asked.client_receipt_operation_id) == ("run_here", turn_id)
    assert _tap(plan, card, "run_here", start="Again.").json()["detail"] == {"code": "card_taken"}

    plan.store.complete_agent_task("turn-busy", applied_revision=None, result={"messages": []})
    plan.app.state.reconcile_thread_handoffs()

    task = plan.store.agent_task(turn_id)
    assert task.authorized_by == plan.human and task.request["message"] == queued.message
    assert task.episode_id is None
    assert plan.store.agent_thread_queue("shared-chat") == []
    assert _reread(plan, card)["card"]["state"] == "running_here"
    plan.store.complete_agent_task(turn_id, applied_revision=None, result={"messages": []})
    assert _reread(plan, card)["card"]["state"] == "ran_here"
    assert _experiment_loops(plan) == []


def test_an_interrupted_run_here_settles_from_its_queued_message(plan):
    card = _plan_card(plan)
    _turn(plan, "turn-busy", chat_id="shared-chat")
    request_id = plan.store.approval_card(card["question_id"])["request_id"]
    assert plan.store.claim_approval_card(card["question_id"])
    plan.store.queue_agent_thread_message(
        queue_id="queued",
        thread_id="shared-chat",
        user_id=plan.human.user_id,
        idempotency_key=request_id,
        message="The plan.",
        attachment_set_id=None,
        attachment_client_id=None,
        attachment_count=0,
        request_sha256="hash",
    )

    settle_interrupted_approval_cards(
        plan.store, service_for=plan.app.state.catalog.open, claimed_before=plan.store.now()
    )

    settled = plan.store.approval_card(card["question_id"])
    assert (settled["state"], settled["result"]["outcome"]) == ("running_here", "run_here")


def test_run_here_is_an_experiment_cards_decision_only(plan):
    card = _auto_plan_card(plan)
    refused = plan.client.post(
        f"/api/approval-cards/{card['question_id']}",
        json={"decision": "run_here", "pinned_sha256": card["card"]["pinned_sha256"]},
    )
    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        {"code": "decision_not_available"},
    )
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"


def test_an_auto_research_card_parks_and_is_revised_in_place(plan):
    card = _auto_plan_card(plan)
    parked = plan.client.post(
        f"/api/approval-cards/{card['question_id']}",
        json={"decision": "park", "pinned_sha256": card["card"]["pinned_sha256"]},
    )
    assert parked.json() == {"outcome": "parked", "pinned_sha256": card["card"]["pinned_sha256"]}

    revised = _auto_plan_card(plan, key="again")

    assert revised["question_id"] == card["question_id"]
    assert (revised["card"]["state"], revised["card"]["revised_operation_id"]) == (
        "parked",
        "turn-again",
    )


def test_a_card_row_from_before_parking_reads_open_and_settles(plan):
    from rcp.storage import AppStore

    card = _plan_card(plan)
    with plan.store.connection() as connection:
        for suffix in ("agent_threads", "questions"):
            for event in ("insert", "update", "delete"):
                connection.execute(f"DROP TRIGGER resource_approval_cards_{suffix}_{event}")
        for column in ("shelved_at", "revised_operation_id", "revised_at", "revised_by_json"):
            connection.execute(f"ALTER TABLE approval_cards DROP COLUMN {column}")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=95")

    reopened = AppStore(plan.store.path)

    old = reopened.approval_card(card["question_id"])
    assert (old["state"], old["shelved_at"], old["revised_at"]) == ("pending", None, None)
    assert (old["revised_by"], old["revised_operation_id"]) == (None, None)
    assert old["pinned_sha256"] == card["card"]["pinned_sha256"]
    assert _tap(plan, card, "decline").json()["outcome"] == "declined"


def _start(plan, card, **overrides):
    node = plan.app.state.service.history.state().nodes[card["card"]["action"]["node_id"]]
    body = {
        "goal": "Compare warmup schedules at lr 1e-4.",
        "invocation_ceiling": 2,
        "node_rev": node.updated_rev,
        **overrides,
    }
    return plan.client.post(
        f"/api/approval-cards/{card['question_id']}",
        json={
            "decision": "approve",
            "pinned_sha256": card["card"]["pinned_sha256"],
            "start": {"kind": "experiment_start", **body},
        },
    )


def _experiment_loops(plan):
    return [
        item.episode_id
        for item in plan.store.episodes(plan.project_id, limit=None)
        if item.mode == "experiment_loop"
    ]


def test_kickoff_records_the_request_and_starts_nothing(plan, monkeypatch):
    from rcp.runs import questions

    first, call = _turn(plan, WORK_TURN, chat_id="shared-chat")
    monkeypatch.setattr(questions, "work_ask_authorized", lambda _execution: False)
    refused = call(_kickoff("unasked"))
    assert (refused.status, refused.result) == ("invalid", {"code": "experiment_card_unavailable"})
    monkeypatch.undo()

    pending = call(_kickoff("first", limit=2))
    assert pending.status == "ok", pending.message
    assert pending.result["disposition"] == "card_pending"
    assert call(_kickoff("first", limit=2)).result["disposition"] == "card_pending"
    assert call(_kickoff("first", goal_file="other.md", limit=2)).status == "invalid"
    assert _experiment_loops(plan) == []

    _land(plan, first, succeeded=False)
    assert _cards(plan, "shared-chat") == []

    retry, call_retry = _turn(plan, "work-retry", chat_id="shared-chat", parent=WORK_TURN)
    assert call_retry(_kickoff("first", limit=2)).result["disposition"] == "card_pending"
    _land(plan, retry)
    _land(plan, retry)
    (card,) = _cards(plan, "shared-chat")
    action = card["card"]["action"]
    assert (action["node_id"], action["invocation_ceiling"]) == (EXPERIMENT_ID, 2)
    assert action["goal"] == "Compare warmup schedules at lr 3e-4."
    assert action["run_truth_scope"] == ["repo-a"]
    assert card["operation_id"] == "work-retry"
    assert card["card"]["state"] == "pending"
    assert _experiment_loops(plan) == []


def test_a_card_waits_for_the_node_its_turn_writes(plan):
    execution, call = _turn(plan, WORK_TURN, chat_id="shared-chat")
    assert call(_kickoff("later", node_id=LATER_ID)).result["disposition"] == "card_pending"
    assert call(_kickoff("question", node_id="rq/learning-after-shift")).status == "ok"
    append_fixture_patch(plan.app.state.service, _experiment_patch(LATER_ID))
    _land(plan, execution)

    (card,) = _cards(plan, "shared-chat")
    assert card["card"]["action"]["node_id"] == LATER_ID
    assert card["card"]["action"]["invocation_ceiling"] == 3
    assert "updated_rev" not in str(card["card"]["pinned"])
    assert any(event.level == "warning" for event in plan.store.agent_task_events(WORK_TURN))


def test_a_later_request_revises_the_open_card_in_place(plan):
    older = _plan_card(plan, key="older")
    with plan.store.connection() as connection:
        connection.execute("UPDATE approval_cards SET expires_at=?", (_soon(plan),))
    (plan.workspace / "goal.md").write_text("Compare three schedules.", encoding="utf-8")
    newer = _plan_card(plan, key="newer")
    other_chat = _plan_card(plan, "elsewhere", key="other")

    assert [row["question_id"] for row in _cards(plan, "shared-chat")] == [older["question_id"]]
    assert newer["operation_id"] == "turn-older"
    revised, before = newer["card"], older["card"]
    assert revised["state"] == "pending"
    assert revised["action"]["goal"] == "Compare three schedules."
    assert revised["revised_operation_id"] == "turn-newer"
    assert revised["revised_by"]["kind"] == "agent" and revised["revised_at"]
    assert revised["pinned_sha256"] != before["pinned_sha256"]
    assert revised["request_id"] != before["request_id"]
    assert revised["expires_at"] > _soon(plan)
    assert plan.store.pending_experiment_card_tasks() == []
    assert _cards(plan, "elsewhere")[0]["card"]["state"] == "pending"

    with plan.store.connection() as connection:
        connection.execute(
            "UPDATE approval_cards SET expires_at=? WHERE question_id=?",
            (plan.store.now(), other_chat["question_id"]),
        )
    assert _cards(plan, "elsewhere")[0]["card"]["state"] == "expired"


def test_a_pre_card_receipt_with_an_episode_still_replays_existing(plan):
    execution, call = _turn(plan, WORK_TURN, chat_id="shared-chat")
    arguments = _kickoff("old", limit=2).arguments
    goal = (plan.workspace / "goal.md").read_text(encoding="utf-8")
    episode_id = turn_keyed_id((WORK_TURN,), "episode", "old")
    plan.store.record_agent_task_receipt(
        WORK_TURN,
        "work_experiment_kickoff",
        {
            "episode_id": episode_id,
            "intent_sha256": intent_sha256({**arguments.model_dump(mode="json"), "goal": goal}),
        },
        tier="diagnostic",
    )
    _start_directly(plan, WORK_TURN, episode_id, goal)

    replayed = call(_kickoff("old", limit=2))
    assert replayed.status == "ok", replayed.message
    assert (replayed.result["disposition"], replayed.result["episode_id"]) == (
        "existing",
        episode_id,
    )
    assert call(_kickoff("old", goal_file="other.md", limit=2)).status == "invalid"
    _land(plan, execution)
    assert _cards(plan, "shared-chat") == []


def _node_rev(plan):
    return plan.app.state.service.history.state().nodes[EXPERIMENT_ID].updated_rev


def _live_governing(plan):
    from rcp.control import governing_links

    return governing_links(plan.app.state.service.history.state(), EXPERIMENT_ID)


def _start_directly(plan, source, episode_id, goal, node_rev=None, governing=None):
    from rcp.api.experiments import start_work_experiment

    return start_work_experiment(
        plan.app.state.catalog,
        plan.app.state.background_tasks,
        plan.app.state.services.experiment_operation_lock,
        WorkExperimentStart(
            source_operation_id=source,
            episode_id=episode_id,
            node_id=EXPERIMENT_ID,
            node_rev=_node_rev(plan) if node_rev is None else node_rev,
            governing=_live_governing(plan) if governing is None else governing,
            goal=goal,
            invocation_limit=2,
            provider=None,
            model=None,
            reasoning=None,
            authorized_by=plan.human,
        ),
    )


def test_start_in_a_private_chat_runs_the_members_edited_plan(plan):
    thread = _private_thread(plan)
    card = _plan_card(plan, thread)

    moved = _start(plan, card, node_rev=card_node_rev(plan) + 1)
    assert (moved.status_code, moved.json()["detail"]) == (409, {"code": "plan_changed"})
    assert _start(plan, card, invocation_ceiling=4).status_code == 422
    with plan.store.connection() as connection:
        connection.execute("UPDATE agent_threads SET user_id='someone-else'")
    assert _start(plan, card).status_code == 404
    with plan.store.connection() as connection:
        connection.execute("UPDATE agent_threads SET user_id=?", (plan.human.user_id,))
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"

    started = _start(plan, card)
    assert started.status_code == 200, started.text
    assert started.json()["outcome"] == "approved"
    request_id = plan.store.approval_card(card["question_id"])["request_id"]
    episode = plan.store.episode(plan_card_episode_id(request_id, "experiment_start"))
    assert episode is not None and episode.invocation_ceiling == 2
    assert episode.authorized_by == plan.human
    root = plan.store.agent_task(episode.root_operation_id or "")
    assert root.request["message"] == "Compare warmup schedules at lr 1e-4."
    assert root.request["started_from_operation_id"] == card["operation_id"]
    starter = episode_loop_metadata(plan.store, episode).started_by
    assert (starter.kind, starter.chat_id) == ("chat", thread)
    wait_for_task(plan.store, root.operation_id)

    assert _start(plan, card).json()["detail"] == {"code": "card_taken"}
    assert _experiment_loops(plan) == [episode.episode_id]


def card_node_rev(plan):
    return plan.app.state.service.history.state().nodes[EXPERIMENT_ID].updated_rev


def test_start_in_a_shared_chat_takes_a_write_member(plan, monkeypatch):
    card = _plan_card(plan, authorized_by=fabricated_authorizer("Chat starter"))
    question = plan.store.get_question(card["question_id"])
    with plan.store.connection() as connection:
        assert question_recipients(connection, question) == set()

    before = resource_watermark_tokens(plan.client, plan.project_id, {"name": "questions"})
    assert plan.store.claim_approval_card(card["question_id"])
    assert resource_watermark_tokens(plan.client, plan.project_id, {"name": "questions"}) != before
    plan.store.release_approval_card(card["question_id"])

    monkeypatch.setattr(plan.store, "is_project_member", lambda *_args: False)
    assert _start(plan, card).status_code == 404
    monkeypatch.undo()
    with plan.store.connection() as connection:
        connection.execute("UPDATE space_users SET display_name=NULL")
    assert _start(plan, card).status_code == 428
    plan.store.rename_space_user(plan.human.user_id, plan.human.display_name)
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"

    started = _start(plan, card)
    assert started.json()["outcome"] == "approved", started.text
    episode = plan.store.episode(started_episode(plan, card))
    assert episode is not None and episode.authorized_by == plan.human
    wait_for_task(plan.store, episode.root_operation_id or "")


def started_episode(plan, card):
    return plan_card_episode_id(
        plan.store.approval_card(card["question_id"])["request_id"], "experiment_start"
    )


def test_a_refused_start_settles_failed_with_its_reason(plan):
    running = _plan_card(plan, key="running")
    refused = _plan_card(plan, "node-chat", key="refused", kind="node_chat")
    assert _start(plan, running).json()["outcome"] == "approved"

    result = _start(plan, refused).json()
    assert result["outcome"] == "failed" and result["reason"]
    assert plan.store.get_question(refused["question_id"]).state == "answered"
    assert len(_experiment_loops(plan)) == 1
    wait_for_task(plan.store, plan.store.episode(started_episode(plan, running)).root_operation_id)


def test_a_plan_edited_before_the_start_lock_is_refused(plan):
    from rcp.runs.experiment_admission import PlanChanged

    card = _plan_card(plan)
    with pytest.raises(PlanChanged):
        _start_directly(
            plan, card["operation_id"], started_episode(plan, card), "Goal.", _node_rev(plan) - 1
        )
    with pytest.raises(PlanChanged):
        _start_directly(
            plan,
            card["operation_id"],
            started_episode(plan, card),
            "Goal.",
            governing=(*_live_governing(plan), ("decision-changed-since", "other")),
        )
    with pytest.raises(PlanChanged):
        _start_directly(
            plan,
            card["operation_id"],
            started_episode(plan, card),
            "Goal.",
            governing=(*_live_governing(plan), ("undecided-link-removed-since", None)),
        )
    assert _experiment_loops(plan) == []


def test_an_interrupted_start_settles_from_its_episode(plan):
    card = _plan_card(plan)
    assert plan.store.claim_approval_card(card["question_id"])
    episode_id = started_episode(plan, card)
    root = _start_directly(plan, card["operation_id"], episode_id, "Interrupted goal.")

    settle_interrupted_approval_cards(
        plan.store,
        service_for=plan.app.state.catalog.open,
        claimed_before=plan.store.now(),
    )
    settled = plan.store.approval_card(card["question_id"])
    assert settled["result"]["outcome"] == "approved"
    assert settled["result"]["result"] == {"episode_id": episode_id}
    again = _start_directly(plan, card["operation_id"], episode_id, "Interrupted goal.")
    assert again.operation_id == root.operation_id
    assert _experiment_loops(plan) == [episode_id]
    wait_for_task(plan.store, root.operation_id)


def test_dont_run_this_now_declines_and_reaches_the_agent(plan):
    card = _plan_card(plan)
    declined = plan.client.post(
        f"/api/approval-cards/{card['question_id']}",
        json={"decision": "decline", "pinned_sha256": card["card"]["pinned_sha256"]},
    )
    assert declined.json()["outcome"] == "declined"
    assert plan.store.get_question(card["question_id"]).answer == "declined"
    plan.app.state.reconcile_question_answers.assert_called_with(plan.project_id)
    assert _experiment_loops(plan) == []


def _auto_start_directly(plan, source, episode_id, *, authorizer=None):
    from rcp.api.work_episodes import start_work_auto_research
    from rcp.runs.work_episode_controls import WorkAutoResearchStart

    return start_work_auto_research(
        plan.app.state.catalog,
        plan.app.state.background_tasks,
        plan.app.state.services.experiment_operation_lock,
        WorkAutoResearchStart(
            source_operation_id=source,
            episode_id=episode_id,
            goal="Explore warmup.",
            invocation_ceiling=2,
            code_worktree=False,
            worktree_repository=None,
            branch_name="warmup-exploration",
            provider=None,
            model=None,
            reasoning=None,
        ),
        authorized_by=authorizer or plan.human,
    )


def test_auto_research_start_uses_tapping_member_and_replays_once(plan, monkeypatch):
    _turn(plan, WORK_TURN, chat_id="shared-chat", authorized_by=fabricated_authorizer("Raiser"))
    monkeypatch.setattr(
        "rcp.api.work_episodes.require_auto_research_provider_ready", lambda *_args, **_kw: None
    )
    episode_id = str(uuid.uuid4())
    episode = _auto_start_directly(plan, WORK_TURN, episode_id)
    assert episode.authorized_by == plan.human
    assert episode.invocation_ceiling == 2
    root = plan.store.agent_task(episode.root_operation_id)
    assert root.request["started_from_operation_id"] == WORK_TURN
    assert (
        plan.store.auto_research_state(episode.episode_id).starting_instruction == "Explore warmup."
    )
    assert _auto_start_directly(plan, WORK_TURN, episode_id).episode_id == episode_id
    assert len(plan.store.episodes(plan.project_id, limit=None)) == 1
    wait_for_task(plan.store, root.operation_id)


def _auto_plan_card(plan, *, chat_id="shared-chat", key="auto", land=True):
    execution, call = _turn(
        plan, f"turn-{key}", chat_id=chat_id, authorized_by=fabricated_authorizer("Raiser")
    )
    command = EpisodeCommandRequest.model_validate(
        {
            "mailbox_id": "a" * 32,
            "request_id": uuid.uuid4().hex,
            "credential": "b" * 64,
            "verb": "episode",
            "idempotency_key": key,
            "arguments": {
                "action": "kick_off_auto_research",
                "goal_file": "goal.md",
                "invocation_limit": 3,
            },
        }
    )
    receipt = call(command)
    assert receipt.status == "ok", receipt.message
    assert receipt.result["disposition"] == "card_pending"
    assert plan.store.episodes(plan.project_id, limit=None) == []
    if land:
        _land(plan, execution)
    else:
        plan.store.complete_agent_task(
            execution.operation_id, applied_revision=None, result={"messages": []}
        )
        _startup_pass(plan)
        _startup_pass(plan)
    listed = plan.client.get(f"/api/projects/{plan.project_id}/chats/{chat_id}/questions")
    assert listed.status_code == 200, listed.text
    (card,) = [row for row in listed.json() if row["card"]["kind"] == "auto_research"]
    return card


def _start_auto_card(plan, card, **overrides):
    return plan.client.post(
        f"/api/approval-cards/{card['question_id']}",
        json={
            "decision": "approve",
            "pinned_sha256": card["card"]["pinned_sha256"],
            "start": {
                "kind": "auto_research",
                "starting_instruction": "Explore the edited warmup plan.",
                "invocation_ceiling": 2,
                "branch_name": "edited-warmup",
                "code_worktree": False,
                **overrides,
            },
        },
    )


def test_auto_research_card_starts_edited_plan_and_repeated_tap_starts_nothing(plan, monkeypatch):
    card = _auto_plan_card(plan, land=False)
    monkeypatch.setattr(
        "rcp.api.work_episodes.require_auto_research_provider_ready", lambda *_args, **_kw: None
    )
    started = _start_auto_card(plan, card)
    assert started.status_code == 200, started.text
    assert started.json()["outcome"] == "approved"
    (episode,) = plan.store.episodes(plan.project_id, limit=None)
    assert episode.authorized_by == plan.human
    assert episode.invocation_ceiling == 2
    assert uuid.UUID(episode.episode_id).version == 4
    root = plan.store.agent_task(episode.root_operation_id)
    assert (
        plan.store.auto_research_state(episode.episode_id).starting_instruction
        == "Explore the edited warmup plan."
    )
    assert root.request["started_from_operation_id"] == card["operation_id"]
    assert root.request["code_worktree"] is False
    assert _start_auto_card(plan, card).json()["detail"] == {"code": "card_taken"}
    assert len(plan.store.episodes(plan.project_id, limit=None)) == 1
    wait_for_task(plan.store, root.operation_id)


@pytest.mark.parametrize(
    "code",
    ["provider_not_ready", "machine_not_found", "auto_research_profile_unavailable"],
)
def test_auto_research_fixable_readiness_refusal_keeps_card_pending(plan, monkeypatch, code):
    card = _auto_plan_card(plan)

    def refuse(*_args, **_kwargs):
        raise ValueError(code)

    monkeypatch.setattr("rcp.api.work_episodes.require_auto_research_provider_ready", refuse)
    response = _start_auto_card(plan, card)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"
    assert plan.store.episodes(plan.project_id, limit=None) == []


def test_plan_card_refuses_mismatched_start_kind_without_consuming_it(plan):
    card = _plan_card(plan)
    response = _start_auto_card(plan, card)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "start_kind_mismatch"
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"


def test_auto_research_branch_name_refusal_can_be_fixed_on_the_same_card(plan, monkeypatch):
    card = _auto_plan_card(plan)
    monkeypatch.setattr(
        "rcp.api.work_episodes.require_auto_research_provider_ready", lambda *_args, **_kw: None
    )
    invalid = _start_auto_card(plan, card, branch_name="main")
    assert invalid.status_code == 409, invalid.text
    assert invalid.json()["detail"]["code"] == "branch_name_invalid"
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"
    fixed = _start_auto_card(plan, card)
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["outcome"] == "approved"
    (episode,) = plan.store.episodes(plan.project_id, limit=None)
    assert episode.root_operation_id is not None
    wait_for_task(plan.store, episode.root_operation_id)


def test_interrupted_auto_research_start_settles_from_its_episode(plan, monkeypatch):
    card = _auto_plan_card(plan)
    monkeypatch.setattr(
        "rcp.api.work_episodes.require_auto_research_provider_ready", lambda *_args, **_kw: None
    )
    stored = plan.store.approval_card(card["question_id"])
    episode_id = plan_card_episode_id(stored["request_id"], "auto_research")
    assert plan.store.claim_approval_card(card["question_id"])
    episode = _auto_start_directly(plan, card["operation_id"], episode_id)
    settle_interrupted_approval_cards(
        plan.store, service_for=plan.app.state.catalog.open, claimed_before=plan.store.now()
    )
    settled = plan.store.approval_card(card["question_id"])
    assert settled["result"]["outcome"] == "approved"
    assert settled["result"]["result"] == {"episode_id": episode_id}
    assert len(plan.store.episodes(plan.project_id, limit=None)) == 1
    assert episode.root_operation_id is not None
    wait_for_task(plan.store, episode.root_operation_id)


def test_auto_research_replay_skips_provider_probe(plan, monkeypatch):
    _turn(plan, WORK_TURN, chat_id="shared-chat")
    probe = Mock()
    monkeypatch.setattr("rcp.api.work_episodes.require_auto_research_provider_ready", probe)
    episode = _auto_start_directly(plan, WORK_TURN, str(uuid.uuid4()))
    probe.side_effect = ValueError("provider_not_ready")
    assert (
        _auto_start_directly(plan, WORK_TURN, episode.episode_id).episode_id == episode.episode_id
    )
    assert probe.call_count == 1
    assert episode.root_operation_id is not None
    wait_for_task(plan.store, episode.root_operation_id)


def test_auto_research_oversized_edit_keeps_card_pending(plan):
    card = _auto_plan_card(plan)
    response = _start_auto_card(plan, card, starting_instruction="x" * 16_001)
    assert response.status_code == 422
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"
    assert plan.store.episodes(plan.project_id, limit=None) == []


def test_startup_card_failure_does_not_abort_other_tasks_and_retries_next_startup(plan):
    _crash_before_card(plan, "broken")
    _crash_before_card(plan, "healthy", chat_id="other-chat")
    background = plan.app.state.background_tasks
    failing = {"turn-broken"}

    def execution_for(task):
        if task.operation_id in failing:
            raise ValueError("transient failure")
        return background.recorded_execution(task)

    raise_pending_experiment_cards(plan.store, execution_for)
    assert [task.operation_id for task in plan.store.pending_experiment_card_tasks()] == [
        "turn-broken"
    ]
    assert any(event.level == "warning" for event in plan.store.agent_task_events("turn-broken"))
    assert len(_cards(plan, "other-chat")) == 1
    assert _cards(plan, "shared-chat") == []
    failing.clear()
    raise_pending_experiment_cards(plan.store, execution_for)
    assert plan.store.pending_experiment_card_tasks() == []
    assert len(_cards(plan, "shared-chat")) == 1
    assert len(_cards(plan, "other-chat")) == 1


def test_auto_research_invalid_worktree_edit_can_be_corrected(plan, monkeypatch):
    card = _auto_plan_card(plan)
    response = _start_auto_card(plan, card, code_worktree=False, worktree_repository="repo-a")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "auto_research_start_invalid"
    assert response.json()["detail"]["message"]
    assert plan.store.approval_card(card["question_id"])["state"] == "pending"
    monkeypatch.setattr(
        "rcp.api.work_episodes.require_auto_research_provider_ready", lambda *_args, **_kw: None
    )
    assert _start_auto_card(plan, card).json()["outcome"] == "approved"
    (episode,) = plan.store.episodes(plan.project_id, limit=None)
    assert episode.root_operation_id is not None
    wait_for_task(plan.store, episode.root_operation_id)


@pytest.mark.parametrize("kind", ["experiment", "auto_research"])
def test_old_pending_receipts_recover_after_database_reopen(plan, kind):
    import json

    from rcp.storage import AppStore

    execution, call = _turn(plan, "old-pending", chat_id="shared-chat")
    if kind == "experiment":
        command = _kickoff("old")
        category = "work_experiment_kickoff"
    else:
        command = EpisodeCommandRequest.model_validate(
            {
                **_kickoff("old").model_dump(mode="json"),
                "arguments": {
                    "action": "kick_off_auto_research",
                    "goal_file": "goal.md",
                    "invocation_limit": 3,
                },
            }
        )
        category = "work_auto_research_kickoff"
    assert call(command).result["disposition"] == "card_pending"
    (receipt,) = plan.store.agent_task_receipts_by_category(execution.operation_id, category)
    payload = dict(receipt.payload)
    if kind == "experiment":
        payload["invocation_limit"] = payload.pop("invocation_ceiling")
    else:
        payload["starting_instruction"] = payload.pop("goal")
    with plan.store.connection() as connection:
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=90")
        connection.execute("DROP INDEX graph_run_receipts_plan_cards")
        connection.execute(
            "UPDATE graph_run_receipts SET payload_json=? WHERE operation_id=? AND category=?",
            (json.dumps(payload), execution.operation_id, category),
        )
    plan.store.complete_agent_task(
        execution.operation_id, applied_revision=None, result={"messages": []}
    )
    reopened = AppStore(plan.store.path)
    assert [task.operation_id for task in reopened.pending_experiment_card_tasks()] == [
        execution.operation_id
    ]
    _startup_pass(plan)
    (question,) = plan.store.list_questions(project_id=plan.project_id)
    card = plan.store.approval_card(question.question_id)
    assert card["state"] == "pending"
    assert card["action"]["invocation_ceiling"] == 3
    assert reopened.pending_experiment_card_tasks() == []
    assert any(
        event.level == "info" for event in reopened.agent_task_events(execution.operation_id)
    )
