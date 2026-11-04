"""A member's Work chat runs their Auto-research controls on the episodes it started."""

from __future__ import annotations

import uuid
from pathlib import Path

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import EpisodeCommandRequest
from rcp.background import AgentTaskExecution
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.questions import work_command_handler
from rcp.runs.shared import _sse
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.transport.workspace_mailbox import RunStageMailbox

from .helpers import async_wait_until, authorized_human, wait_for_task, wait_until
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
                authorized_by=self.human,
                dispatch_authority=resolve_dispatch_authority("project_chat", request),
            )
        )
        execution = AgentTaskExecution(
            operation_id=operation_id,
            store=self.store,
            control=AgentProcessControl(),
            experiment_starter=self.background.experiment_starter,
            episode_controls=self.background.episode_controls,
        )
        handler = work_command_handler(
            execution, None, mailbox=RunStageMailbox(self.workspace, None)
        )
        identity = CommandTurnIdentity(
            episode_id=None, task_id=operation_id, turn_id="turn", authority="broker"
        )
        return lambda key, arguments: handler(_episode(key, arguments), identity)

    def pausable_orchestrator(self) -> None:
        stage = str(self.stage)

        async def stream(_project_id, _kind, request, execution):
            execution.checkpoint_stage("", stage)
            yield _sse(AgentEvent(event="session", session_id="orchestrator-session"))
            await async_wait_until(execution.control.pause_requested.is_set)
            yield _sse(AgentEvent(event="paused", text="Provider process paused."))

        self.background.stream = stream

    def settling_orchestrator(self) -> None:
        stage = str(self.stage)

        async def stream(_project_id, _kind, _request, execution):
            execution.checkpoint_stage("", stage)
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
    assert started.result["disposition"] == "created"
    episode_id = str(started.result["episode_id"])
    episode = store.episode(episode_id)
    assert episode is not None and episode.mode == "auto_research"
    assert episode.authorized_by == member.human
    assert episode.invocation_ceiling == 3
    assert store.episode_started_from_chat(episode_id).chat_id == THREAD_CHAT  # type: ignore[union-attr]
    replayed = act("go", KICKOFF)
    assert (replayed.status, replayed.result["episode_id"]) == ("ok", episode_id)
    assert replayed.result["disposition"] == "existing"
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
    unready = kickoff("go", KICKOFF)
    assert (unready.status, unready.result["code"]) == ("invalid", "provider_not_ready")
    member.provider_ready()
    started = kickoff("go", KICKOFF)
    assert started.status == "ok", started.message
    episode_id = str(started.result["episode_id"])
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
