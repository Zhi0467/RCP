from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from rcp.agents.context import RepositoryPointer
from rcp.agents.write_scope import (
    ProjectWriteScope,
    RegisteredRepositoryRoot,
    registered_repository_roots,
    resolve_project_write_scope,
)
from rcp.config import Manifest
from rcp.core.models import ConversationWorktreeBinding
from rcp.storage import AgentTaskRecord, AppStore, ProjectRecord


def _binding(manifest: Manifest, tmp_path: Path) -> ConversationWorktreeBinding:
    worktree = tmp_path / "conversation-worktree"
    worktree.mkdir(exist_ok=True)
    metadata = Path(manifest.repository_map["repo-a"].path) / ".git"
    metadata.mkdir(exist_ok=True)
    return ConversationWorktreeBinding(
        project_id="project",
        chat_id=str(uuid.uuid4()),
        chat_scope="project",
        repository_alias="repo-a",
        machine="laptop",
        execution_host="",
        shared_path=str(Path(manifest.repository_map["repo-a"].path).resolve()),
        worktree_path=str(worktree.resolve()),
        git_common_dir=str(metadata.resolve()),
        branch="rcp/chat-fixture",
        starting_branch="trunk",
        starting_commit="a" * 40,
        status="ready",
    )


def _resolve(
    manifest: Manifest,
    tmp_path: Path,
    binding: ConversationWorktreeBinding | None,
    *,
    pointer_path: str | None = None,
    aliases: list[str] | None = None,
    inventory: list[RegisteredRepositoryRoot] | None = None,
    include_shared_checkout: bool = False,
    capability: str = "work_auto",
    app_data_dir: Path | None = None,
):
    stage = tmp_path / "stage"
    workspace = stage / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return resolve_project_write_scope(
        manifest=manifest,
        project_id="project",
        execution_machine="laptop",
        capability=capability,
        stage_root=str(stage),
        workspace_root=str(workspace),
        admitted_aliases=aliases if aliases is not None else ["repo-a"],
        repository_pointers=[
            RepositoryPointer(
                alias="repo-a",
                machine="laptop",
                path=pointer_path or binding.worktree_path,
            )
        ],
        remote_stage=None,
        app_data_dir=app_data_dir or tmp_path / "data",
        repository_inventory=(
            registered_repository_roots(manifest, project_id="project")
            if inventory is None
            else inventory
        ),
        conversation_worktree=binding,
        include_shared_checkout=include_shared_checkout,
    )


