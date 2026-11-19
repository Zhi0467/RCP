"""A member's Work chat runs their Auto-research controls on the episodes it started."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import Mock

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import EpisodeCommandRequest
from rcp.background import AgentTaskExecution
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.questions import work_command_handler
from rcp.runs.shared import _sse
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.transport.workspace_mailbox import RunStageMailbox

from .helpers import async_wait_until, authorized_human, signed_in_client, wait_for_task, wait_until
from .helpers import create_named_app as create_app

THREAD_CHAT = str(uuid.uuid4())
OTHER_CHAT = str(uuid.uuid4())


def _episode(key: str, arguments: dict[str, object]) -> EpisodeCommandRequest:
    return EpisodeCommandRequest.model_validate(
        {
            "mailbox_id": "a" * 32,
            "request_id": uuid.uuid4().hex,
            "credential": "b" * 64,
            "verb": "episode",
            "idempotency_key": key,
            "arguments": arguments,
        }
    )


class _Member:
    """The member's app, with Work turns in their thread and in another chat."""

    def __init__(self, manifest, tmp_path: Path, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        self.app = create_app(str(manifest.path), data_dir=tmp_path / "data")
        self.app.state.reconcile_question_answers = Mock()
        self.client = signed_in_client(self.app)
        self.executions = {}
        self.background = self.app.state.background_tasks
        self.store = self.background.store
        self.project_id = self.app.state.default_project_id
        self.human = authorized_human(self.store)
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        (self.workspace / "goal.md").write_text("Find why warmup diverges.", encoding="utf-8")
        (self.workspace / "note.md").write_text("Try the smaller batch first.", encoding="utf-8")
        self.stage = tmp_path / "orchestrator-stage"
        self.stage.mkdir()

    def provider_ready(self) -> None:
        self.monkeypatch.setattr(
            "rcp.api.work_episodes.require_auto_research_provider_ready",
            lambda *_args, **_kwargs: None,
        )

    def turn(self, operation_id: str, chat_id: str = THREAD_CHAT):
        request = RunRequest(
            provider="codex",
            run_truth_scope=["repo-a"],
            chat_scope="project",
            chat_id=chat_id,
            message="Run Auto-research on warmup.",
            mode="work",
        )
        now = self.store.now()
        self.store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=self.project_id,
                kind="project_chat",
                status="running",
                request=request.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                status_message="Working.",
                native_session_id="session",
                stage_root=str(self.workspace),
                write_scope_fingerprint="a" * 64,
                authorized_by=self.human,
                dispatch_authority=resolve_dispatch_authority("project_chat", request),
            )
        )
        execution = AgentTaskExecution(
            operation_id=operation_id,
            store=self.store,
            control=AgentProcessControl(),
            experiment_card_raiser=self.background.experiment_card_raiser,
            episode_controls=self.background.episode_controls,
        )
        self.executions[operation_id] = execution
        handler = work_command_handler(
            execution, None, mailbox=RunStageMailbox(self.workspace, None)
        )
        identity = CommandTurnIdentity(
            episode_id=None, task_id=operation_id, turn_id="turn", authority="broker"
        )
        return lambda key, arguments: handler(_episode(key, arguments), identity)

    def land(self, operation_id: str):
        self.store.complete_agent_task(operation_id, applied_revision=None, result={"messages": []})
        self.background.experiment_card_raiser(self.executions[operation_id])
        return next(
            question
            for question in self.store.list_questions(project_id=self.project_id)
            if question.origin.operation_id == operation_id
        )

    def start(self, question, **overrides):
        card = self.store.approval_card(question.question_id)
        assert card is not None
        return self.client.post(
            f"/api/approval-cards/{question.question_id}",
            json={
                "decision": "approve",
                "pinned_sha256": card["pinned_sha256"],
                "start": {
                    "kind": "auto_research",
                    "starting_instruction": "Find why warmup diverges.",
                    "invocation_ceiling": 3,
                    **overrides,
                },
            },
        )

    def pausable_orchestrator(self) -> None:
        stage = str(self.stage)

        async def stream(_project_id, _kind, request, execution):
            execution.checkpoint_stage(BACKEND_MACHINE_ID, stage)
            yield _sse(AgentEvent(event="session", session_id="orchestrator-session"))
            await async_wait_until(execution.control.pause_requested.is_set)
            yield _sse(AgentEvent(event="paused", text="Provider process paused."))

        self.background.stream = stream

    def settling_orchestrator(self) -> None:
        stage = str(self.stage)

        async def stream(_project_id, _kind, _request, execution):
            execution.checkpoint_stage(BACKEND_MACHINE_ID, stage)
            yield _sse(AgentEvent(event="session", session_id="orchestrator-session"))
            yield _sse(AgentEvent(event="done"))

        self.background.stream = stream


