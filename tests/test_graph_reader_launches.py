from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.prompts import render_graph_history
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.tasks.coach import stream_coach
from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.graph import stream_graph_run
from rcp.runs.tasks.work import stream_work_run

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    async_wait_until,
    changed_values,
    create_named_app,
    launch_contract_path,
    refresh_patch,
    seed_patch,
    signed_in_client,
    wait_for_task_response,
    write_turn_outcome,
)
from .paper_helpers import wire_saved_paper
from .test_consolidation_work import _apply_request
from .test_experiment_loop_agent_io import _LoopLauncher
from .test_graph_history_commands import request as history_request


@pytest.fixture
def history_handlers(monkeypatch):
    from rcp.runs import patch_validator
    from rcp.runs.tasks import auto_research_child_work, auto_research_stream

    handlers = {}
    original = patch_validator.serve_command_mailbox

    async def capture(*, staged, handler, **kwargs):
        handlers[staged.credential.mailbox_id] = (staged.credential.identity, handler)
        await original(staged=staged, handler=handler, **kwargs)

    for module in (patch_validator, auto_research_stream, auto_research_child_work):
        monkeypatch.setattr(module, "serve_command_mailbox", capture)
    return handlers


class _HistoryLauncher:
    def __init__(self, service, handlers, *, kind, succeed=True, during_provider=None):
        self.service = service
        self.handlers = handlers
        self.kind = kind
        self.succeed = succeed
        self.during_provider = during_provider
        self.calls = 0
        self.masters = []
        self.prompts = []

    async def stream(self, provider, prompt, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        gate = kwargs["invocation_gate"]
        root = Path(gate.workspace)
        master = launch_contract_path(prompt).read_text()
        self.masters.append(master)
        command = next(
            code.split(" graph_diff ")[0]
            for code in master.split("`")[1::2]
            if " graph_diff " in code
        )
        scope = GraphReadScope(
            project_id=self.service._project_id,
            target=self.service.history.graph_target,
        )
        assert render_graph_history(scope, command) in master
        await async_wait_until(lambda: gate.mailbox_id in self.handlers)
        identity, handler = self.handlers[gate.mailbox_id]

        response = await handler(history_request(**{"from": "main@0", "to": "@head"}), identity)
        assert response.status == "ok", response
        assert response.result["to_head"] == (
            f"{scope.target.key}@{self.service.history.state().revision}"
        )
        if self.kind in {"discuss", "paper_coach"}:
            response = await handler(_apply_request("forbidden"), identity)
            assert response.status == "invalid", response
        if self.during_provider is not None:
            self.during_provider()
        yield AgentEvent(event="session", session_id="reader-session")
        if not self.succeed:
            yield AgentEvent(event="error", text="provider failed")
            return
        if self.kind in {"work", "consolidation"}:
            write_turn_outcome(root)
        if self.kind in {"seed", "refresh"}:
            patch = seed_patch() if self.kind == "seed" else refresh_patch()
            (root / "patch.json").write_text(agent_patch_json(patch))
        if self.kind == "experiment":
            launcher = _LoopLauncher("reader-session", root, write_handoff=True)
            async for event in launcher.stream(provider, prompt, **kwargs):
                yield event
            return
        yield AgentEvent(event="answer", text="Read the graph history.")
        yield AgentEvent(event="done")


@pytest.mark.parametrize("kind", ["seed", "refresh", "paper_coach", "discuss", "work"])
@pytest.mark.parametrize("succeed", [True, False])
def test_graph_reader_master_commands_and_committed_observation(
    manifest, tmp_path, history_handlers, kind, succeed
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.paper.create()
    if kind == "paper_coach":
        wire_saved_paper(service)
    if kind != "seed":
        append_fixture_patch(service, seed_patch())
    input_revision = service.history.state().revision
    launcher = _HistoryLauncher(service, history_handlers, kind=kind, succeed=succeed)

    async def stream(project_id, task_kind, request, execution):
        agent = cast(AgentLauncher, launcher)
        if kind == "paper_coach":
            frames = stream_coach(
                service, agent, service.paper, request, tmp_path / "data", execution
            )
        elif kind in {"discuss", "work"}:
            run = stream_discuss_run if kind == "discuss" else stream_work_run
            frames = run(service, agent, request, tmp_path / "data", execution=execution)
        else:
            frames = stream_graph_run(service, agent, kind, request, tmp_path / "data", execution)
        async for frame in frames:
            yield frame

    app.state.background_tasks.stream = stream
    project_id = app.state.default_project_id
    chat_id = str(uuid4())
    continue_session = succeed and kind in {"discuss", "work", "paper_coach"}
    task_kind = "project_chat" if kind in {"discuss", "work"} else kind
    payload: dict[str, object] = (
        {"message": "Read the graph."} if kind == "paper_coach" else {"run_truth_scope": ["repo-a"]}
    )
    if kind in {"discuss", "work"}:
        payload.update(chat_id=chat_id, mode=kind, message="Read the graph.")
    with signed_in_client(app) as client:
        started = client.post(f"/api/projects/{project_id}/tasks/{task_kind}", json=payload)
        assert started.status_code == 202, started.text
        task = wait_for_task_response(client, project_id, started.json()["operation_id"])
        if continue_session:
            assert task["status"] == "succeeded", task.get("error")
            append_fixture_patch(service, refresh_patch())
            payload["session_id"] = "reader-session"
            started = client.post(f"/api/projects/{project_id}/tasks/{task_kind}", json=payload)
            assert started.status_code == 202, started.text
            task = wait_for_task_response(client, project_id, started.json()["operation_id"])
            assert (
                changed_values(launcher.prompts[-1])["current.graph_changed_since"]
                == f"main@{input_revision}"
            )
    assert task["status"] == ("succeeded" if succeed else "failed"), task.get("error")
    assert launcher.calls == (2 if continue_session else 1)
    if kind == "refresh":
        assert any(
            f"current.graph_changed_since: main@{input_revision}" in master
            for master in launcher.masters
        )
    owner = (
        f"chat:{chat_id}"
        if kind in {"discuss", "work"}
        else "paper"
        if kind == "paper_coach"
        else "graph"
    )
    seen = app.state.background_tasks.store.graph_seen_head(
        project_id, owner, "reader-session", GraphTargetRef()
    )
    assert (seen is not None) == succeed


@pytest.fixture
def seeded_app(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    return app


async def _check_launch(launcher, frames):
    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in frames
    ]
    assert launcher.calls == 1
    assert not [event.text for event in events if event.event == "error"]


@pytest.mark.asyncio
async def test_consolidation_history_launch(seeded_app, tmp_path, history_handlers):
    from .test_consolidation_work import _admit

    service = seeded_app.state.service
    request, execution = _admit(seeded_app)
    launcher = _HistoryLauncher(service, history_handlers, kind="consolidation")
    await _check_launch(
        launcher,
        stream_work_run(
            service, cast(AgentLauncher, launcher), request, tmp_path / "data", execution=execution
        ),
    )


@pytest.mark.asyncio
async def test_experiment_history_launch(seeded_app, tmp_path, history_handlers):
    from rcp.runs.tasks.experiment_loop import stream_experiment_loop_task

    from .test_experiment_loop_agent_io import _execution, _experiment_patch, _loop_request

    service = seeded_app.state.service
    append_fixture_patch(service, _experiment_patch())
    request = _loop_request(str(uuid4()), str(uuid4()), invocation=1)
    execution = _execution(
        seeded_app.state.background_tasks.store,
        seeded_app.state.default_project_id,
        "history-experiment",
        request,
    )
    launcher = _HistoryLauncher(service, history_handlers, kind="experiment")
    await _check_launch(
        launcher,
        stream_experiment_loop_task(
            service, cast(AgentLauncher, launcher), request, tmp_path / "data", execution=execution
        ),
    )


@pytest.mark.asyncio
async def test_child_history_launch(manifest, tmp_path, history_handlers):
    from rcp.runs.tasks.auto_research_child_work import stream_auto_research_child_work_run
    from rcp.service import RunRequest, resolve_dispatch_authority

    from .test_auto_research_children_storage import _work_pair
    from .test_auto_research_stream import _execution, _service, _setup_branch_auto_research

    service, store, episode, actor, _child = _setup_branch_auto_research(
        _service(manifest, tmp_path), tmp_path / "store"
    )
    route, task = _work_pair(store, episode, actor, worker_id=str(uuid4()))
    request = RunRequest.model_validate(
        {
            **task.request,
            "run_on": "laptop",
            "run_truth_scope": ["repo-a"],
            "node_id": "blk/check-result",
            "chat_id": route.worker_id,
            "isolation_owner_episode_id": episode.episode_id,
        }
    )
    route = route.model_copy(update={"control_node_id": request.node_id})
    task = task.model_copy(
        update={
            "request": request.model_dump(mode="json"),
            "dispatch_authority": resolve_dispatch_authority("node_chat", request),
        }
    )
    store.create_auto_research_child_work(route, task)
    launcher = _HistoryLauncher(service, history_handlers, kind="auto_child")
    await _check_launch(
        launcher,
        stream_auto_research_child_work_run(
            service,
            cast(AgentLauncher, launcher),
            request,
            tmp_path / "data",
            _execution(store, task),
            route=route,
        ),
    )


@pytest.mark.parametrize("has_scope", [False, True])
def test_session_master_adds_history_without_an_owner_splice(tmp_path, monkeypatch, has_scope):
    from rcp.agents import prompts
    from rcp.agents.continuation_prompt import LaunchPhase, classify, compose
    from rcp.background import AgentTaskExecution
    from rcp.runs.questions import discuss_command_handler
    from rcp.runs.session_master import continuation_session_master, start_session_master
    from rcp.storage import AppStore

    from .test_auto_research_children_storage import _project
    from .test_session_master import _task

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    stage = tmp_path / "stage"
    stage.mkdir()
    scope = (
        GraphReadScope(
            project_id="project", target=GraphTargetRef(), verbs=frozenset({"graph_log"})
        )
        if has_scope
        else None
    )
    dispatcher = discuss_command_handler(None, graph_read_scope=scope)
    rendered_scopes = []
    original = prompts.render_graph_history

    def capture(rendered_scope, command):
        rendered_scopes.append(rendered_scope)
        assert rendered_scope is dispatcher.graph_read_scope
        return original(rendered_scope, command)

    monkeypatch.setattr(prompts, "render_graph_history", capture)
    values = {"history_client": "client"}
    master = start_session_master(
        cast(AgentTaskExecution, _task(store, "start", stage)),
        local_stage=stage,
        remote_stage=None,
        label_prefix="new-owner",
        key="key",
        content="owner contract",
        values=values,
        graph_read_scope=scope,
        command_client="client",
    )
    content = Path(master.path).read_text()
    assert master.values == values
    assert len(rendered_scopes) == int(has_scope)
    if has_scope:
        assert "`project`" in content
        assert "`main`" in content
        assert "`client graph_log " in content
        assert "`client graph_diff " not in content
    else:
        assert content == "owner contract"

    def unexpected_render():
        pytest.fail("An unchanged session must retain its master")

    kept = continuation_session_master(
        cast(AgentTaskExecution, _task(store, "next", stage)),
        local_stage=stage,
        remote_stage=None,
        native_session_id="session",
        label_prefix="new-owner",
        key="key",
        render=unexpected_render,
        values={"history_client": "next-client"},
        graph_read_scope=scope,
        command_client="next-client",
    )
    assert not kept.bootstrap
    assert kept.values == values
    assert Path(kept.path).read_text() == content
    assert len(rendered_scopes) == int(has_scope)
    continuation = compose(
        classify(LaunchPhase(session_id="session", phase="turn")), parts=[], master=kept, delta=None
    )
    assert "`client graph_log " not in continuation
    assert "`next-client graph_log " not in continuation


def test_embedded_work_and_correction_leave_history_in_the_master():
    from rcp.agents.auto_research_prompt import auto_research_patch_correction_parts

    from .test_prompts import _work_contract

    scope = GraphReadScope(project_id="project", target=GraphTargetRef())
    standalone = _work_contract(graph_read_scope=scope, command_client="client")
    embedded = _work_contract(graph_read_scope=scope, command_client="client", embedded=True)
    correction = "\n".join(
        auto_research_patch_correction_parts(
            actor="worker",
            diagnostics_path="diagnostics.json",
            graph_read_scope=scope,
            command_client="client",
        )
    )
    for verb in scope.verbs:
        assert standalone.count(f"`client {verb} ") == 1
        assert f"`client {verb} " not in embedded
        assert f"`client {verb} " not in correction
