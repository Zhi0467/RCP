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
    return next(block["loop_status"] for block in blocks if "loop_status" in block)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["live", "stopped", "completed", "none", "unavailable"])
@pytest.mark.parametrize(
    ("mode", "continuation"),
    [
        ("discuss", "fresh"),
        ("work", "fresh"),
        ("discuss", "resume"),
        ("work", "resume"),
        ("work", "watcher_wake"),
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
    assert status["node_id"] == EXPERIMENT_ID
    assert status["graph_target"] == GraphTargetRef().model_dump(mode="json")
    assert status["live_elsewhere"] == []
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
    elif state == "none":
        assert status["current"] is None and watchers == []
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

    def capture(rows):
        rendered.append([row.model_dump(mode="json") for row in rows])
        return render(rows)

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
    assert rendered and all(
        [row["episode_id"] for row in rows] == [loop.episode_id] for rows in rendered
    )
    assert all(
        rows[0]["graph_target"] == GraphTargetRef().model_dump(mode="json") for rows in rendered
    )
    episode = loop.store.episode(loop.episode_id)
    assert episode is not None and episode.stop_requested_at is None
