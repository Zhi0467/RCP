"""Legacy machine aliases and host keys map to space cards once, through one rule."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
import tomlkit

from rcp.config import load_manifest
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.machine_upgrade import (
    LegacyMachine,
    is_legacy_provisioning_payload,
    machine_id_for_alias,
    machine_id_for_host,
    migrate_machine_identity,
    read_legacy_manifest,
    read_legacy_provisioning_request,
    upgraded_hidden_read_scope,
    upgraded_manifest_text,
    upgraded_worktree_binding,
)
from rcp.storage import AppStore
from rcp.storage.base import AppStoreBase
from rcp.storage.models import ProjectRecord, SpaceMachineHostInUse, SpaceMachineRecord

BACKEND = BACKEND_MACHINE_ID
OLD_BACKEND = "c" * 32
GPU = "b" * 32
NOW = "2026-11-01T00:00:00+00:00"


def _card(machine_id: str, host: str) -> SpaceMachineRecord:
    return SpaceMachineRecord(
        machine_id=machine_id,
        name=host or "Backend",
        host=host,
        os_account="",
        created_at=NOW,
        updated_at=NOW,
    )


CARDS = [_card(GPU, "gpu.example")]


def _legacy_manifest(tmp_path: Path) -> Path:
    research = tmp_path / "project" / ".research"
    research.mkdir(parents=True)
    path = research / "manifest.toml"
    path.write_text(
        """name = "legacy"

[[machines]]
alias = "laptop"
host = ""

[[machines]]
alias = "gpu"
host = "gpu.example"
os_account = "alice"

[machines.compute]
job_manager = "slurm"

[[machines]]
alias = "gone"
host = "gone.example"

[[repositories]]
alias = "code"
machine = "laptop"
path = "/home/x/code"

[[repositories]]
alias = "data"
machine = "gpu"
path = "/srv/data"

[[repositories]]
alias = "old"
machine = "gone"
path = "/srv/old"

[project]
truth_scope = ["code", "data", "old"]

[state]
repository = "code"

[agent]
default_run_truth_scope = ["code"]

[agent.refresh]
provider = "claude"
run_on = "laptop"

[execution]
run_on = "laptop"