def test_bound_scope_admits_worktree_and_only_merge_adds_shared_checkout(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    ordinary = _resolve(manifest, tmp_path, binding)
    integration = _resolve(manifest, tmp_path, binding, include_shared_checkout=True)
    assert ordinary.repository_roots == [binding.worktree_path]
    assert ordinary.git_metadata_roots == [binding.git_common_dir]
    assert integration.git_metadata_roots == ordinary.git_metadata_roots
    assert set(integration.repository_roots) == {binding.shared_path, binding.worktree_path}
    assert ordinary.fingerprint != integration.fingerprint
    for scope in [ordinary, integration]:
        assert f"{binding.worktree_path}/.research" in scope.protected_write_paths
        assert f"{binding.shared_path}/.research" in scope.protected_write_paths


def test_real_worktree_git_commit_uses_exact_common_metadata_outside_worktree(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    shared = Path(binding.shared_path)
    worktree = Path(binding.worktree_path)

    def git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git(shared, "init", "--initial-branch=trunk")
    git(shared, "config", "user.name", "Scope fixture")
    git(shared, "config", "user.email", "scope@example.invalid")
    (shared / "scope-notes.txt").write_text("shared\n")
    git(shared, "add", "scope-notes.txt")
    git(shared, "commit", "-m", "fixture base")
    starting_commit = git(shared, "rev-parse", "HEAD")
    worktree.rmdir()
    git(shared, "worktree", "add", "-b", binding.branch, str(worktree), starting_commit)
    metadata = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    binding = binding.model_copy(update={"starting_commit": starting_commit})
    scope = _resolve(manifest, tmp_path, binding)
    assert (worktree / ".git").is_file()
    assert scope.git_metadata_roots == [metadata]
    assert scope.writable_roots == [
        str((tmp_path / "stage" / "workspace").resolve()),
        str(worktree),
        metadata,
    ]
    assert str(shared) not in scope.writable_roots
    (worktree / "scope-notes.txt").write_text("worktree\n")
    git(worktree, "add", "scope-notes.txt")
    git(worktree, "commit", "-m", "fixture worktree edit")
    assert git(shared, "rev-parse", binding.branch) == git(worktree, "rev-parse", "HEAD")
    assert (shared / "scope-notes.txt").read_text() == "shared\n"


def test_legacy_scope_fingerprint_omits_empty_git_metadata_roots() -> None:
    legacy = {
        "schema_generation": 1,
        "project_id": "project",
        "execution_machine": "local",
        "execution_host": "",
        "capability": "work_auto",
        "stage_root": "/stage",
        "workspace_root": "/stage/workspace",
        "repositories": [],
        "protected_write_paths": ["/stage/inputs"],
    }
    digest = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    loaded = ProjectWriteScope.model_validate({**legacy, "fingerprint": digest})
    created = ProjectWriteScope.create(
        **{k: v for k, v in legacy.items() if k != "schema_generation"}
    )
    assert loaded.fingerprint == created.fingerprint == digest
    assert loaded.git_metadata_roots == []
    assert ProjectWriteScope.model_validate(loaded.model_dump()).fingerprint == digest


def test_git_metadata_root_retargeting_and_foreign_ownership_fail_closed(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    metadata = Path(binding.git_common_dir)
    metadata.rmdir()
    with pytest.raises(ValueError, match="unavailable"):
        _resolve(manifest, tmp_path, binding)
    foreign_path = tmp_path / "foreign-metadata"
    foreign_path.mkdir()
    metadata.symlink_to(foreign_path, target_is_directory=True)
    with pytest.raises(ValueError, match="Git metadata moved or its path was retargeted"):
        _resolve(manifest, tmp_path, binding)
    binding = binding.model_copy(update={"git_common_dir": str(foreign_path.resolve())})
    inventory = registered_repository_roots(manifest, project_id="project")
    inventory.append(
        RegisteredRepositoryRoot(
            project_id="foreign",
            alias="foreign",
            machine="local",
            execution_host="",
            path=str(foreign_path),
        )
    )
    with pytest.raises(ValueError, match="overlaps another project"):
        _resolve(manifest, tmp_path, binding, inventory=inventory)


def test_unbound_scope_cannot_admit_arbitrary_pointer_or_merge_exception(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    with pytest.raises(ValueError, match="registered project root"):
        _resolve(manifest, tmp_path, None, pointer_path=binding.worktree_path)
    with pytest.raises(ValueError, match="requires a conversation worktree binding"):
        _resolve(
            manifest,
            tmp_path,
            None,
            pointer_path=binding.worktree_path,
            include_shared_checkout=True,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"project_id": "foreign"}, "different project or execution host"),
        ({"machine": "elsewhere"}, "different project or execution host"),
        ({"execution_host": "somewhere"}, "different project or execution host"),
        ({"status": "removed"}, "not ready"),
        ({"status": "creating"}, "not ready"),
        ({"repository_alias": "repo-b"}, "exact single repository"),
    ],
)
def test_bound_scope_refuses_changed_identity(
    manifest: Manifest, tmp_path: Path, changes: dict, message: str
) -> None:
    binding = _binding(manifest, tmp_path).model_copy(update=changes)
    with pytest.raises(ValueError, match=message):
        _resolve(manifest, tmp_path, binding)


def test_bound_scope_refuses_scope_changes_and_orchestrator(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    with pytest.raises(ValueError, match="exact single repository"):
        _resolve(manifest, tmp_path, binding, aliases=["repo-a", "repo-b"])
    with pytest.raises(ValueError, match="ordinary Work"):
        _resolve(manifest, tmp_path, binding, capability="orchestrate")
    with pytest.raises(ValueError, match="pointer does not match"):
        _resolve(manifest, tmp_path, binding, pointer_path=binding.shared_path)
    relocated = manifest.model_copy(deep=True)
    relocated.repository_map["repo-a"].path = manifest.repository_map["repo-b"].path
    with pytest.raises(ValueError, match="registered shared root"):
        _resolve(relocated, tmp_path, binding)


def test_bound_scope_fails_closed_for_missing_or_retargeted_worktree(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    worktree = Path(binding.worktree_path)
    worktree.rmdir()
    with pytest.raises(ValueError, match="unavailable"):
        _resolve(manifest, tmp_path, binding)
    worktree.symlink_to(binding.shared_path, target_is_directory=True)
    with pytest.raises(ValueError, match="moved or its path was retargeted"):
        _resolve(manifest, tmp_path, binding)


def test_worktree_retains_catalog_ownership_and_data_directory_protections(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    inventory = registered_repository_roots(manifest, project_id="project")
    foreign = RegisteredRepositoryRoot(
        project_id="foreign",
        alias="foreign",
        machine="local",
        execution_host="",
        path=binding.worktree_path,
    )
    with pytest.raises(ValueError, match="overlaps another project"):
        _resolve(manifest, tmp_path, binding, inventory=[*inventory, foreign])
    with pytest.raises(ValueError, match="canonical project inventory"):
        _resolve(manifest, tmp_path, binding, inventory=[])
    with pytest.raises(ValueError, match="application data directory"):
        _resolve(manifest, tmp_path, binding, app_data_dir=Path(binding.worktree_path) / "data")


def test_storage_binding_reload_immutable_identity_and_removed_tombstone(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path).model_copy(update={"status": "creating"})
    database = tmp_path / "rcp.sqlite3"
    store = AppStore(database)
    assert store.conversation_worktree(binding.project_id, binding.chat_id) is None
    assert store.create_conversation_worktree(binding) == binding
    assert store.create_conversation_worktree(binding) == binding
    assert AppStore(database).conversation_worktree(binding.project_id, binding.chat_id) == binding
    with pytest.raises(ValueError, match="cannot be replaced"):
        store.create_conversation_worktree(binding.model_copy(update={"branch": "different"}))
    for before, after in [("creating", "ready"), ("ready", "removing"), ("removing", "removed")]:
        saved = store.set_conversation_worktree_status(
            binding.project_id, binding.chat_id, before, after
        )
        assert saved.status == after
        assert saved.model_dump(exclude={"status"}) == binding.model_dump(exclude={"status"})
    with pytest.raises(ValueError, match="cannot be replaced"):
        store.create_conversation_worktree(binding)
    with pytest.raises(ValueError, match="invalid.*transition"):
        store.set_conversation_worktree_status(
            binding.project_id, binding.chat_id, "removed", "ready"
        )


def test_storage_worktree_status_compare_and_set(manifest: Manifest, tmp_path: Path) -> None:
    binding = _binding(manifest, tmp_path).model_copy(update={"status": "creating"})
    database = tmp_path / "rcp.sqlite3"
    store = AppStore(database)
    store.create_conversation_worktree(binding)
    competing_stores = [AppStore(database), AppStore(database)]
    barrier = Barrier(2)

    def update(candidate: AppStore) -> bool:
        barrier.wait()
        try:
            candidate.set_conversation_worktree_status(
                binding.project_id, binding.chat_id, "creating", "ready"
            )
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, competing_stores)) == [False, True]


def test_existing_store_adds_worktree_table_without_rewriting_tasks(tmp_path: Path) -> None:
    database = tmp_path / "rcp.sqlite3"
    store = AppStore(database)
    with store.connection() as connection:
        connection.execute("DROP TABLE conversation_worktrees")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 8")
    upgraded = AppStore(database)
    assert upgraded.conversation_worktree("project", str(uuid.uuid4())) is None
    with upgraded.connection() as connection:
        assert (
            connection.execute(
                "SELECT migration_name FROM storage_schema_migrations WHERE migration_version = 8"
            ).fetchone()[0]
            == "conversation_worktrees_v1"
        )


def _register_project(
    store: AppStore, project_id: str, tmp_path: Path, *, canonical: bool = False
) -> None:
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            home_space_id=store.space_id if canonical else None,
            locator=str(tmp_path / project_id / "research.yaml"),
            name=project_id,
            state_location=str(tmp_path / project_id / ".research"),
            state_remote=False,
            added_at=store.now(),
        )
    )


