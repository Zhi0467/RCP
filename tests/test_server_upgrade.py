from __future__ import annotations

import gzip
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import rcp.storage.base as storage_base_module
import rcp.storage.models as storage_models
from rcp.api import create_app
from rcp.config import load_manifest
from rcp.history import HistoryManager
from rcp.storage import AppStore

from . import server_upgrade_harness
from .server_upgrade_harness import (
    build_exact_base_checkout,
    build_exact_base_fixture,
    build_release_checkout,
    capture_release_update_with,
    exact_base_gate_enabled,
    immutable_fixture_directories,
    published_release_tags,
    verify_fixture_integrity,
    verify_fixture_registry,
)


def test_immutable_server_boundary_registry_is_complete() -> None:
    verify_fixture_registry()


def test_release_catalog_is_complete_and_missing_sources_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tags = [f"v1.0.{index}" for index in range(125)]
    releases = [{"tagName": tag, "isDraft": False, "isPrerelease": False} for tag in tags] + [
        {"tagName": "v2.0.0", "isDraft": True, "isPrerelease": False},
        {"tagName": "v2.0.1", "isDraft": False, "isPrerelease": True},
        {"tagName": "build/1", "isDraft": False, "isPrerelease": False},
    ]

    def release_list(argv: list[str], *, cwd: Path) -> str:
        assert argv[:3] == ["gh", "release", "list"]
        return json.dumps(releases[: int(argv[argv.index("--limit") + 1])])

    monkeypatch.setattr(server_upgrade_harness, "_capture", release_list)
    assert published_release_tags() == tags
    releases.clear()
    with pytest.raises(ValueError):
        published_release_tags()
    with pytest.raises(subprocess.CalledProcessError):
        build_release_checkout("refs/tags/upgrade-gate-missing-tag", tmp_path / "missing")


def test_pre_ledger_fixture_records_migrations_and_never_rescans(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = next(
        path
        for path in immutable_fixture_directories()
        if path.name == "pre-storage-migration-ledger-v12-c3191bf"
    )
    copied = tmp_path / "fixture"
    shutil.copytree(fixture, copied)
    database = _materialize_database(copied / "data")

    AppStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT migration_version FROM storage_schema_migrations ORDER BY migration_version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
            (15,),
            (16,),
            (17,),
            (18,),
            (19,),
            (20,),
            (21,),
            (22,),
            (23,),
            (24,),
        ]

    def unexpected_migration(*_args) -> None:
        raise AssertionError("completed legacy migration was rescanned")

    monkeypatch.setattr(
        AppStore,
        "_migrate_episode_lineage",
        classmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        storage_base_module,
        "migrate_legacy_episodes",
        unexpected_migration,
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_experiment_episode_state",
        staticmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_agent_usage_counted_dedupe",
        staticmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_space_run_projection_indexes",
        staticmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_conversation_worktrees",
        staticmethod(unexpected_migration),
    )

    AppStore(database)


@pytest.mark.parametrize("fixture", immutable_fixture_directories(), ids=lambda path: path.name)
def test_immutable_server_boundaries_upgrade_and_start(
    fixture: Path,
    tmp_path: Path,
) -> None:
    verify_fixture_integrity(fixture)
    copied = tmp_path / "fixture"
    shutil.copytree(fixture, copied)

    _exercise_candidate_upgrade(copied)


@pytest.mark.parametrize("fixture", immutable_fixture_directories(), ids=lambda path: path.name)
def test_immutable_server_boundaries_converge_on_the_baseline_schema(
    fixture: Path,
    tmp_path: Path,
) -> None:
    verify_fixture_integrity(fixture)
    copied = tmp_path / "fixture"
    shutil.copytree(fixture, copied)
    upgraded = _materialize_database(copied / "data")
    AppStore(upgraded)

    baseline = tmp_path / "baseline.sqlite3"
    AppStore(baseline)

    assert _normalized_schema(upgraded) == _normalized_schema(baseline)