[paper.coach]
default_provider = "claude"
default_model = "opus"
default_reasoning = "high"
""",
        encoding="utf-8",
    )
    return path


def test_a_legacy_manifest_names_each_machine_by_the_card_for_its_host(tmp_path) -> None:
    path = _legacy_manifest(tmp_path)
    legacy = read_legacy_manifest(path)
    assert legacy is not None
    machine_ids = legacy.machine_ids(CARDS)

    path.write_text(upgraded_manifest_text(legacy, machine_ids), encoding="utf-8")
    manifest = load_manifest(path)

    assert machine_ids == {"laptop": BACKEND, "gpu": GPU, "gone": ""}
    assert manifest.machines == [BACKEND, GPU]
    assert {item.alias: item.machine for item in manifest.repositories} == {
        "code": BACKEND,
        "data": GPU,
        "old": "",
    }
    assert manifest.base_machine == BACKEND
    assert manifest.agent_profile("refresh").provider == "claude"
    coach = manifest.agent_profile("paper_coach")
    assert (coach.provider, coach.model, coach.reasoning) == ("claude", "opus", "high")
    document = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    assert "execution" not in document
    assert "paper" not in document
    assert read_legacy_manifest(path) is None
    assert legacy.machines[1].compute is not None


@pytest.mark.parametrize("host", ["-Ffoo", "-oProxyCommand=sh"])
def test_a_legacy_machine_rejects_option_shaped_ssh_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        LegacyMachine(alias="remote", host=host)


def test_the_rule_maps_by_host_and_aliases_only_through_their_manifest() -> None:
    machine_ids = {"laptop": BACKEND, "gone": ""}
    card_ids = {BACKEND, GPU}

    assert machine_id_for_host("gpu.example", CARDS) == GPU
    assert machine_id_for_host("", CARDS) == BACKEND
    assert machine_id_for_host("other.example", CARDS) == ""
    assert machine_id_for_alias("laptop", machine_ids, card_ids) == BACKEND
    assert machine_id_for_alias(GPU, machine_ids, card_ids) == GPU
    assert machine_id_for_alias("gone", machine_ids, card_ids) == ""
    assert machine_id_for_alias("gpu.example", machine_ids, card_ids) == ""


def test_stored_bindings_drop_their_alias_and_host_copies() -> None:
    machine_ids = {"gpu": GPU}
    binding = upgraded_worktree_binding(
        {"repository_alias": "data", "machine": "gpu", "execution_host": "gpu.example"},
        machine_ids,
        {GPU},
    )
    scope = upgraded_hidden_read_scope(
        {"execution_machine": "gpu", "execution_host": "gpu.example", "os_account": "alice"},
        machine_ids,
        {GPU},
    )

    assert binding == {"repository_alias": "data", "machine_id": GPU}
    assert scope == {"machine_id": GPU, "os_account": "alice"}


def _legacy_provisioning_payload(digest: str) -> dict[str, object]:
    space_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    return {
        "request_id": str(uuid.uuid4()),
        "kind": "add_repository",
        "status": "completed",
        "target_space_id": space_id,
        "authorized_by": {
            "space_id": space_id,
            "user_id": str(uuid.uuid4()),
            "display_name": "Owner",
        },
        "proposed_project_id": project_id,
        "target_project_id": project_id,
        "machines": [
            {
                "alias": "gpu",
                "location": "ssh",
                "host": "gpu.example",
                "os_account": "alice",
                "central_root": None,
                "resolved_central_root": "/srv/rcp",
            }
        ],
        "repositories": [
            {
                "alias": "data",
                "repository": None,
                "machine_alias": "gpu",
                "count_as_project_truth": True,
                "intended_path": None,
                "resolved_path": None,
                "checkout_disposition": None,
                "git_check": {
                    "status": "ready",
                    "commit": "0" * 40,
                    "write_verified": False,
                    "deploy_key_label": None,
                    "public_key_fingerprint": None,
                    "checked_at": NOW,
                    "diagnostic": None,
                },
            }
        ],
        "provider_checks": [],
        "final_review_digest": digest,
        "revision": 3,
        "created_at": NOW,
        "updated_at": NOW,
        "ready_at": NOW,
        "completed_at": NOW,
    }


def _review_digest(payload: dict[str, object]) -> str:
    reviewed = {
        key: payload[key]
        for key in (
            "request_id",
            "kind",
            "target_space_id",
            "authorized_by",
            "proposed_project_id",
            "machines",
            "repositories",
            "provider_checks",
            "target_project_id",
        )
    }
    reviewed |= {
        "name": None,
        "state_repository": None,
        "project_truth_scope": [],
        "default_run_truth_scope": [],
        "default_auto_research_invocation_ceiling": 10,
    }
    canonical = json.dumps(reviewed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_a_completed_legacy_receipt_verifies_then_names_its_cards() -> None:
    payload = _legacy_provisioning_payload("0" * 64)
    payload["final_review_digest"] = _review_digest(payload)

    record = read_legacy_provisioning_request(payload, CARDS)

    assert is_legacy_provisioning_payload(payload)
    assert [machine.machine_id for machine in record.machines] == [GPU]
    assert record.machines[0].resolved_central_root == "/srv/rcp"
    assert record.repositories[0].machine_id == GPU
    assert not is_legacy_provisioning_payload(record.model_dump(mode="json"))


def test_a_legacy_receipt_whose_digest_does_not_match_is_refused() -> None:
    payload = _legacy_provisioning_payload("0" * 64)
    payload["final_review_digest"] = _review_digest(payload)
    payload["machines"][0]["host"] = "elsewhere.example"  # type: ignore[index]

    with pytest.raises(ValueError):
        read_legacy_provisioning_request(payload, CARDS)


def _old_store(
    path: Path, monkeypatch: pytest.MonkeyPatch, seed: Callable[[sqlite3.Connection], None]
) -> None:
    migrations = AppStoreBase._STORAGE_SCHEMA_MIGRATIONS
    run_migration = AppStoreBase._run_storage_schema_migration

    def old_migration(self, connection, *, version, name, migration) -> None:
        if version < 77:
            run_migration(self, connection, version=version, name=name, migration=migration)

    with monkeypatch.context() as legacy:
        legacy.setattr(AppStore, "_baseline_storage_schema_cache", None)
        legacy.setattr(AppStore, "_baseline_storage_sql", ())
        legacy.setattr(AppStoreBase, "_run_storage_schema_migration", old_migration)
        legacy.setattr(
            AppStoreBase,
            "_STORAGE_SCHEMA_MIGRATIONS",
            tuple(item for item in migrations if item[0] < 77),
        )
        old = AppStore(path)
        with old.connection() as connection:
            seed(connection)
        old.close()


def _seed(connection: sqlite3.Connection) -> None:
    for machine_id, name, host in ((GPU, "GPU", "gpu.example"), (OLD_BACKEND, "Laptop", "")):
        connection.execute(
            "INSERT INTO space_machines (machine_id, name, host, os_account, "
            "writable_paths_json, created_at, updated_at) VALUES (?, ?, ?, 'alice', '[]', ?, ?)",
            (machine_id, name, host, NOW, NOW),
        )
    connection.execute(
        "INSERT INTO graph_runs (operation_id, project_id, kind, status, request_json, "
        "created_at, updated_at, status_message) "
        "VALUES ('space-op', 'space', 'space_chat', 'queued', ?, ?, ?, 'Waiting')",
        (json.dumps({"run_on": OLD_BACKEND, "chat_id": "thread"}), NOW, NOW),
    )
    connection.execute(
        "INSERT INTO agent_threads (thread_id, user_id, kind, created_at, agent_run_on) "
        "VALUES ('thread', 'user', 'space', ?, ?)",
        (NOW, OLD_BACKEND),
    )
    for host, changed_at in (
        ("gpu.example", NOW),
        ("", NOW),
        ("gone.example", "2026-10-01T00:00:00+00:00"),
        ("lost.example", "2026-10-02T00:00:00+00:00"),
    ):
        connection.execute(
            "INSERT INTO provider_login_states (provider, host, login_id, state, generation, "
            "changed_at, detail) VALUES ('codex', ?, '', 'signed_in', 1, ?, ?)",
            (host, changed_at, host),
        )
    connection.execute(
        "INSERT INTO provider_login_settings VALUES ('user', 'codex', 'gpu.example', 'own', ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT OR IGNORE INTO provider_login_settings VALUES ('user', 'codex', '*', 'shared', ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO provider_readiness_snapshots VALUES "
        "('codex', 'gpu.example', '', '/bin/codex', '1', '{}', ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO graph_runs (operation_id, project_id, kind, status, request_json, "
        "created_at, updated_at, status_message, stage_host, stage_root) "
        "VALUES ('op', 'remote', 'project_chat', 'running', ?, ?, ?, 'Running', '', '/stage')",
        (json.dumps({"run_on": "laptop", "chat_id": "chat"}), NOW, NOW),
    )
    connection.execute(
        "INSERT INTO graph_run_receipts (operation_id, created_at, tier, category, payload_json) "
        "VALUES ('op', ?, 'diagnostic', 'chat_stage_layout', ?)",
        (NOW, json.dumps({"layout": "split-v1", "stage_host": "gpu.example", "stage_root": "/s"})),
    )
    connection.execute(
        "INSERT INTO compute_jobs (job_id, project_id, origin_operation_id, execution_machine, "
        "execution_host, backend_id, backend_handle, job_root, cwd, argv, log_path, exit_path, "
        "containment, status, created_at) VALUES ('job', 'remote', 'op', 'gpu', 'gpu.example', "
        "'slurm', '1', '/j', '/j', '[]', '/j/log', '/j/exit', 'cooperative', 'running', ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO chat_session_contexts VALUES ('codex', 'gpu', 'native', 'remote', "
        "'project_chat', 'chat', NULL, 1, '{}', 'sha', 'op', ?, ?)",
        (NOW, NOW),
    )
    for project_id, location, remote in (
        ("remote", "gpu.example:/srv/p/.research", 1),
        ("local", "/home/x/p/.research", 0),
    ):
        connection.execute(
            "INSERT INTO projects (project_id, locator, name, state_location, state_remote, "
            "added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, project_id, project_id, location, remote, NOW),
        )
    connection.execute(
        "INSERT INTO memory_deletions VALUES ('gone', 'gpu.example:/srv/g/.research', ?, 'key')",
        (
            json.dumps(
                {
                    "project_id": "gone",
                    "locator": "gone",
                    "name": "gone",
                    "state_location": "gpu.example:/srv/g/.research",
                    "state_remote": True,
                    "added_at": NOW,
                }
            ),
        ),
    )


def test_migration_77_rekeys_host_rows_by_card_and_marks_projects(tmp_path, monkeypatch) -> None:
    path = tmp_path / "data" / "rcp.sqlite3"
    path.parent.mkdir()
    providers = tmp_path / "data" / "providers" / "codex"
    gpu_folder = providers / ("remote-" + hashlib.sha256(b"gpu.example").hexdigest())
    for folder in (providers / "local", gpu_folder):
        folder.mkdir(parents=True)
        (folder / "setup-token").write_text("token", encoding="utf-8")
    _old_store(path, monkeypatch, _seed)

    store = AppStore(path)

    backend_card = store.backend_space_machine()
    backend = backend_card.machine_id
    assert (backend, backend_card.name, backend_card.os_account) == (BACKEND, "Laptop", "alice")
    logins = {row.detail: row.machine_id for row in store.provider_login_states()}
    assert logins == {"gpu.example": GPU, "": backend, "lost.example": ""}
    assert store.provider_login_setting("user", "codex", GPU).setting == "own"
    assert store.provider_login_setting("user", "codex", backend).setting == "shared"
    with store.connection() as connection:
        projects = {
            row["project_id"]: ProjectRecord.model_validate(dict(row))
            for row in connection.execute("SELECT * FROM projects")
        }
    assert (projects["remote"].state_machine_id, projects["remote"].state_path) == (
        GPU,
        "/srv/p/.research",
    )
    assert (projects["local"].state_machine_id, projects["local"].state_path) == (
        backend,
        "/home/x/p/.research",
    )
    assert all(project.machine_ids_pending for project in projects.values())
    assert (providers / f"machine-{GPU}" / "setup-token").read_text(encoding="utf-8") == "token"
    assert (providers / f"machine-{backend}").is_dir()
    assert not gpu_folder.exists()
    with store.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM provider_readiness_snapshots").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT stage_machine_id FROM graph_runs WHERE operation_id = 'op'"
            ).fetchone()[0]
            == backend
        )
        receipt = json.loads(
            connection.execute("SELECT payload_json FROM graph_run_receipts").fetchone()[0]
        )
        assert receipt["stage_machine_id"] == GPU
        space_request = connection.execute(
            "SELECT request_json FROM graph_runs WHERE operation_id = 'space-op'"
        ).fetchone()[0]
        assert json.loads(space_request)["run_on"] == BACKEND
        assert connection.execute("SELECT agent_run_on FROM agent_threads").fetchone()[0] == BACKEND
        assert "stage_host" not in receipt
        assert connection.execute("SELECT machine_id FROM compute_jobs").fetchone()[0] == GPU
        connection.execute("UPDATE compute_jobs SET status = 'exited' WHERE job_id = 'job'")
        assert (
            connection.execute("SELECT machine_id FROM chat_session_contexts").fetchone()[0]
            == "gpu"
        )
        deletion = connection.execute("SELECT * FROM memory_deletions").fetchone()
        assert (deletion["state_machine_id"], deletion["state_path"]) == (GPU, "/srv/g/.research")
        assert json.loads(deletion["project_json"])["state_machine_id"] == GPU
        migrate_machine_identity(connection, path.parent)
        assert connection.execute("SELECT machine_id FROM compute_jobs").fetchone()[0] == GPU
    store.close()

    reopened = AppStore(path)
    assert reopened.storage_schema_ledger_head() == 77
    reopened.close()


def test_a_card_cannot_take_a_host_another_card_holds(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    first = store.create_space_machine(name="GPU", host="gpu.example", os_account="alice")
    second = store.create_space_machine(name="CPU", host="cpu.example", os_account="alice")

    with pytest.raises(SpaceMachineHostInUse):
        store.update_space_machine(second.machine_id, host="gpu.example")
    moved = store.update_space_machine(first.machine_id, host="gpu2.example", os_account="bob")

    assert (moved.host, moved.os_account, moved.name) == ("gpu2.example", "bob", "GPU")
    assert store.space_machine(second.machine_id).host == "cpu.example"
    store.close()
