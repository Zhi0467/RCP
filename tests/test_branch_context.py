from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcp.core.models import GraphState
from rcp.core.research_md import render_research_md
from rcp.history import HistoryManager
from rcp.paper import PaperService
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.shared import _stage_context_paths
from rcp.runs.tasks.auto_research_stream import (
    _auto_research_context,
    _refreshed_orchestrator_state_paths,
    _WorkerStage,
)
from rcp.service import ProjectService, RunRequest
from rcp.storage import AppStore
from tests.helpers import seed_patch
from tests.test_branch_history import _branch_metadata, _branch_patch


@pytest.fixture
def branch_services(manifest, tmp_path):
    history = HistoryManager(manifest)
    history.append(seed_patch())
    metadata = _branch_metadata(history)
    branch = history.create_auto_research_branch(metadata)
    branch.append(_branch_patch("ev/branch-only"))
    history.append(_branch_patch("ev/main-only", "Main-only result"))
    paper = PaperService(manifest, AppStore(tmp_path / "app.sqlite3"))
    paper.canonical_path.parent.mkdir(exist_ok=True)
    paper.canonical_path.write_text("Human paper introduction.\n")
    service = ProjectService(manifest, history, paper, project_id="project")
    return service, service.for_graph_target(branch.graph_target)


def test_chat_context_reads_exact_materialized_target_and_shared_inputs(branch_services):
    main, branch = branch_services
    for service, included, excluded in (
        (main, "ev/main-only", "ev/branch-only"),
        (branch, "ev/branch-only", "ev/main-only"),
    ):
        context = service.assemble_chat(RunRequest(node_id=included))
        graph = GraphState.model_validate_json(Path(context.graph_path).read_text())
        assert included in graph.nodes
        assert excluded not in graph.nodes
        assert graph.revision == context.graph_revision
        assert context.node == graph.nodes[included].model_dump(mode="json")
        assert Path(context.research_md_path).read_text() == render_research_md(graph)
        assert json.loads(Path(context.glossary_path).read_text()) == {
            term: item.model_dump(mode="json") for term, item in graph.glossary.items()
        }
        assert "coverage_path" not in context.model_dump()
        for field in ("graph_path", "research_md_path", "glossary_path"):
            assert Path(getattr(context, field)).parent == service.history.root
        assert context.introduction_path == str(main.paper.canonical_path)
        assert Path(context.introduction_path).read_text() == "Human paper introduction.\n"
        assert context.facts_dir == str(main.history.root / "facts")


def test_orchestrator_context_and_refresh_keep_branch_pointer(branch_services, tmp_path):
    main, branch = branch_services
    request = AutoResearchRunRequest(
        episode_id=branch.history.branch_id,
        role="orchestrator",
        run_on="laptop",
    )
    stage = _WorkerStage(
        local=tmp_path,
        remote=None,
        workspace=tmp_path,
        execution_host="",
        provider_binary=None,
    )
    context = _auto_research_context(branch, request, stage)
    assert "ev/branch-only" in json.loads(Path(context.graph_path).read_text())["nodes"]
    assert Path(context.graph_path).parent == branch.history.root

    branch.history.append(_branch_patch("ev/later-branch-result"))
    revision, graph_path, research_path = _refreshed_orchestrator_state_paths(
        branch, request, stage
    )
    graph = GraphState.model_validate_json(Path(graph_path).read_text())
    assert graph_path == context.graph_path
    assert graph.revision == revision == context.graph_revision + 1
    assert "ev/later-branch-result" in graph.nodes
    assert "ev/main-only" not in graph.nodes
    assert Path(research_path).read_text() == render_research_md(graph)
    assert "ev/later-branch-result" not in main.history.state().nodes


@pytest.mark.parametrize("target", ["main", "branch"])
def test_remote_context_preserves_target_namespace_and_shared_paper_path(branch_services, target):
    service = branch_services[0 if target == "main" else 1]
    context = service.assemble_chat(RunRequest(chat_scope="project"))
    # The local cache remains the source of relative paths; the execution host
    # opens the corresponding files in the remote canonical repository.
    service.manifest.repository_map["repo-a"].path = "/srv/research-project"
    updates = _stage_context_paths(context, service, None, "laptop")  # type: ignore[arg-type]
    canonical = Path("/srv/research-project/.research")
    graph_root = canonical
    if target == "branch":
        graph_root /= Path("branches") / service.history.branch_id
    for field, filename in (
        ("graph_path", "graph.json"),
        ("research_md_path", "research.md"),
        ("glossary_path", "glossary.json"),
    ):
        assert updates[field] == str(graph_root / filename)
    assert updates["introduction_path"] == str(canonical / "paper" / "introduction.md")
    assert updates["facts_dir"] == str(canonical / "facts")