@pytest.mark.skipif(not exact_base_gate_enabled(), reason="dedicated exact-base upgrade gate")
def test_exact_candidate_base_upgrades_and_starts(tmp_path: Path) -> None:
    checkout, base_commit = build_exact_base_checkout(tmp_path / "base-build")
    fixture = build_exact_base_fixture(checkout, base_commit, tmp_path)
    metadata = verify_fixture_integrity(fixture)

    assert metadata["created_with_commit"] == base_commit
    _exercise_candidate_upgrade(fixture)


@pytest.mark.skipif(not exact_base_gate_enabled(), reason="dedicated exact-base upgrade gate")
@pytest.mark.parametrize(
    "release_tag",
    published_release_tags() if exact_base_gate_enabled() else ["gate-disabled"],
)
@pytest.mark.parametrize("stale_cache", [False, True], ids=["current-cache", "stale-cache"])
def test_release_update_from_every_published_release_validates(
    release_tag: str,
    stale_cache: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every published release must migrate current and stale display caches.

    No supported source floor is declared yet; that policy decision remains open.

    This is the server's update across versions, from the releases a team server may
    still run: the copied-state validation, then the switched release's live check
    against the old release's data and display cache, current or left stale by a
    failed refresh.
    """
    from rcp.server_ops.deployment import (
        PrepareRequest,
        ValidateRequest,
        prepare,
        validate,
        verify_live_application,
    )

    # git archive fails for a missing tag; an incomplete fetch cannot skip a source.
    checkout = build_release_checkout(release_tag, tmp_path / "release")
    root = tmp_path / "update"
    capture = capture_release_update_with(checkout, root, stale_cache=stale_cache)
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(
            service_account=pwd.getpwuid(os.geteuid()).pw_name,
            projects_root=root / "projects",
        ),
    )
    # Use the candidate's preparation path: installed releases cannot classify
    # scratch produced by every tool an agent may have used after their release.
    prepared = prepare(
        PrepareRequest(
            version=1,
            data_dir=str(root / "data"),
            output_dir=str(root / "prepared"),
            **capture,
        )
    )
    checked = validate(
        ValidateRequest(
            version=1,
            proof_path=str(prepared["proof_path"]),
            proof_sha256=str(prepared["proof_sha256"]),
            output_dir=str(tmp_path / "validated"),
        )
    )
    assert checked["status"] == "verified"
    live = create_app(data_dir=root / "data")
    assert verify_live_application(
        Path(checked["proof_path"]),
        proof_sha256=checked["proof_sha256"],
        background=live.state.background_tasks,
        catalog=live.state.catalog,
        store=live.state.background_tasks.store,
    )
    # On a real server the copied cache can be stale while the live one is current,
    # so each check may read either; a current cache must equal a fresh replay.
    for card in live.state.catalog.cards():
        status, cached = live.state.catalog.cached_snapshot_status(card["id"])
        _service, fresh = live.state.catalog.open_snapshot(card["id"])
        assert status == "valid" and cached is not None
        if stale_cache:
            assert cached["graph"]["revision"] < fresh["graph"]["revision"]
        else:
            assert cached["graph"] == fresh["graph"]


def _exercise_candidate_upgrade(fixture: Path) -> None:
    metadata = verify_fixture_integrity(fixture)
    original_patches = _canonical_patch_hashes(fixture)
    data_dir = fixture / "data"
    database = _materialize_database(data_dir)
    experiment_episode_id = _metadata_optional_string(metadata, "experiment_episode_id")
    experiment_operation_id = _metadata_optional_string(metadata, "experiment_operation_id")
    expected_repair = metadata.get("expected_repair")
    _assert_raw_member_auth_rows(database, metadata)
    if expected_repair not in {None, "impossible_legacy_experiment_wrapup"}:
        raise ValueError("server-upgrade fixture names an unknown expected repair")
    if expected_repair is not None:
        assert experiment_episode_id is not None
        _assert_raw_impossible_experiment_wrapup(database, experiment_episode_id)
    with chdir(fixture):
        store = AppStore(database)
        space_id = _metadata_string(metadata, "space_id")
        user_id = _metadata_string(metadata, "user_id")
        project_id = _metadata_string(metadata, "project_id")
        operation_id = _metadata_string(metadata, "active_operation_id")
        assert store.space_id == space_id
        assert store.space_kind == "team"
        member = store.space_user(user_id)
        assert member is not None
        assert member.removal_started_at is None
        assert member.removed_at is None
        assert {member.user_id for member in store.project_members(project_id)} == {user_id}
        _assert_member_auth_rows(store, metadata)
        upgraded_task = store.agent_task(operation_id)
        assert upgraded_task is not None
        assert upgraded_task.status == "running"
        assert upgraded_task.history_only is False
        _assert_provisioning_rows(store, metadata)
        if experiment_episode_id is not None:
            assert store.experiment_episode(experiment_episode_id) is not None
            if expected_repair is not None:
                assert store.episode_wrapup(experiment_episode_id) is None

        record = store.project(project_id)
        assert record is not None
        manifest = load_manifest(record.locator)
        replay = (
            HistoryManager(
                manifest,
                expected_space_id=space_id,
                project_id=project_id,
                require_attribution=True,
            )
            .initialize()
            .state
        )
        assert replay.replay_status == "complete"
        assert replay.revision == int(metadata["expected_revision"])

        app = create_app(
            data_dir=data_dir,
            acceptance_agent=True,
            trusted_principal_resolver=lambda _request, opened: opened.space_user(user_id),
        )
        with TestClient(app) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            health_payload = health.json()
            expected_health = {
                "status": "ok",
                "space_id": space_id,
                "space_kind": "team",
                "agent_mode": "acceptance",
                "projects": 1,
                "active_agent_tasks": 0,
            }
            assert {name: health_payload[name] for name in expected_health} == expected_health

            projects = client.get("/api/projects")
            assert projects.status_code == 200
            assert [item["id"] for item in projects.json()] == [project_id]

            projection = client.get(f"/api/projects/{project_id}")
            assert projection.status_code == 200
            payload = projection.json()
            assert payload["id"] == project_id
            assert payload["home_space_id"] == space_id
            assert payload["graph"]["revision"] == int(metadata["expected_revision"])

            tasks = client.get(f"/api/projects/{project_id}/tasks")
            assert tasks.status_code == 200
            task_payload = tasks.json()
            recovered = next(item for item in task_payload if item["operation_id"] == operation_id)
            assert recovered["status"] == "interrupted"
            assert recovered["active"] is False
            if experiment_operation_id is not None:
                experiment_task = next(
                    item for item in task_payload if item["operation_id"] == experiment_operation_id
                )
                assert experiment_task["active"] is False

        reopened = AppStore(database)
        _assert_member_auth_rows(reopened, metadata)
        _assert_provisioning_rows(reopened, metadata)
        assert reopened.agent_task(operation_id).status == "interrupted"
        if experiment_episode_id is not None:
            assert reopened.experiment_episode(experiment_episode_id) is not None
            if expected_repair is not None:
                assert reopened.episode_wrapup(experiment_episode_id) is None
        assert _canonical_patch_hashes(fixture) == original_patches
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _metadata_string(metadata: dict[str, object], name: str) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"server-upgrade fixture metadata is missing {name}")
    return value


def _metadata_optional_string(metadata: dict[str, object], name: str) -> str | None:
    value = metadata.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"server-upgrade fixture metadata has invalid {name}")
    return value


def _materialize_database(data_dir: Path) -> Path:
    database = data_dir / "rcp.sqlite3"
    compressed = data_dir / "rcp.sqlite3.gz"
    if database.exists() or not compressed.is_file():
        raise ValueError("server-upgrade fixture database compression is invalid")
    database.write_bytes(gzip.decompress(compressed.read_bytes()))
    compressed.unlink()
    return database


def _canonical_patch_hashes(fixture: Path) -> dict[str, str]:
    patch_root = fixture / "project" / ".research" / "patches"
    return {
        path.relative_to(patch_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(patch_root.rglob("*"))
        if path.is_file()
    }


def _normalized_schema(database: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    return [
        (object_type, name, table_name, re.sub(r"\s+", " ", sql or "").strip())
        for object_type, name, table_name, sql in rows
    ]


def _assert_member_auth_rows(store: AppStore, metadata: dict[str, object]) -> None:
    user_id = _metadata_string(metadata, "user_id")
    token_id = _metadata_string(metadata, "member_token_id")
    token_hash = _metadata_string(metadata, "member_token_hash")
    session_hash = _metadata_string(metadata, "member_session_hash")
    with store.connection() as connection:
        credential = connection.execute(
            """
            SELECT token_hash, revoked_at FROM team_member_tokens
            WHERE token_id = ? AND user_id = ?
            """,
            (token_id, user_id),
        ).fetchone()
        session = connection.execute(
            "SELECT user_id FROM team_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
    assert credential is not None
    assert credential["token_hash"] == token_hash
    assert credential["revoked_at"] is None
    assert session is not None
    assert session["user_id"] == user_id


def _assert_raw_member_auth_rows(database: Path, metadata: dict[str, object]) -> None:
    user_id = _metadata_string(metadata, "user_id")
    token_id = _metadata_string(metadata, "member_token_id")
    token_hash = _metadata_string(metadata, "member_token_hash")
    session_hash = _metadata_string(metadata, "member_session_hash")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        credential = connection.execute(
            """
            SELECT token_hash, revoked_at FROM team_member_tokens
            WHERE token_id = ? AND user_id = ?
            """,
            (token_id, user_id),
        ).fetchone()
        session = connection.execute(
            "SELECT user_id FROM team_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
    assert credential is not None
    assert credential["token_hash"] == token_hash
    assert credential["revoked_at"] is None
    assert session is not None
    assert session["user_id"] == user_id


def _assert_provisioning_rows(store: AppStore, metadata: dict[str, object]) -> None:
    request_id = _metadata_optional_string(metadata, "provisioning_request_id")
    proposed_project_id = _metadata_optional_string(metadata, "provisioning_project_id")
    if request_id is None and proposed_project_id is None:
        return
    assert request_id is not None
    assert proposed_project_id is not None
    request = store.project_provisioning_request(request_id)
    assert request is not None
    assert request.proposed_project_id == proposed_project_id
    assert request.status == "setup_in_progress"
    assert request.revision == 1
    expected_configuration = metadata.get("provisioning_configuration_complete")
    if expected_configuration is not None:
        assert isinstance(expected_configuration, bool)
        assert request.configuration_complete is expected_configuration
    legacy_request_id = _metadata_optional_string(
        metadata,
        "legacy_provisioning_request_id",
    )
    if legacy_request_id is not None:
        legacy = store.project_provisioning_request(legacy_request_id)
        assert legacy is not None
        assert legacy.status == "setup_in_progress"
        assert legacy.configuration_complete is False
    assert store.project(proposed_project_id) is None
    receipts = store.project_provisioning_step_receipts(request_id)
    assert [receipt.receipt_id for receipt in receipts] == ["fixture-setup-started"]


def _assert_raw_impossible_experiment_wrapup(database: Path, episode_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        episode = connection.execute(
            "SELECT status, ending, wrapup_state FROM episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        wrapup = connection.execute(
            "SELECT state FROM episode_wrapups WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
    assert episode is not None
    assert episode["status"] in {"queued", "running", "stopping"}
    assert episode["ending"] is None
    assert episode["wrapup_state"] == "not_started"
    assert wrapup is not None
    assert wrapup["state"] == "legacy_unavailable"
