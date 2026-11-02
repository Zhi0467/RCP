from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents.write_scope import ProjectWriteScope


def test_memory_roots_survive_scope_serialization_and_change_identity(tmp_path: Path) -> None:
    values = dict(
        project_id="project",
        execution_machine="local",
        execution_host="",
        capability="work_auto",
        stage_root=str(tmp_path),
        workspace_root=str(tmp_path),
        repositories=[],
        protected_write_paths=[],
    )
    plain = ProjectWriteScope.create(**values)
    root = str(tmp_path / "memory" / "project" / "shared")
    scope = ProjectWriteScope.create(**values, memory_roots=[root])
    restored = ProjectWriteScope.model_validate_json(scope.model_dump_json())
    assert root in restored.writable_roots
    assert restored.fingerprint != plain.fingerprint
    old_record = json.loads(plain.model_dump_json())
    old_record.pop("memory_roots")
    assert ProjectWriteScope.model_validate(old_record).fingerprint == plain.fingerprint
    assert (
        plain.fingerprint
        == ProjectWriteScope.model_validate_json(plain.model_dump_json()).fingerprint
    )


def test_discuss_codex_permissions_include_only_supplied_memory(tmp_path: Path) -> None:
    from rcp.providers.codex.profile import _codex_discuss_permission_profile

    root = tmp_path / "memory" / "project" / "shared"
    profile = _codex_discuss_permission_profile(tmp_path / "stage", write_dirs=[root])
    assert tomllib.loads(profile)["permissions"]["rcp_discuss"]["workspace_roots"] == {
        str(tmp_path / "stage"): True,
        str(root): True,
    }


def test_memory_pointer_uses_shared_index_without_staging_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    from rcp.runs import operational_context

    root = tmp_path / "memory" / "project" / "shared"
    monkeypatch.setattr(
        operational_context, "prepare_project_memory", lambda *a, **kw: (str(root), 0)
    )
    execution = SimpleNamespace(
        store=SimpleNamespace(
            consolidation_run_for_operation=lambda _: None,
            auto_research_child_work_for_operation=lambda _: None,
            agent_task=lambda _: SimpleNamespace(
                project_id="project", kind="project_chat", parent_operation_id=None, episode_id=None
            ),
        ),
        operation_id="turn",
    )
    pointer = operational_context.project_memory_pointer(execution, None)
    assert str(root / "MEMORY.md") in pointer
    assert not list(tmp_path.rglob("lessons.md"))


@pytest.mark.parametrize(
    "kind", ["seed", "refresh", "branch_merge", "paper_coach", "artifact_edit"]
)
def test_excluded_runs_do_not_prepare_or_point_to_memory(kind, monkeypatch) -> None:
    from rcp.runs import operational_context

    def refuse(*args, **kwargs):
        raise AssertionError("Excluded runs must not prepare memory")

    monkeypatch.setattr(operational_context, "prepare_project_memory", refuse)
    execution = SimpleNamespace(
        store=SimpleNamespace(agent_task=lambda _: SimpleNamespace(kind=kind)),
        operation_id="turn",
    )
    assert operational_context.project_memory_pointer(execution, None) == ""


@pytest.mark.parametrize(
    ("kind", "eligible"),
    [
        ("node_chat", True),
        ("project_chat", True),
        ("auto_research", True),
        ("seed", False),
        ("refresh", False),
        ("branch_merge", False),
        ("paper_coach", False),
        ("artifact_edit", False),
    ],
)
def test_launch_scope_only_admits_memory_for_eligible_runs(kind, eligible, tmp_path, monkeypatch):
    from rcp.agents import memory_files
    from rcp.runs import chat

    root = str(tmp_path / "memory" / "project" / "shared")
    prepared = []

    def prepare(store, project_id, remote_stage):
        prepared.append(project_id)
        return root, 0

    monkeypatch.setattr(memory_files, "prepare_project_memory", prepare)
    monkeypatch.setattr(chat, "resolve_project_write_scope", lambda **kwargs: kwargs)
    monkeypatch.setattr(chat, "write_repositories", lambda state: [])
    monkeypatch.setattr(chat, "_machine_writable_paths", lambda *args: [])
    task = SimpleNamespace(kind=kind, project_id="project", episode_id=None, request={})
    execution = SimpleNamespace(
        store=SimpleNamespace(agent_task=lambda _: task), operation_id="turn"
    )
    service = SimpleNamespace(
        history=SimpleNamespace(state=lambda: None),
        manifest=None,
        repository_ownership_inventory=lambda **kwargs: [],
    )
    result = chat._project_write_scope(
        SimpleNamespace(repositories=[]),
        service,
        "local",
        workspace=tmp_path / "stage",
        remote_stage=None,
        data_dir=tmp_path,
        execution=execution,
        capability="work_auto",
        stage_only=True,
    )
    assert result["memory_roots"] == ([root] if eligible else [])
    assert prepared == (["project"] if eligible else [])


