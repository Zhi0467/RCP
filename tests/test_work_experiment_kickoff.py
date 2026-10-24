"""A Work turn starts the Experiment its human asked for, as their Run would."""

from __future__ import annotations

import uuid
from pathlib import Path

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import EpisodeCommandRequest, ExperimentKickoffArguments
from rcp.background import AgentTaskExecution
from rcp.core.models import Patch
from rcp.loop_status import episode_loop_metadata
from rcp.runs.questions import work_command_handler
from rcp.runs.shared import _sse
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.transport.workspace_mailbox import RunStageMailbox

from .helpers import append_fixture_patch, authorized_human, seed_patch, wait_for_task
from .helpers import create_named_app as create_app

EXPERIMENT_ID = "exp/work-started"
WORK_TURN = "work-turn"


def _experiment_patch() -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Added an Experiment a Work chat can start.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "type": "experiment",
                        "title": "Work-started loop",
                        "objective": "Exercise a Work kickoff.",
                        "completion_criteria": ["The comparison finishes."],
                        "invocation_ceiling": 3,
                    }
                ],
            }
        ],
    )


def _kickoff(key: str, *, goal_file: str = "goal.md", limit: int | None = None):
    return EpisodeCommandRequest(
        mailbox_id="a" * 32,
        request_id=uuid.uuid4().hex,
        credential="b" * 64,
        verb="episode",
        idempotency_key=key,
        arguments=ExperimentKickoffArguments(
            action="kick_off_experiment",
            node_id=EXPERIMENT_ID,
            goal_file=goal_file,
            invocation_limit=limit,
        ),
    )


def test_work_turn_starts_the_experiment_its_human_asked_for(manifest, tmp_path: Path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    background = app.state.background_tasks
    store = background.store
    project_id = app.state.default_project_id
    human = authorized_human(store)

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(AgentEvent(event="done"))

    background.stream = stream
    chat_id = str(uuid.uuid4())
    request = RunRequest(
        provider="codex",
        run_truth_scope=["repo-a"],
        chat_scope="project",
        chat_id=chat_id,
        message="Run the comparison.",
        mode="work",
    )
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=WORK_TURN,
            project_id=project_id,
            kind="project_chat",
            status="running",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Working.",
            authorized_by=human,
            dispatch_authority=resolve_dispatch_authority("project_chat", request),
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "goal.md").write_text("Compare warmup schedules at lr 3e-4.", encoding="utf-8")
    (workspace / "other.md").write_text("A different goal.", encoding="utf-8")
    execution = AgentTaskExecution(
        operation_id=WORK_TURN,
        store=store,
        control=AgentProcessControl(),
        experiment_starter=background.experiment_starter,
    )
    handler = work_command_handler(execution, None, mailbox=RunStageMailbox(workspace, None))
    assert "episode" in handler.allowed_verbs
    identity = CommandTurnIdentity(
        episode_id=None, task_id=WORK_TURN, turn_id="turn", authority="broker"
    )

    # The agent may shorten an episode, never lengthen it past the node's limit.
    assert handler(_kickoff("too-long", limit=4), identity).status == "invalid"
    started = handler(_kickoff("first", limit=2), identity)

    assert started.status == "ok", started.message
    assert started.result["disposition"] == "created"
    episode = store.episode(started.result["episode_id"])
    assert episode is not None
    assert episode.control_node_id == EXPERIMENT_ID
    assert episode.authorized_by == human
    assert episode.invocation_ceiling == 2
    root = store.agent_task(episode.root_operation_id or "")
    assert root is not None
    assert root.request["message"] == "Compare warmup schedules at lr 3e-4."
    assert root.request["started_from_operation_id"] == WORK_TURN
    starter = episode_loop_metadata(store, episode).started_by
    assert (starter.kind, starter.chat_id) == ("chat", chat_id)
    wait_for_task(store, root.operation_id)

    # The same key replays the same episode; a changed goal under it is refused.
    replayed = handler(_kickoff("first", limit=2), identity)
    assert replayed.status == "ok"
    assert (replayed.result["disposition"], replayed.result["episode_id"]) == (
        "existing",
        episode.episode_id,
    )
    assert handler(_kickoff("first", goal_file="other.md", limit=2), identity).status == "invalid"
    assert [
        item.episode_id
        for item in store.episodes(project_id, limit=None)
        if item.mode == "experiment_loop"
    ] == [episode.episode_id]

    # An Experiment turn cannot start another Experiment.
    loop_execution = AgentTaskExecution(
        operation_id=root.operation_id,
        store=store,
        control=AgentProcessControl(),
        experiment_starter=background.experiment_starter,
    )
    assert "episode" not in work_command_handler(loop_execution, None).allowed_verbs