KICKOFF = {"action": "kick_off_auto_research", "goal_file": "goal.md", "invocation_limit": 3}


def test_a_work_chat_starts_pauses_resumes_steers_stops_and_extends_its_auto_research(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.provider_ready()
    member.pausable_orchestrator()
    act = member.turn("kickoff-turn")

    started = act("go", KICKOFF)
    assert started.status == "ok", started.message
    assert started.result["disposition"] == "card_pending"
    card = member.land("kickoff-turn")
    tapped = member.start(card)
    assert tapped.status_code == 200, tapped.text
    episode_id = tapped.json()["result"]["episode_id"]
    episode = store.episode(episode_id)
    assert episode is not None and episode.mode == "auto_research"
    assert episode.authorized_by == member.human
    assert episode.invocation_ceiling == 3
    assert store.episode_started_from_chat(episode_id).chat_id == THREAD_CHAT  # type: ignore[union-attr]
    replayed = act("go", KICKOFF)
    assert (replayed.status, replayed.result["disposition"]) == ("ok", "card_pending")
    repeated_tap = member.start(card)
    assert repeated_tap.status_code == 409
    assert repeated_tap.json()["detail"]["code"] == "card_taken"
    assert [item.episode_id for item in store.episodes(member.project_id, limit=None)] == [
        episode_id
    ]

    root_id = episode.root_operation_id
    assert root_id is not None
    root = store.agent_task(root_id)
    assert root is not None and root.request["started_from_operation_id"] == "kickoff-turn"
    old_writer = {
        key: value for key, value in root.request.items() if key != "started_from_operation_id"
    }
    assert AutoResearchRunRequest.model_validate(old_writer).model_dump(mode="json") == old_writer
    wait_until(lambda: store.agent_task(root_id).status == "running")  # type: ignore[union-attr]
    paused = act("pause", {"action": "pause", "episode_id": episode_id})
    assert paused.status == "ok", paused.message
    wait_for_task(store, root_id, expect="paused")

    member.settling_orchestrator()
    resumed = act("resume", {"action": "resume", "episode_id": episode_id})
    assert resumed.status == "ok", resumed.message
    resumed_id = str(resumed.result["operation_id"])
    assert resumed_id != root_id
    wait_for_task(store, resumed_id, expect="succeeded")

    sent = act("steer", {"action": "message", "episode_id": episode_id, "body_file": "note.md"})
    assert sent.status == "ok", sent.message
    message = store.auto_research_message(str(sent.result["message_id"]))
    assert message is not None and message.authorized_by == member.human

    stopped = act("stop", {"action": "stop", "episode_id": episode_id})
    assert stopped.status == "ok", stopped.message
    wait_until(lambda: store.episode(episode_id).status == "stopped")  # type: ignore[union-attr]
    assert store.episode(episode_id).stop_initiated_by == "agent:kickoff-turn"  # type: ignore[union-attr]

    extended = act("more", {"action": "add_turns", "episode_id": episode_id, "invocation_limit": 2})
    assert extended.status == "ok", extended.message
    continuation = store.episode(str(extended.result["episode_id"]))
    assert continuation is not None and continuation.continues_episode_id == episode_id
    assert continuation.graph_target == episode.graph_target
    assert continuation.code_worktree == episode.code_worktree
    assert continuation.invocation_ceiling == 2
    assert continuation.authorized_by == member.human
    again = act("more", {"action": "add_turns", "episode_id": episode_id, "invocation_limit": 2})
    assert (again.status, again.result["episode_id"]) == ("ok", continuation.episode_id)
    restopped = act("stop-again", {"action": "stop", "episode_id": episode_id})
    assert restopped.status == "ok", restopped.message
    assert restopped.result["episode_id"] == continuation.episode_id


def test_another_chat_cannot_control_the_episode(manifest, tmp_path: Path, monkeypatch) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.settling_orchestrator()
    kickoff = member.turn("kickoff-turn")
    pending = kickoff("go", KICKOFF)
    assert pending.result["disposition"] == "card_pending"
    card = member.land("kickoff-turn")
    unready = member.start(card)
    assert unready.json()["detail"]["code"] == "provider_not_ready"
    assert store.approval_card(card.question_id)["state"] == "pending"
    member.provider_ready()
    started = member.start(card)
    assert started.status_code == 200, started.text
    episode_id = started.json()["result"]["episode_id"]
    other = member.turn("other-turn", OTHER_CHAT)

    for arguments in (
        {"action": "stop", "episode_id": episode_id},
        {"action": "pause", "episode_id": episode_id},
        {"action": "resume", "episode_id": episode_id},
        {"action": "add_turns", "episode_id": episode_id, "invocation_limit": 2},
        {"action": "message", "episode_id": episode_id, "body_file": "note.md"},
    ):
        refused = other(str(arguments["action"]), arguments)
        assert (refused.status, refused.result["code"]) == ("invalid", "episode_not_started_here")
    assert store.episode(episode_id).stop_requested_at is None  # type: ignore[union-attr]
    assert store.auto_research_messages(episode_id) == []


def test_a_refused_start_retries_with_edited_arguments(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    store = member.store
    member.settling_orchestrator()
    kickoff = member.turn("kickoff-turn")
    assert kickoff("go", KICKOFF).result["disposition"] == "card_pending"
    card = member.land("kickoff-turn")
    unready = member.start(card)
    assert unready.json()["detail"]["code"] == "provider_not_ready"
    member.provider_ready()
    started = member.start(card, invocation_ceiling=4, starting_instruction="Compare batch sizes.")
    assert started.status_code == 200, started.text
    episode_id = started.json()["result"]["episode_id"]
    episode = store.episode(episode_id)
    assert episode is not None and episode.invocation_ceiling == 4
    changed = kickoff("go", {**KICKOFF, "invocation_limit": 4})
    assert changed.status == "invalid"

    root_id = store.episode(episode_id).root_operation_id  # type: ignore[union-attr]
    wait_for_task(store, root_id, expect="succeeded")
    paused = kickoff("pause", {"action": "pause", "episode_id": episode_id})
    assert (paused.status, paused.result["code"]) == ("invalid", "episode_not_pausable")


def test_auto_research_kickoff_waits_for_turn_success_and_a_human_start(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    member = _Member(manifest, tmp_path, monkeypatch)
    call = member.turn("plan-turn")
    response = call("go", KICKOFF)
    assert (response.status, response.result) == ("ok", {"disposition": "card_pending"})
    assert member.store.episodes(member.project_id, limit=None) == []
    assert call("go", KICKOFF).result == response.result
    assert call("go", {**KICKOFF, "invocation_limit": 4}).status == "invalid"
    receipts = member.store.agent_task_receipts_by_category(
        "plan-turn", "work_auto_research_kickoff"
    )
    assert len(receipts) == 1
    assert receipts[0].payload["phase"] == "card_pending"


def test_auto_research_recovery_does_not_replace_a_newer_plan(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    from rcp.runs.work_experiment_commands import raise_pending_experiment_cards

    member = _Member(manifest, tmp_path, monkeypatch)
    call = member.turn("old-turn")
    assert call("go", KICKOFF).result["disposition"] == "card_pending"
    older = member.land("old-turn")
    call = member.turn("lost-turn")
    assert call("lost", KICKOFF).result["disposition"] == "card_pending"
    member.store.complete_agent_task("lost-turn", applied_revision=None, result={"messages": []})
    newer_call = member.turn("newer-turn")
    assert newer_call("newer", KICKOFF).result["disposition"] == "card_pending"
    newer = member.land("newer-turn")
    raise_pending_experiment_cards(member.store, member.background.recorded_execution)
    assert len(member.store.list_questions(project_id=member.project_id)) == 2
    assert member.store.approval_card(older.question_id)["result"]["outcome"] == "stale"
    assert member.store.approval_card(newer.question_id)["state"] == "pending"
    assert member.store.pending_experiment_card_tasks() == []


def test_auto_research_old_direct_start_receipt_still_replays(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    from rcp.runs.work_episode_controls import (
        WorkAutoResearchStart,
        intent_sha256,
        turn_keyed_branch_id,
    )

    member = _Member(manifest, tmp_path, monkeypatch)
    member.provider_ready()
    member.settling_orchestrator()
    call = member.turn("old-writer")
    episode_id = turn_keyed_branch_id(("old-writer",), "auto-research", "old")
    goal = (member.workspace / "goal.md").read_text()
    arguments = _episode("old", KICKOFF).arguments
    member.store.record_agent_task_receipt(
        "old-writer",
        "work_auto_research_kickoff",
        {
            "episode_id": episode_id,
            "intent_sha256": intent_sha256({**arguments.model_dump(mode="json"), "goal": goal}),
        },
        tier="diagnostic",
    )
    episode = member.background.episode_controls.start_auto_research(
        WorkAutoResearchStart(
            source_operation_id="old-writer",
            episode_id=episode_id,
            goal=goal,
            invocation_ceiling=3,
            code_worktree=None,
            worktree_repository=None,
            branch_name=None,
            provider=None,
            model=None,
            reasoning=None,
        )
    )
    replayed = call("old", KICKOFF)
    assert replayed.status == "ok"
    assert (replayed.result["disposition"], replayed.result["episode_id"]) == (
        "existing",
        episode_id,
    )
    assert call("old", {**KICKOFF, "invocation_limit": 4}).status == "invalid"
    member.store.complete_agent_task("old-writer", applied_revision=None, result={"messages": []})
    member.background.experiment_card_raiser(member.executions["old-writer"])
    assert member.store.list_questions(project_id=member.project_id) == []
    assert len(member.store.episodes(member.project_id, limit=None)) == 1
    wait_for_task(member.store, episode.root_operation_id)


def test_auto_research_card_requires_an_authorized_work_turn_on_main(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    from rcp.core.transition_models import GraphTargetRef

    member = _Member(manifest, tmp_path, monkeypatch)
    call = member.turn("gated-turn")
    with monkeypatch.context() as patch:
        patch.setattr("rcp.runs.questions.work_ask_authorized", lambda _execution: False)
        refused = call("go", KICKOFF)
        assert refused.result == {"code": "auto_research_card_unavailable"}
    source = member.store.agent_task("gated-turn")
    assert source is not None
    on_branch = source.model_copy(
        update={"graph_target": GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))}
    )
    original = member.store.agent_task
    with monkeypatch.context() as patch:
        patch.setattr(
            member.store,
            "agent_task",
            lambda operation_id: (
                on_branch if operation_id == "gated-turn" else original(operation_id)
            ),
        )
        refused = call("go", KICKOFF)
        assert refused.result == {"code": "auto_research_starts_from_main"}
    assert (
        member.store.agent_task_receipts_by_category("gated-turn", "work_auto_research_kickoff")
        == []
    )
