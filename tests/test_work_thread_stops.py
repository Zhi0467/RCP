"""A member's Work chat stops the Experiments it started, and nothing else."""

from __future__ import annotations

import uuid
from pathlib import Path

from rcp.agents import AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import EpisodeCommandRequest, EpisodeControlArguments
from rcp.background import AgentTaskExecution
from rcp.runs.questions import work_command_handler
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord

from .helpers import create_named_app
from .lifecycles.episode import EXPERIMENT_ID, Loop

THREAD_CHAT = str(uuid.uuid4())
OTHER_CHAT = str(uuid.uuid4())


def _envelope() -> dict[str, str]:
    return {"mailbox_id": "a" * 32, "request_id": uuid.uuid4().hex, "credential": "b" * 64}


def _stop_episode(episode_id: str) -> EpisodeCommandRequest:
    return EpisodeCommandRequest(
        **_envelope(),
        verb="episode",
        idempotency_key="stop",
        arguments=EpisodeControlArguments(action="stop", episode_id=episode_id),
    )


def _work_turn(loop: Loop, operation_id: str, chat_id: str, *, mode: str = "work"):
    """A member's Work (or Discuss) turn in ``chat_id`` and the command handler it is granted."""

    store = loop.store
    request = RunRequest(
        provider="codex",
        run_truth_scope=["repo-a"],
        chat_scope="node",
        node_id=EXPERIMENT_ID,
        chat_id=chat_id,
        message="Stop that run.",
        mode=mode,
    )
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=loop.project_id,
            kind="node_chat",
            status="running",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Working.",
            authorized_by=loop.authorizer,
            dispatch_authority=resolve_dispatch_authority("node_chat", request),
        )
    )
    background = loop.app.state.background_tasks
    execution = AgentTaskExecution(
        operation_id=operation_id,
        store=store,
        control=AgentProcessControl(),
        experiment_card_raiser=background.experiment_card_raiser,
        episode_controls=background.episode_controls,
    )
    handler = work_command_handler(execution, None)
    identity = CommandTurnIdentity(
        episode_id=None, task_id=operation_id, turn_id="turn", authority="broker"
    )
    return handler, lambda command: handler(command, identity)


def test_a_work_chat_stops_only_the_experiment_it_started(manifest, tmp_path: Path) -> None:
    loop = Loop(create_named_app(str(manifest.path), data_dir=tmp_path / "data"))
    _work_turn(loop, "kickoff-turn", THREAD_CHAT)
    loop.start_episode(
        status="running",
        request=loop.root_request().model_copy(
            update={"started_from_operation_id": "kickoff-turn"}
        ),
    )
    store = loop.store
    store.complete_agent_task("kickoff-turn", applied_revision=None, result={})

    discuss, _ = _work_turn(loop, "discuss-turn", THREAD_CHAT, mode="discuss")
    assert "episode" not in discuss.allowed_verbs
    store.complete_agent_task("discuss-turn", applied_revision=None, result={})
    _, other = _work_turn(loop, "other-chat-turn", OTHER_CHAT)
    refused = other(_stop_episode(loop.episode_id))
    assert (refused.status, refused.result["code"]) == ("invalid", "episode_not_started_here")
    assert store.episode(loop.episode_id).stop_requested_at is None  # type: ignore[union-attr]

    granted, own = _work_turn(loop, "later-turn", THREAD_CHAT)
    assert "episode" in granted.allowed_verbs
    stopped = own(_stop_episode(loop.episode_id))
    assert stopped.status == "ok", stopped.message
    episode = store.episode(loop.episode_id)
    assert episode is not None and episode.stop_requested_at is not None
    assert episode.stop_initiated_by == "agent:later-turn"
    assert (stopped.result["disposition"], stopped.result["status"]) == ("stopping", "stopping")
    task = store.agent_task("loop-root")
    assert task is not None and task.status == "running"

    replayed = own(_stop_episode(loop.episode_id))
    assert replayed.status == "ok"
    assert store.episode(loop.episode_id).stop_requested_at == episode.stop_requested_at  # type: ignore[union-attr]