@pytest.mark.parametrize("oversized", [False, True])
def test_launch_stats_the_live_index_and_reports_its_bound(tmp_path, monkeypatch, oversized):
    from rcp.limits import MEMORY_INDEX_MAX_BYTES
    from rcp.runs import operational_context

    root = str(tmp_path / "shared")
    monkeypatch.setattr(
        operational_context,
        "prepare_project_memory",
        lambda *args: (root, MEMORY_INDEX_MAX_BYTES + int(oversized)),
    )
    execution = SimpleNamespace(
        store=SimpleNamespace(
            agent_task=lambda _: SimpleNamespace(
                kind="node_chat", project_id="project", episode_id="experiment"
            ),
            consolidation_run_for_operation=lambda _: None,
        ),
        operation_id="turn",
    )
    pointer = operational_context.project_memory_pointer(execution, None)
    assert (str(MEMORY_INDEX_MAX_BYTES) in pointer) is oversized
    assert root + "/MEMORY.md" in pointer


def test_launch_withholds_a_redirected_shared_folder(tmp_path, monkeypatch):
    import shutil

    from rcp.agents.memory_files import prepare_project_memory
    from rcp.runs import chat
    from rcp.storage import AppStore

    store = AppStore(tmp_path / "rcp.sqlite3")
    shared, _size = prepare_project_memory(store, "project")
    shutil.rmtree(shared)
    (tmp_path / "outside" / "notes").mkdir(parents=True)
    Path(shared).symlink_to(tmp_path / "outside", target_is_directory=True)
    monkeypatch.setattr(
        store,
        "agent_task",
        lambda _: SimpleNamespace(
            kind="project_chat", project_id="project", episode_id=None, request={}
        ),
    )
    monkeypatch.setattr(chat, "resolve_project_write_scope", lambda **kwargs: kwargs)
    monkeypatch.setattr(chat, "write_repositories", lambda state: [])
    monkeypatch.setattr(chat, "_machine_writable_paths", lambda *args: [])
    service = SimpleNamespace(
        history=SimpleNamespace(state=lambda: None),
        manifest=None,
        repository_ownership_inventory=lambda **kwargs: [],
    )
    result = chat._project_write_scope(
        SimpleNamespace(repositories=[]),
        service,
        "local",
        workspace=tmp_path / "stage",
        remote_stage=None,
        data_dir=tmp_path,
        execution=SimpleNamespace(store=store, operation_id="turn"),
        capability="work_auto",
        stage_only=True,
    )
    assert result["memory_roots"] == []


@pytest.mark.parametrize(
    ("chat_id", "episode_id", "remote", "member_root"),
    [
        ("puck", None, False, True),
        ("shared-chat", None, False, False),
        ("puck", "experiment", False, False),
        ("puck", None, True, False),
    ],
)
def test_only_a_backend_private_thread_turn_adds_its_owner_notes(
    chat_id, episode_id, remote, member_root, tmp_path, monkeypatch
):
    from rcp.agents import memory_files
    from rcp.runs import chat
    from rcp.storage.agent_threads import AgentThreadRecord

    shared = str(tmp_path / "memory" / "project" / "shared")
    monkeypatch.setattr(memory_files, "prepare_project_memory", lambda *args: (shared, 0))
    monkeypatch.setattr(chat, "resolve_project_write_scope", lambda **kwargs: kwargs)
    monkeypatch.setattr(chat, "write_repositories", lambda state: [])
    monkeypatch.setattr(chat, "_machine_writable_paths", lambda *args: [])
    thread = AgentThreadRecord(
        thread_id="puck", user_id="alice", kind="project", project_id="project", created_at="now"
    )
    task = SimpleNamespace(
        kind="project_chat",
        project_id="project",
        episode_id=episode_id,
        request={"chat_id": chat_id},
    )
    store = SimpleNamespace(
        path=tmp_path / "rcp.sqlite3",
        agent_task=lambda _: task,
        agent_thread=lambda thread_id: thread if thread_id == "puck" else None,
    )
    service = SimpleNamespace(
        history=SimpleNamespace(state=lambda: None),
        manifest=None,
        repository_ownership_inventory=lambda **kwargs: [],
    )
    stage = SimpleNamespace(root=tmp_path / "remote-stage") if remote else None
    result = chat._project_write_scope(
        SimpleNamespace(repositories=[]),
        service,
        "local",
        workspace=tmp_path / "stage",
        remote_stage=stage,
        data_dir=tmp_path,
        execution=SimpleNamespace(store=store, operation_id="turn"),
        capability="work_auto",
        stage_only=True,
    )
    notes = tmp_path.resolve() / "memory" / "members" / "alice"
    # Project shared memory stays; the owner's notes join only a backend private thread turn.
    assert result["memory_roots"] == [shared]
    assert result["member_memory_root"] == (str(notes) if member_root else None)
    assert (notes / "MEMORY.md").is_file() is member_root


def test_member_notes_are_labelled_apart_from_project_memory(tmp_path):
    from rcp.agents.prompts import write_scope_section

    shared, notes = str(tmp_path / "shared"), str(tmp_path / "members" / "alice")
    scope = ProjectWriteScope.create(
        project_id="project",
        execution_machine="local",
        execution_host="",
        capability="work_auto",
        stage_root=str(tmp_path),
        workspace_root=str(tmp_path),
        repositories=[],
        protected_write_paths=[],
        memory_roots=[shared],
        member_memory_root=notes,
    )
    restored = ProjectWriteScope.model_validate_json(scope.model_dump_json())
    assert restored.memory_roots == sorted([shared, notes])
    assert notes in restored.writable_roots
    section = write_scope_section(restored)
    assert section.count(shared) == 1 and section.count(notes) == 1
