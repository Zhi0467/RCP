from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rcp.api.dependencies import get_graph_service
from rcp.history import HistoryManager
from rcp.paper import PaperService
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.transport import StateUnavailable
from tests.helpers import seed_patch
from tests.test_branch_history import _branch_metadata, _branch_patch


@pytest.fixture
def branch_service(manifest, tmp_path):
    history = HistoryManager(manifest)
    history.append(seed_patch())
    metadata = _branch_metadata(history)
    branch = history.create_auto_research_branch(metadata)
    branch.append(_branch_patch("ev/branch-only"))
    history.append(_branch_patch("ev/main-only"))
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
        project_id=metadata.project_id,
    )
    return service, metadata


def test_repeated_read_resolution_never_repairs_or_publishes(branch_service, monkeypatch):
    service, metadata = branch_service
    catalog = SimpleNamespace(open=lambda _project_id: service)
    root = service.history.root
    (root / "branches" / metadata.branch_id / "graph.json").unlink()
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    workspace = service.history.workspace
    refresh_if_stale = workspace.refresh_if_stale
    refreshes = 0

    def counted_refresh():
        nonlocal refreshes
        refreshes += 1
        return refresh_if_stale()

    def forbidden_mutation(*_args, **_kwargs):
        raise AssertionError("read resolution must not repair, transact, or publish")

    monkeypatch.setattr(workspace, "refresh_if_stale", counted_refresh)
    monkeypatch.setattr(workspace, "transaction", forbidden_mutation)
    monkeypatch.setattr(workspace, "publish", forbidden_mutation)
    monkeypatch.setattr(service.history, "current_materialization", forbidden_mutation)

    for _ in range(2):
        resolved = get_graph_service(
            catalog, metadata.project_id, metadata.branch_id, initialize=False
        )
        state = resolved.history.materialize(write_outputs=False).state
        assert resolved.history.graph_target == metadata.head.target
        assert "ev/branch-only" in state.nodes
        assert "ev/main-only" not in state.nodes

    assert refreshes == 2
    assert before == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_default_resolution_still_initializes_branch_outputs(branch_service):
    service, metadata = branch_service
    catalog = SimpleNamespace(open=lambda _project_id: service)
    graph_path = service.history.root / "branches" / metadata.branch_id / "graph.json"
    graph_path.unlink()

    get_graph_service(catalog, metadata.project_id, metadata.branch_id, initialize=False)
    assert not graph_path.exists()

    get_graph_service(catalog, metadata.project_id, metadata.branch_id)
    assert "ev/branch-only" in json.loads(graph_path.read_text())["nodes"]


def test_read_resolution_refreshes_before_reading_metadata(branch_service, monkeypatch):
    service, metadata = branch_service
    metadata_path = service.history.root / "branches" / metadata.branch_id / "branch.json"
    current = metadata_path.read_bytes()
    metadata_path.write_text("stale incomplete remote metadata")

    def refresh_snapshot():
        metadata_path.write_bytes(current)
        return True

    monkeypatch.setattr(service.history.workspace, "refresh_if_stale", refresh_snapshot)
    branch = service.history.branch(metadata.branch_id, initialize=False)
    assert branch.graph_target == metadata.head.target


@pytest.mark.parametrize("raises", [False, True])
def test_unavailable_read_refresh_is_explicit(branch_service, monkeypatch, raises):
    service, metadata = branch_service
    catalog = SimpleNamespace(open=lambda _project_id: service)
    metadata_path = service.history.root / "branches" / metadata.branch_id / "branch.json"
    metadata_path.write_text("stale incomplete remote metadata")

    def unavailable_refresh():
        if raises:
            raise StateUnavailable("canonical state is unreachable")
        return False

    monkeypatch.setattr(service.history.workspace, "refresh_if_stale", unavailable_refresh)
    with pytest.raises(StateUnavailable, match="canonical state"):
        get_graph_service(catalog, metadata.project_id, metadata.branch_id, initialize=False)


@pytest.mark.parametrize("foreign", ["project", "branch", "malformed"])
def test_read_resolution_refuses_foreign_and_malformed_targets(branch_service, foreign):
    service, metadata = branch_service
    catalog = SimpleNamespace(open=lambda _project_id: service)
    metadata_path = service.history.root / "branches" / metadata.branch_id / "branch.json"
    document = json.loads(metadata_path.read_text())
    if foreign == "project":
        document["project_id"] = "another-project"
    elif foreign == "branch":
        other_id = str(uuid.uuid4())
        document["branch_id"] = document["episode_id"] = other_id
        document["head"]["target"]["branch_id"] = other_id
    else:
        document["unexpected"] = True
    metadata_path.write_text(json.dumps(document))

    with pytest.raises(HTTPException) as error:
        get_graph_service(catalog, metadata.project_id, metadata.branch_id, initialize=False)
    assert error.value.status_code == 404


def test_read_resolution_refuses_wrong_episode_and_unsafe_paths(branch_service, tmp_path):
    service, metadata = branch_service
    with pytest.raises(ValueError, match="different episode"):
        service.history.branch(
            metadata.branch_id, expected_episode_id=str(uuid.uuid4()), initialize=False
        )
    with pytest.raises(ValueError, match="canonical episode UUIDv4"):
        service.history.branch("../patches", initialize=False)

    symlink_id = str(uuid.uuid4())
    outside = tmp_path / "outside"
    outside.mkdir()
    (service.history.root / "branches" / symlink_id).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="not a regular directory"):
        service.history.branch(symlink_id, initialize=False)
