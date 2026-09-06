from __future__ import annotations

import asyncio
import json
import re
import shlex
import uuid
from contextlib import nullcontext
from pathlib import Path

from rcp.agents import AgentEvent
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.config import Manifest
from rcp.core.models import GraphState
from rcp.runs.auto_research_experiments import AutoResearchExperimentCoordinator
from rcp.runs.tasks.experiment_loop import stream_experiment_loop_task
from rcp.runs.tasks.work import _apply_work_patch
from rcp.service import RunRequest

from .helpers import agent_patch_json, wait_for_task
from .test_auto_research_experiments import EXPERIMENT_ID, _admit, _experiment_patch
from .test_auto_research_stream import _service, _setup_branch_auto_research


class _CompletingExperimentProvider:
    """Replace only the external model; consume its actual file-backed contract."""

    def __init__(self) -> None:
        self.graphs: list[GraphState] = []
        self.validation_results: list[dict[str, object]] = []

    async def stream(self, _provider, prompt, **kwargs):
        contract = Path(prompt.splitlines()[1]).read_text(encoding="utf-8")
        pointer = re.search(
            r"Current graph, including the Experiment's attempts: `([^`]+)`", contract
        )
        assert pointer is not None
        graph = GraphState.model_validate_json(Path(pointer.group(1)).read_text(encoding="utf-8"))
        self.graphs.append(graph)
        assert graph.nodes[EXPERIMENT_ID].type == "experiment"
        (Path(kwargs["cwd"]) / "patch.json").write_text(
            json.dumps(
                {
                    "summary": "Verified the assigned branch Experiment and finished the check.",
                    "ops": [
                        {
                            "op": "update_nodes",
                            "nodes": [{"id": EXPERIMENT_ID, "changes": {"status": "completed"}}],
                        }
                    ],
                    "repositories_read": [],
                    "change_summary": ["Completed the branch-only Experiment."],
                }
            ),
            encoding="utf-8",
        )
        (Path(kwargs["cwd"]) / "watch.json").write_text(
            '{"external": [], "graph": []}', encoding="utf-8"
        )
        validator = re.search(r"run this exact command: `([^`]+)`", contract)
        assert validator is not None
        process = await asyncio.create_subprocess_exec(
            *shlex.split(validator.group(1)),
            cwd=kwargs["cwd"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, (stdout.decode(), stderr.decode())
        self.validation_results.append(json.loads(stdout))
        yield AgentEvent(event="session", session_id="child-experiment-session")
        yield AgentEvent(event="answer", text="The assigned branch Experiment is complete.")
        yield AgentEvent(event="done")


def test_dispatched_child_reads_and_updates_its_parent_branch_with_child_provenance(
    manifest: Manifest,
    tmp_path: Path,
) -> None:
    main = _service(manifest, tmp_path)
    branch, store, parent, root, _worker = _setup_branch_auto_research(main, tmp_path / "store")
    main_head = main.history.head_ref()
    main_graph = (manifest.research_dir / "graph.json").read_bytes()
    # Exercise the same attributable branch Apply used by the orchestrator to
    # create an Experiment that is deliberately absent from main.
    created, failure = _apply_work_patch(
        branch,
        None,
        agent_patch_json(_experiment_patch()),
        run_truth_scope=["repo-a"],
        profile="orchestrator",
        source_operation_id=root.operation_id,
    )
    assert failure is None
    assert created is not None and created.status == "applied"
    assert EXPERIMENT_ID not in main.history.state().nodes
    branch_revision = branch.history.state().revision
    provider = _CompletingExperimentProvider()

    async def stream(project_id: str, kind: str, request: object, execution: AgentTaskExecution):
        assert project_id == parent.project_id
        assert kind == "node_chat"
        assert isinstance(request, RunRequest)
        target = store.agent_task(execution.operation_id).graph_target
        service = main.for_graph_target(target, expected_episode_id=parent.episode_id)
        async for frame in stream_experiment_loop_task(
            service, provider, request, tmp_path / "run-data", execution=execution
        ):
            yield frame

    background = BackgroundAgentTasks(store, stream)
    coordinator = AutoResearchExperimentCoordinator(
        store,
        background,
        project_service=lambda _project_id, episode_id: main.for_graph_target(
            parent.graph_target, expected_episode_id=episode_id
        ),
        operation_lock=lambda _project_id: nullcontext(),
    )
    child_id = str(uuid.uuid4())
    admission_id = str(uuid.uuid4())
    _admit(
        store,
        parent_episode_id=parent.episode_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )
    try:
        action = coordinator.kick_off(
            auto_research_episode_id=parent.episode_id,
            parent_operation_id=root.operation_id,
            child_episode_id=child_id,
            node_id=EXPERIMENT_ID,
            goal=None,
            goal_sha256=None,
            invocation_limit=None,
            admission_id=admission_id,
        )
        assert action.disposition == "created"
        assert action.operation_id is not None
        task = wait_for_task(store, action.operation_id, expect="succeeded")
        assert task.result["graph_update"]["status"] == "applied"
        assert task.graph_target == parent.graph_target
        assert task.episode_id == child_id != parent.episode_id
        assert len(provider.graphs) == 1
        assert provider.graphs[0].revision == branch_revision
        assert provider.validation_results[0]["status"] == "valid"
        applied = branch.history.load_patches()[-1]
        assert applied.kind == "experiment_loop"
        assert applied.task_id == task.operation_id
        assert applied.episode_id == child_id
        assert applied.authorized_by == parent.authorized_by
        child = store.experiment_episode(child_id)
        assert child is not None
        assert child.last_graph_result.startswith("applied")
        ending = store.experiment_episode_ending_signal(child_id)
        assert ending is not None
        assert ending[0] == task.operation_id
        assert ending[1]["ending"] == "completed"
        assert branch.history.state().nodes[EXPERIMENT_ID].status == "completed"
        assert main.history.head_ref() == main_head
        assert (manifest.research_dir / "graph.json").read_bytes() == main_graph
    finally:
        background.shutdown()