@pytest.mark.parametrize("legacy_data", [False, True])
def test_project_identity_migration_preserves_worktree_binding(
    manifest: Manifest, tmp_path: Path, legacy_data: bool
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    binding = _binding(manifest, tmp_path).model_copy(update={"status": "creating"})
    store.create_conversation_worktree(binding)
    canonical = str(uuid.uuid4())
    if legacy_data:
        _register_project(store, canonical, tmp_path, canonical=True)
        store.migrate_legacy_project_data(binding.project_id, canonical)
    else:
        _register_project(store, binding.project_id, tmp_path)
        store.migrate_project_identity(binding.project_id, canonical, store.space_id)
    saved = AppStore(store.path).conversation_worktree(canonical, binding.chat_id)
    assert saved == binding.model_copy(update={"project_id": canonical})
    assert store.conversation_worktree(binding.project_id, binding.chat_id) is None
    assert Path(binding.worktree_path).is_dir()


def test_project_deletion_removes_only_its_binding_record(
    manifest: Manifest, tmp_path: Path
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    binding = _binding(manifest, tmp_path).model_copy(update={"status": "creating"})
    other = binding.model_copy(update={"project_id": "other"})
    for item in (binding, other):
        _register_project(store, item.project_id, tmp_path)
        store.create_conversation_worktree(item)
    counts = store.delete_project_records(binding.project_id)
    assert counts["conversation_worktrees"] == 1
    assert store.conversation_worktree(binding.project_id, binding.chat_id) is None
    assert store.conversation_worktree(other.project_id, other.chat_id) == other
    assert Path(binding.worktree_path).is_dir()


def test_work_turn_history_uses_only_durable_ordinary_chat_mode(
    manifest: Manifest, tmp_path: Path
) -> None:
    binding = _binding(manifest, tmp_path)
    store = AppStore(tmp_path / "rcp.sqlite3")
    assert not store.chat_has_work_turn(binding.project_id, binding.chat_id)
    for kind, mode, expected in [
        ("project_chat", "discuss", False),
        ("refresh", "work", False),
        ("node_chat", "work", True),
    ]:
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=str(uuid.uuid4()),
                project_id=binding.project_id,
                kind=kind,
                status="succeeded",
                request={"chat_id": binding.chat_id, "mode": mode},
                created_at=store.now(),
                updated_at=store.now(),
                status_message="complete",
            )
        )
        assert store.chat_has_work_turn(binding.project_id, binding.chat_id) is expected
    assert not store.chat_has_work_turn("foreign", binding.chat_id)
    assert not store.chat_has_work_turn(binding.project_id, str(uuid.uuid4()))
