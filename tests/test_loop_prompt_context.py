from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.api.task_requests import _resolved_graph_request
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.work import stream_work_run
from rcp.service import RunRequest
from rcp.storage import EpisodeRecord

from .helpers import create_named_app
from .test_api import ScriptedLauncher
from .test_experiment_loop_agent_io import _events, _execution
from .test_experiment_stop import EXPERIMENT_ID, _Loop


class _InterruptibleLauncher(ScriptedLauncher):
    interrupted = False

    async def stream(self, provider, prompt, **kwargs):
        async for event in super().stream(provider, prompt, **kwargs):
            yield event
            if self.interrupted and event.event == "session":
                yield AgentEvent(event="error", text="Interrupted fixture turn.")
                return


def _status(prompt: str) -> dict:
    blocks = [json.loads(value) for value in re.findall(r"```json\n(.*?)\n```", prompt, re.S)]
    statuses = [block["loop_status"] for block in blocks if "loop_status" in block]
    assert len(statuses) == 1
    return statuses[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["discuss", "work"])
async def test_non_control_node_chat_has_no_loop_status(manifest, tmp_path, mode):
    data_dir = tmp_path / "data"
    loop = _Loop(create_named_app(str(manifest.path), data_dir=data_dir))
    request = _resolved_graph_request(
        loop.service,
        "node_chat",
        RunRequest(
            chat_scope="node",
            chat_id=str(uuid.uuid4()),
            node_id="hyp/replanning-restores-plasticity",
            message="Inspect this node.",
            mode=mode,
            run_on="laptop",
            run_truth_scope=["repo-a"],
        ),
    )
    execution = _execution(loop.store, loop.project_id, "non-control", request)
    launcher = ScriptedLauncher([{}], message="Inspected.")
    stream = stream_work_run if mode == "work" else stream_discuss_run
    events = await _events(
        stream(loop.service, cast(AgentLauncher, launcher), request, data_dir, execution=execution)
    )
    assert not [event.text for event in events if event.event == "error"]
    blocks = [
        json.loads(value)
        for value in re.findall(r"```json\n(.*?)\n```", launcher.prompts[-1], re.S)
    ]
    assert all("loop_status" not in block for block in blocks)
    assert not list(launcher.workspaces[0].parent.glob("inputs/loop-status-watchers-*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "mode", "continuation"),
    [
        (state, "discuss", "fresh")
        for state in ["live", "stopped", "completed", "none", "unavailable"]
    ]
    + [
        ("stopped", mode, continuation)
        for mode, continuation in [
            ("work", "fresh"),
            ("discuss", "resume"),
            ("work", "resume"),
            ("discuss", "watcher_wake"),
            ("work", "watcher_wake"),
        ]
    ],
)
async def test_later_node_turn_refreshes_status_without_maintenance_authority(
    manifest, tmp_path, state, mode, continuation
):
    data_dir = tmp_path / "data"
    loop = _Loop(create_named_app(str(manifest.path), data_dir=data_dir))
    if state in {"live", "stopped", "completed"}:
        loop.start_episode()
        loop.bind_session(tmp_path / "loop-stage")
        loop.arm_watcher("loop-observer", status="active")
    launcher = _InterruptibleLauncher([{}], message="Inspected.")
    launcher.interrupted = continuation == "resume"
    request = RunRequest(
        chat_scope="node",
        chat_id=str(uuid.uuid4()),
        node_id=EXPERIMENT_ID,
        message="Inspect this node.",
        mode=mode if continuation == "resume" else "discuss",
        run_on="laptop",
        run_truth_scope=["repo-a"],
    )
    request = _resolved_graph_request(loop.service, "node_chat", request)
    first = _execution(loop.store, loop.project_id, "first", request)
    first_stream = stream_work_run if request.mode == "work" else stream_discuss_run
    events = await _events(
        first_stream(
            loop.service, cast(AgentLauncher, launcher), request, data_dir, execution=first
        )
    )
    assert bool([event for event in events if event.event == "error"]) == launcher.interrupted
    loop.store.checkpoint_agent_task("first", native_session_id=launcher.native_session_id)
    if launcher.interrupted:
        loop.store.fail_agent_task("first", "Interrupted fixture turn.", status="interrupted")
    else:
        loop.store.complete_agent_task("first", applied_revision=None, result={})
    launcher.interrupted = False
    before = _status(launcher.prompts[-1])

    if state == "stopped":
        loop.stop()
    elif state == "completed":
        loop.store.end_episode_without_report(loop.episode_id, ending="completed")
    elif state == "unavailable":
        now = loop.store.now()
        orphan = loop.store.create_episode(
            EpisodeRecord(
                episode_id=str(uuid.uuid4()),
                project_id=loop.project_id,
                mode="experiment_loop",
                control_node_id=EXPERIMENT_ID,
                status="queued",
                authorized_by=loop.authorizer,
                invocation_ceiling=1,
                created_at=now,
                updated_at=now,
            )
        )
        loop.store.end_episode_without_report(orphan.episode_id, ending="completed")
    request = request.model_copy(
        update={
            "mode": mode,
            "session_id": launcher.native_session_id,
            "trigger": "watcher" if continuation == "watcher_wake" else "human",
        }
    )
    first_task = loop.store.agent_task("first")
    assert first_task is not None
    second = _execution(
        loop.store,
        loop.project_id,
        "second",
        request,
        continuation=continuation,
        parent_operation_id="first" if continuation == "resume" else None,
        stage_root=first_task.stage_root,
    )
    stream = stream_work_run if mode == "work" else stream_discuss_run
    events = await _events(
        stream(loop.service, cast(AgentLauncher, launcher), request, data_dir, execution=second)
    )
    assert not [event.text for event in events if event.event == "error"]
    status = _status(launcher.prompts[-1])
    assert status["state"] == state
    assert "node_id" not in status and "graph_target" not in status
    assert status["live_elsewhere"] == {"rows": [], "omitted": 0}
    path = Path(status["watcher_state_path"])
    assert path.is_file() and path.stat().st_mode & 0o222 == 0
    assert not list(path.parent.glob("main-graph-*.json"))
    watchers = json.loads(path.read_text())
    if state == "stopped":
        assert status["current"]["episode_id"] == loop.episode_id
        assert status["current"]["stop_initiated_by"] is not None
        assert status["current"]["stop_settled_at"] is not None
        assert [(item["watcher_id"], item["status"]) for item in watchers] == [
            ("loop-observer", "stopped")
        ]
        assert status["watcher_state_path"] != before["watcher_state_path"]
        for operation_id in ("first", "second"):
            master = loop.store.agent_task_contract(operation_id, "session_master")
            if master is not None:
                master_files = [
                    content
                    for snapshot in launcher.input_snapshots
                    for content in snapshot.values()
                    if content == master
                ]
                assert master_files
                blocks = [
                    json.loads(value)
                    for value in re.findall(r"```json\n(.*?)\n```", master_files[0], re.S)
                ]
                assert all("loop_status" not in block for block in blocks)
        assert not Path(before["watcher_state_path"]).exists()
    elif state == "none":
        assert "current" not in status and watchers == []
    if state in {"stopped", "none", "unavailable"}:
        assert (
            loop.store.experiment_watcher_resources(loop.project_id, graph_target=GraphTargetRef())
            == []
        )


def test_experiment_loop_prompt_receives_other_target_rows_from_shared_renderer(
    manifest, tmp_path, monkeypatch
):
    from rcp.agents import experiment_loop_prompt

    from .helpers import wait_for_task

    loop = _Loop(create_named_app(str(manifest.path), data_dir=tmp_path / "data"))
    loop.start_episode()
    rendered = []
    render = experiment_loop_prompt.render_loop_overlap

    def capture(rows, *, ask_allowed):
        rendered.append([row.model_dump(mode="json") for row in rows.rows])
        return render(rows, ask_allowed=ask_allowed)

    monkeypatch.setattr(experiment_loop_prompt, "render_loop_overlap", capture)
    patch = {
        "summary": "Completed branch experiment.",
        "ops": [
            {
                "op": "update_nodes",
                "nodes": [
                    {"id": EXPERIMENT_ID, "changes": {"status": "completed", "next_action": None}}
                ],
            }
        ],
        "repositories_read": [],
        "change_summary": [],
    }
    launcher = ScriptedLauncher(
        [{"patch.json": json.dumps(patch), "watch.json": '{"external":[],"graph":[]}'}],
        message="Completed branch experiment.",
    )
    monkeypatch.setattr(loop.app.state.launcher, "stream", launcher.stream)
    response = loop.client.post(
        f"/api/projects/{loop.project_id}/experiments/exp%2Fbounded-loop/run",
        json={"chat_id": str(uuid.uuid4()), "graph_isolation": True},
    )
    assert response.status_code == 202, response.text
    task = wait_for_task(loop.store, response.json()["operation_id"])
    assert task.status == "succeeded", task.error
    assert len(rendered) == launcher.calls == 1
    master = loop.store.agent_task_contract(task.operation_id, "session_master")
    assert master is not None
    assert master in launcher.input_snapshots[0].values()
    assert loop.episode_id not in master
    assert not list(launcher.workspaces[0].parent.glob("inputs/loop-status-watchers-*.json"))
    assert rendered and all(
        [row["episode_id"] for row in rows] == [loop.episode_id] for rows in rendered
    )
    assert all(
        rows[0]["graph_target"] == GraphTargetRef().model_dump(mode="json") for rows in rendered
    )
    episode = loop.store.episode(loop.episode_id)
    assert episode is not None and episode.stop_requested_at is None


@pytest.mark.asyncio
async def test_failed_node_status_staging_closes_the_work_mailbox(manifest, tmp_path, monkeypatch):
    from rcp.runs.tasks import work

    data_dir = tmp_path / "data"
    loop = _Loop(create_named_app(str(manifest.path), data_dir=data_dir))
    request = _resolved_graph_request(
        loop.service,
        "node_chat",
        RunRequest(
            chat_scope="node",
            chat_id=str(uuid.uuid4()),
            node_id=EXPERIMENT_ID,
            message="Inspect this node.",
            mode="work",
            run_on="laptop",
            run_truth_scope=["repo-a"],
        ),
    )
    execution = _execution(loop.store, loop.project_id, "status-failure", request)
    owners = []
    start = work._start_work_validator_mailbox

    def capture(*args, **kwargs):
        owner = start(*args, **kwargs)
        owners.append(owner)
        return owner

    def fail(*args, **kwargs):
        raise ValueError("Unavailable read context fixture.")

    monkeypatch.setattr(work, "_start_work_validator_mailbox", capture)
    monkeypatch.setattr(work, "stage_chat_loop_status", fail)
    launcher = ScriptedLauncher([{}])
    events = await _events(
        stream_work_run(
            loop.service, cast(AgentLauncher, launcher), request, data_dir, execution=execution
        )
    )
    assert [event.event for event in events] == ["error"]
    assert launcher.calls == 0
    assert len(owners) == 1
    assert owners[0].closed and owners[0].staged.credential.expired


@pytest.mark.parametrize(
    ("child", "rule_id"), [(False, "ask_human"), (True, "escalate_to_orchestrator")]
)
def test_loop_interference_prompt_uses_resolved_launch_verbs(manifest, tmp_path, child, rule_id):
    from types import SimpleNamespace

    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.questions import work_command_handler
    from rcp.runs.tasks.experiment_loop import _loop_read_context
    from rcp.runs.tasks.work import WorkTurn

    from .test_experiment_index import _record_branch_target_child_experiment, _seed_indexed_project

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    if child:
        _seed_indexed_project(app)
        _parent, episode = _record_branch_target_child_experiment(app)
    else:
        loop = _Loop(app)
        loop.start_episode(status="running")
        episode = store.episode(loop.episode_id)
    assert episode is not None and episode.root_operation_id is not None
    execution = AgentTaskExecution(
        operation_id=episode.root_operation_id, store=store, control=AgentProcessControl()
    )
    service = app.state.service.for_graph_target(episode.graph_target)
    request = RunRequest(
        chat_scope="node",
        node_id=episode.control_node_id,
        control_node_id=episode.control_node_id,
        run_on="laptop",
        run_truth_scope=["repo-a"],
    )
    turn = cast(
        WorkTurn,
        SimpleNamespace(
            execution=execution,
            request=request,
            context=service.assemble_chat(request),
            compute_commands=None,
        ),
    )
    prompt = _loop_read_context(turn)
    rule = next(
        json.loads(line)["interference_rule"]
        for line in prompt.splitlines()
        if line.startswith('{"interference_rule":')
    )
    assert rule["id"] == rule_id
    assert ("ask" in work_command_handler(execution, None).allowed_verbs) is (not child)
