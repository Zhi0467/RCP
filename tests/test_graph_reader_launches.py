from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.tasks.coach import stream_coach
from rcp.runs.tasks.graph import stream_graph_run
from rcp.service import ProjectService

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    create_named_app,
    refresh_patch,
    seed_patch,
    signed_in_client,
    wait_for_task_response,
)


@pytest.mark.parametrize("kind", ["seed", "refresh", "paper_coach"])
@pytest.mark.parametrize("succeed", [True, False])
def test_graph_reader_master_and_committed_observation(
    manifest, tmp_path, monkeypatch, kind, succeed
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.paper.create()
    if kind == "refresh":
        append_fixture_patch(service, seed_patch())
    input_revision = service.history.state().revision

    class Engine:
        def resolve(self, spec, *, default_target):
            return GraphHeadRef(target=spec.target or default_target, revision=spec.revision)

        def steps(self, head, *, include_inherited=False):
            return [
                SimpleNamespace(
                    accepted=True, after_head=GraphHeadRef(revision=patch.revision), patch=patch
                )
                for patch in service.history.current_materialization().patches
                if patch.admission == "accepted" and patch.revision <= head.revision
            ]

    monkeypatch.setattr(
        ProjectService, "history_engine", property(lambda self: Engine()), raising=False
    )
    # Slice D owns dispatch; this launch test never sends a history command.
    history_commands = ModuleType("rcp.runs.graph_history_queries")
    monkeypatch.setattr(
        history_commands,
        "handle_graph_history_command",
        lambda *args: pytest.fail("unexpected command"),
        raising=False,
    )
    monkeypatch.setitem(sys.modules, history_commands.__name__, history_commands)
    masters = []

    class Launcher:
        async def stream(self, provider, prompt, **kwargs):
            root = Path(kwargs["cwd"])
            if kind == "paper_coach":
                root = root.parent
            masters.extend(path.read_text() for path in (root / "inputs").glob("*.md"))
            yield AgentEvent(event="session", session_id="reader-session")
            if not succeed:
                yield AgentEvent(event="error", text="provider failed")
                return
            if kind != "paper_coach":
                patch = seed_patch() if kind == "seed" else refresh_patch()
                (root / "patch.json").write_text(agent_patch_json(patch))
            yield AgentEvent(event="done")

    async def stream(project_id, task_kind, request, execution):
        launcher = cast(AgentLauncher, Launcher())
        frames = (
            stream_coach(service, launcher, service.paper, request, tmp_path / "data", execution)
            if kind == "paper_coach"
            else stream_graph_run(service, launcher, kind, request, tmp_path / "data", execution)
        )
        async for frame in frames:
            yield frame

    app.state.background_tasks.stream = stream
    project_id = app.state.default_project_id
    with signed_in_client(app) as client:
        started = client.post(
            f"/api/projects/{project_id}/tasks/{kind}",
            json={"message": "Read the graph."}
            if kind == "paper_coach"
            else {"run_truth_scope": ["repo-a"]},
        )
        assert started.status_code == 202
        task = wait_for_task_response(client, project_id, started.json()["operation_id"])
    assert task["status"] == ("succeeded" if succeed else "failed")
    assert any("graph_diff" in master and "graph_log" in master for master in masters)
    if kind == "refresh":
        assert any(
            f"current.graph_changed_since: main@{input_revision}" in master for master in masters
        )
    from rcp.core.transition_models import GraphTargetRef

    seen = app.state.background_tasks.store.graph_seen_head(
        project_id,
        "paper" if kind == "paper_coach" else "graph",
        "reader-session",
        GraphTargetRef(),
    )
    assert (seen is not None) == succeed
