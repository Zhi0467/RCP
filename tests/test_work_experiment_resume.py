"""A Work turn resumes the Experiment that paused for its human, once the turn ends."""

from __future__ import annotations

import uuid
from pathlib import Path

from rcp.agents import AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import EpisodeCommandRequest, EpisodeControlArguments
from rcp.background import AgentTaskExecution
from rcp.runs.questions import work_command_handler
from rcp.runs.work_experiment_commands import start_scheduled_experiment_resumes
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord

from .helpers import create_named_app, wait_for_task
from .test_episode_continuation import _session_resuming_stream
from .test_experiment_stop import EXPERIMENT_ID, _Loop

WORK_TURN = "work-reply"


def _resume(episode_id: str, key: str) -> EpisodeCommandRequest:
    return EpisodeCommandRequest(
        mailbox_id="a" * 32,
        request_id=uuid.uuid4().hex,
        credential="b" * 64,
        verb="episode",
        idempotency_key=key,
        arguments=EpisodeControlArguments(action="resume", episode_id=episode_id),
    )


def test_work_reply_resumes_a_paused_experiment_with_its_unused_turns(
    manifest, tmp_path: Path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    loop = _Loop(app, invocation_ceiling=3)
    loop.start_episode()
    stage = tmp_path / "loop-stage"
    loop.bind_session(stage, native_session_id="native-session-abc")
    loop.settle_exhausted_ending(ending="human_pause")
    tasks = app.state.background_tasks
    store = tasks.store
    seen: list[tuple[str | None, str | None]] = []
    tasks.stream = _session_resuming_stream(
        kind="node_chat", session_id="native-session-abc", stage_root=str(stage), seen=seen
    )
    # The human answers in the Experiment's own chat, on the session the episode resumes.
    request = RunRequest(
        provider="codex",
        run_truth_scope=["repo-a"],
        chat_scope="node",
        node_id=EXPERIMENT_ID,
        chat_id=loop.chat_id,
        session_id="native-session-abc",
        message="Signed in again; carry on.",
        mode="work",
    )

    def work_turn(operation_id: str):
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
        execution = AgentTaskExecution(
            operation_id=operation_id,
            store=store,
            control=AgentProcessControl(),
            experiment_starter=tasks.experiment_starter,
        )
        handler = work_command_handler(execution, None)
        assert "episode" in handler.allowed_verbs
        identity = CommandTurnIdentity(
            episode_id=None, task_id=operation_id, turn_id="turn", authority="broker"
        )
        return lambda command: handler(command, identity)

    # A turn that fails resumes nothing it scheduled.
    failed = work_turn("work-failed")
    assert failed(_resume(loop.episode_id, "resume")).status == "ok"
    store.fail_agent_task("work-failed", "The provider stopped.")
    start_scheduled_experiment_resumes(store, "work-failed", tasks.experiment_resumer)
    assert store.episode_continuation(loop.episode_id) is None

    reply = work_turn(WORK_TURN)
    assert reply(_resume("no-such-episode", "missing")).status == "invalid"
    scheduled = reply(_resume(loop.episode_id, "resume"))
    assert scheduled.status == "ok", scheduled.message
    assert (scheduled.result["disposition"], scheduled.result["turns"]) == ("scheduled", 2)
    # Nothing starts while the turn runs.
    assert store.episode_continuation(loop.episode_id) is None

    store.complete_agent_task(WORK_TURN, applied_revision=None, result={"messages": []})
    start_scheduled_experiment_resumes(store, WORK_TURN, tasks.experiment_resumer)
    continuation = store.episode_continuation(loop.episode_id)
    assert continuation is not None
    assert continuation.invocation_ceiling == 2
    assert continuation.authorized_by == loop.authorizer
    root = wait_for_task(store, continuation.root_operation_id or "", expect="succeeded")
    assert root.native_session_id == "native-session-abc"
    assert seen == [("native-session-abc", str(stage))]

    # Settling again replays the same continuation instead of a second one.
    start_scheduled_experiment_resumes(store, WORK_TURN, tasks.experiment_resumer)
    assert store.episode_continuation(loop.episode_id) == store.episode(continuation.episode_id)
    loops = [
        item.episode_id
        for item in store.episodes(loop.project_id, limit=None)
        if item.mode == "experiment_loop"
    ]
    assert sorted(loops) == sorted([loop.episode_id, continuation.episode_id])
