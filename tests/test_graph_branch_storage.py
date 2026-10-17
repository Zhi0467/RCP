from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import uuid
from contextlib import chdir
from pathlib import Path

import pytest

from rcp.config import load_manifest
from rcp.core.models import BranchMergeProvenance, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history import HistoryManager
from rcp.runs.branch_merge import branch_merge_id
from rcp.storage import AppStore
from rcp.storage.branch_names import BranchNameTaken
from tests.helpers import unchecked_write_authority
from tests.test_episode_storage import _episode


def test_branch_identity_ownership_names_and_archive_admission(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    branch_id = str(uuid.uuid4())
    episode = _episode(store, "root").model_copy(
        update={
            "graph_target": GraphTargetRef(kind="branch", branch_id=branch_id),
            "graph_base_head": GraphHeadRef(revision=2),
        }
    )
    store.create_episode(episode)
    continuation = episode.model_copy(
        update={
            "episode_id": "next",
            "continues_episode_id": "root",
            "control_node_id": "exp/next",
        }
    )
    store.create_episode(continuation)
    child = store.create_graph_branch(
        project_id="project",
        branch_id=str(uuid.uuid4()),
        owner_episode_id="next",
        parent_head=GraphHeadRef(target=episode.graph_target, revision=3),
        created_by="orchestrator:operation",
        name="nested-fork",
    )
    assert child.owner_episode_id == store.branch_owner("project", branch_id) == "root"
    assert {row.branch_id for row in store.branches_owned_by("project", "next")} == {
        branch_id,
        child.branch_id,
    }
    assert (
        store.create_graph_branch(
            project_id="project",
            branch_id=child.branch_id,
            owner_episode_id="next",
            parent_head=child.parent_head,
            created_by=child.created_by,
            name=child.name,
        )
        == child
    )
    store.rename_branch("project", child.branch_id, "renamed-fork")
    assert store.branch_owner("project", child.branch_id) == "root"
    with pytest.raises(BranchNameTaken):
        store.create_graph_branch(
            project_id="project",
            branch_id=str(uuid.uuid4()),
            owner_episode_id=None,
            parent_head=GraphHeadRef(revision=0),
            created_by="human:user",
            name=child.name,
        )
    store.set_branch_archived("project", child.branch_id, archived=True)
    assert not store.branch_admits_new_work("project", child.branch_id)
    assert store.branch_admits_new_work("project", branch_id)
    with store.connection() as connection, pytest.raises(ValueError, match="graph_branch_archived"):
        store.require_episode_binding_admission_open(
            connection, "project", branch_id=child.branch_id
        )
    store.set_branch_archived("project", branch_id, archived=True)
    with store.connection() as connection, pytest.raises(ValueError, match="graph_branch_archived"):
        store.require_episode_binding_admission_open(connection, "project", owner_episode_id="root")
    store.set_branch_archived("project", child.branch_id, archived=False)
    assert store.branch_admits_new_work("project", child.branch_id)
    with store.connection() as connection:
        store.require_episode_binding_admission_open(
            connection, "project", branch_id=child.branch_id, owner_episode_id="root"
        )


def test_pre55_legacy_branch_retains_replay_names_archive_and_merge(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures/server_upgrade/pre-graph-branches-v28-ae89ee3"
    copied = tmp_path / "fixture"
    shutil.copytree(source, copied)
    fixture = json.loads((copied / "fixture.json").read_text())
    database = copied / "data/rcp.sqlite3"
    database.write_bytes(gzip.decompress(database.with_suffix(".sqlite3.gz").read_bytes()))
    branch_id, project_id = fixture["legacy_branch_id"], fixture["project_id"]
    metadata_path = copied / "project/.research/branches" / branch_id / "branch.json"
    original_metadata = metadata_path.read_bytes()
    assert "parent_head" not in json.loads(original_metadata)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT MAX(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            == 54
        )
    with chdir(copied):
        store = AppStore(database)
        record = store.graph_branch(project_id, branch_id)
        assert record is not None and record.owner_episode_id == branch_id and record.archived
        assert store.branch_names(project_id)[branch_id] == fixture["legacy_branch_name"]
        assert not store.branch_admits_new_work(project_id, branch_id)
        with pytest.raises(BranchNameTaken):
            store.create_graph_branch(
                project_id=project_id,
                branch_id=str(uuid.uuid4()),
                owner_episode_id=None,
                parent_head=record.parent_head,
                created_by="human:fixture",
                name=fixture["legacy_branch_retired_name"],
            )
        project = store.project(project_id)
        assert project is not None
        history = HistoryManager(
            load_manifest(project.locator),
            project_id=project_id,
            expected_space_id=store.space_id,
            write_authority=unchecked_write_authority(),
        )
        branch = history.branch(branch_id)
        assert "ev/legacy-branch" in branch.state().nodes
        assert "ev/legacy-branch" not in history.state().nodes
        assert metadata_path.read_bytes() == original_metadata
        metadata = branch.branch_metadata()
        provenance = BranchMergeProvenance(
            merge_id=branch_merge_id(metadata),
            branch_id=branch_id,
            episode_id=branch_id,
            branch_base_head=metadata.base_head,
            branch_head=branch.head_ref(),
            rebased_main_head=history.head_ref(),
            merge_task_id="legacy-merge",
        )
        saved_patch = Patch.model_validate_json(next(branch.patches_dir.glob("*.json")).read_text())
        history.append(
            saved_patch.model_copy(
                update={
                    "revision": 0,
                    "transition": None,
                    "profile": "orchestrator",
                    "task_id": "legacy-merge",
                    "episode_id": branch_id,
                    "authorized_by": metadata.authorized_by,
                    "branch_merge": provenance,
                }
            )
        )
        assert "ev/legacy-branch" in history.state().nodes
        receipt = branch.reconcile_merge_receipt(provenance.merge_id)
        assert receipt is not None and receipt.outcome == "committed"
        assert metadata_path.read_bytes() == original_metadata
