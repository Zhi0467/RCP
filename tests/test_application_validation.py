from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import rcp.server_ops.application_validation as rehearsal_module
from rcp.__main__ import instance_lock
from rcp.api import create_app
from rcp.background import StartupEffectBlocked, StartupEffectFence
from rcp.config import load_manifest
from rcp.projects import (
    TEAM_PROJECT_DELETE_CONFIRMATION,
    TEAM_PROJECT_DELETE_UNAVAILABLE_REASON,
)
from rcp.server_ops.application_validation import (
    CandidateRehearsalRefused,
    RehearsalOverlay,
    RehearsalProjectOverlay,
    StartupRecoveryReadModel,
    build_rehearsal_overlay,
    run_candidate_child,
)
from rcp.server_ops.backup_capture import (
    BackupSQLiteCaptureReceipt,
)
from rcp.server_ops.backup_models import (
    BackupAppDataCapturePlan,
    BackupFileEntry,
)
from rcp.server_ops.backup_project_files import BackupProjectFileCaptureReceipt
from rcp.storage import (
    AppStore,
    ProjectRecord,
)

BASE_COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40
WEB_BUILD_ID = "sha256:" + ("c" * 64)
FINGERPRINT = "SHA256:" + ("A" * 43)


def _write_remote_manifest(root: Path, *, project_id: str) -> Path:
    remote_root = f"/srv/rcp/projects/{project_id}/repositories/repo"
    root.mkdir(parents=True, mode=0o700)
    manifest = root / "manifest.toml"
    manifest.write_text(
        f'''name = "Unavailable remote project"

[[machines]]
alias = "remote"
host = "unreachable.example.test"
os_account = "rcp"

[[repositories]]
alias = "repo"
machine = "remote"
path = "{remote_root}"

[project]
truth_scope = ["repo"]

[state]
repository = "repo"

[agent]
default_run_truth_scope = ["repo"]

[sources]
claude_roots = ["~/.claude/projects"]
codex_roots = ["~/.codex/sessions"]

[execution]
run_on = "remote"

[paper.coach]
default_provider = "codex"
default_model = ""
default_reasoning = "medium"
''',
        encoding="utf-8",
    )
    return manifest


def _run_unavailable_project_candidate_child(
    tmp_path: Path,
    *,
    expected_updates: dict[str, object] | None = None,
) -> tuple[int, dict[str, object], str]:
    operation_root = tmp_path / "operation"
    overlay_root = operation_root / "overlay"
    data_dir = overlay_root / "data"
    data_dir.mkdir(parents=True, mode=0o700)
    store, bootstrap = AppStore.initialize_team_space(
        data_dir / "rcp.sqlite3",
        "Unavailable Project Rehearsal Lab",
    )
    member, _token = store.enroll_team_member(bootstrap, "Alice")
    project_id = str(uuid.uuid4())
    manifest_path = _write_remote_manifest(
        overlay_root / "projects" / project_id,
        project_id=project_id,
    )
    manifest = load_manifest(manifest_path)
    state_location = f"/srv/rcp/projects/{project_id}/repositories/repo/.research"
    remote_error = "The configured SSH canonical state is currently unreachable."
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            home_space_id=store.space_id,
            locator=str(manifest.path),
            name=manifest.name,
            state_location=state_location,
            state_remote=True,
            added_at=store.now(),
            revision=7,
            reachable=False,
            error=remote_error,
        )
    )
    store.seat_project_member(project_id, member.user_id)
    comparison: dict[str, object] = {
        "id": project_id,
        "home_space_id": store.space_id,
        "name": manifest.name,
        "locator": str(manifest.path),
        "state_location": state_location,
        "remote": True,
        "last_opened_at": None,
        "revision": 7,
        "primary_question": None,
        "attention_count": 0,
        "last_refresh_at": None,
        "reachable": False,
        "error_sha256": hashlib.sha256(remote_error.encode()).hexdigest(),
        "can_delete": True,
        "delete_unavailable_reason": None,
        "delete_confirmation": TEAM_PROJECT_DELETE_CONFIRMATION,
    }
    if expected_updates is not None:
        comparison.update(expected_updates)
    if comparison["can_delete"] is False:
        comparison.pop("delete_confirmation")
    expected_sha256 = rehearsal_module._canonical_sha256(comparison)
    overlay = RehearsalOverlay(
        root=str(overlay_root),
        data_dir=str(data_dir),
        database_path=str(data_dir / "rcp.sqlite3"),
        capture_id=str(uuid.uuid4()),
        sqlite_receipt_sha256="1" * 64,
        sqlite_snapshot_sha256="2" * 64,
        project_receipt_sha256="3" * 64,
        space_id=store.space_id,
        expected_startup_recovery=StartupRecoveryReadModel(
            active_operation_ids=(),
            stopping_experiment_operation_ids=(),
            report_episode_ids=(),
            auto_research_recovery_operation_ids=(),
            active_watcher_ids=(),
        ),
        projects=(
            RehearsalProjectOverlay(
                project_id=project_id,
                name=manifest.name,
                capture_status="remote_unreachable",
                overlay_locator=str(manifest.path),
                original_locator=str(manifest.path),
                original_state_location=state_location,
                original_remote=True,
                original_reachable=False,
                original_error_sha256=hashlib.sha256(remote_error.encode()).hexdigest(),
                expected_card_sha256=expected_sha256,
                expected_graph_sha256=None,
            ),
        ),
        transfer_inbox_entries=(),
    )
    overlay_path = operation_root / "overlay.json"
    result_path = operation_root / "result.json"
    overlay_path.write_text(overlay.model_dump_json(), encoding="utf-8")
    overlay_path.chmod(0o600)

    exit_code = run_candidate_child(overlay_path, result_path)
    return exit_code, json.loads(result_path.read_text(encoding="utf-8")), expected_sha256


def test_candidate_child_accepts_retired_team_deletion_card_projection(tmp_path: Path) -> None:
    exit_code, result, expected_sha256 = _run_unavailable_project_candidate_child(
        tmp_path,
        expected_updates={
            "can_delete": False,
            "delete_unavailable_reason": TEAM_PROJECT_DELETE_UNAVAILABLE_REASON,
        },
    )

    assert exit_code == 0, result
    assert result["status"] == "verified"
    assert result["projects"][0]["projection_sha256"] == expected_sha256


def test_candidate_child_refuses_unrelated_retired_card_projection_change(
    tmp_path: Path,
) -> None:
    exit_code, result, _expected_sha256 = _run_unavailable_project_candidate_child(
        tmp_path,
        expected_updates={
            "revision": 8,
            "can_delete": False,
            "delete_unavailable_reason": TEAM_PROJECT_DELETE_UNAVAILABLE_REASON,
        },
    )

    assert exit_code == 1
    assert result["status"] == "failed"
    assert "changed unavailable projection" in result["diagnostic"]


def test_unavailable_card_projection_hash_accepts_only_the_retired_team_shape() -> None:
    from rcp.projects import TEAM_PROJECT_DELETE_CONFIRMATION
    from rcp.server_ops.application_validation import (
        _canonical_sha256,
        _project_card_comparison,
        unavailable_card_projection_sha256,
    )

    card = {
        "id": "project-one",
        "name": "Project one",
        "revision": 4,
        "can_delete": True,
        "delete_unavailable_reason": None,
        "delete_confirmation": TEAM_PROJECT_DELETE_CONFIRMATION,
    }
    current = _canonical_sha256(_project_card_comparison(card))
    legacy_comparison = _project_card_comparison(card)
    legacy_comparison.pop("delete_confirmation")
    legacy_comparison.update(
        can_delete=False,
        delete_unavailable_reason=TEAM_PROJECT_DELETE_UNAVAILABLE_REASON,
    )
    legacy = _canonical_sha256(legacy_comparison)
    unrelated = _canonical_sha256({**legacy_comparison, "revision": 5})

    assert unavailable_card_projection_sha256(card, current, team_space=True) == current
    assert unavailable_card_projection_sha256(card, legacy, team_space=True) == legacy
    assert unavailable_card_projection_sha256(card, legacy, team_space=False) == current
    assert unavailable_card_projection_sha256(card, unrelated, team_space=True) == current


def test_candidate_child_accepts_current_team_deletion_card_projection(tmp_path: Path) -> None:
    exit_code, result, expected_sha256 = _run_unavailable_project_candidate_child(tmp_path)

    assert exit_code == 0, result
    assert result["status"] == "verified"
    assert result["projects"][0]["projection_sha256"] == expected_sha256


def test_candidate_child_refuses_changed_team_delete_confirmation(tmp_path: Path) -> None:
    exit_code, result, _expected_sha256 = _run_unavailable_project_candidate_child(
        tmp_path,
        expected_updates={"delete_confirmation": "Changed deletion warning."},
    )

    assert exit_code == 1
    assert result["status"] == "failed"
    assert "changed unavailable projection" in result["diagnostic"]


def test_fenced_startup_only_plans_recovery_and_rejects_effect_entrypoints(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    store, bootstrap = AppStore.initialize_team_space(
        data_dir / "rcp.sqlite3",
        "Fenced Startup Lab",
    )
    member, _token = store.enroll_team_member(bootstrap, "Alice")
    fence = StartupEffectFence("candidate update rehearsal")
    app = create_app(
        data_dir=data_dir,
        trusted_principal_resolver=lambda _request, current: current.space_user(member.user_id),
        startup_effect_fence=fence,
    )

    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert app.state.startup_recovery_plan == {
            "active_operation_ids": (),
            "stopping_experiment_operation_ids": (),
            "report_episode_ids": (),
            "auto_research_recovery_operation_ids": (),
            "active_watcher_ids": (),
        }
        with pytest.raises(StartupEffectBlocked, match="blocked startup recovery"):
            app.state.services.background_tasks.recover_at_startup()

    assert fence.attempted_effects == ("startup recovery",)
    with pytest.raises(StartupEffectBlocked, match="cannot open"):
        fence.release()


def test_releasing_the_same_startup_fence_starts_the_deferred_runtime(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    store, bootstrap = AppStore.initialize_team_space(
        data_dir / "rcp.sqlite3",
        "Deferred Startup Lab",
    )
    member, _token = store.enroll_team_member(bootstrap, "Alice")
    fence = StartupEffectFence("candidate cutover verification")
    app = create_app(
        data_dir=data_dir,
        trusted_principal_resolver=lambda _request, current: current.space_user(member.user_id),
        startup_effect_fence=fence,
    )

    with TestClient(app):
        assert not app.state.startup_effect_runtime_started
        fence.release()
        assert app.state.startup_effect_runtime_event.wait(timeout=2)
        assert app.state.startup_effect_runtime_started
        assert app.state.startup_effect_release_error is None


def test_candidate_child_refuses_when_overlay_ownership_is_already_held(tmp_path: Path) -> None:
    operation_root = tmp_path / "operation"
    data_dir = operation_root / "overlay" / "data"
    data_dir.mkdir(parents=True, mode=0o700)
    store, bootstrap = AppStore.initialize_team_space(
        data_dir / "rcp.sqlite3",
        "Locked Rehearsal Lab",
    )
    store.enroll_team_member(bootstrap, "Alice")
    overlay = RehearsalOverlay(
        root=str(operation_root / "overlay"),
        data_dir=str(data_dir),
        database_path=str(data_dir / "rcp.sqlite3"),
        capture_id=str(uuid.uuid4()),
        sqlite_receipt_sha256="1" * 64,
        sqlite_snapshot_sha256="2" * 64,
        project_receipt_sha256="3" * 64,
        space_id=store.space_id,
        expected_startup_recovery=StartupRecoveryReadModel(
            active_operation_ids=(),
            stopping_experiment_operation_ids=(),
            report_episode_ids=(),
            auto_research_recovery_operation_ids=(),
            active_watcher_ids=(),
        ),
        projects=(),
        transfer_inbox_entries=(),
    )
    overlay_path = operation_root / "overlay.json"
    result_path = operation_root / "result.json"
    overlay_path.write_text(overlay.model_dump_json(), encoding="utf-8")
    overlay_path.chmod(0o600)

    with instance_lock(data_dir):
        assert run_candidate_child(overlay_path, result_path) == 1

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert "Another RCP process" in result["diagnostic"]


def test_candidate_child_accepts_a_fresh_team_waiting_for_first_enrollment(
    tmp_path: Path,
) -> None:
    operation_root = tmp_path / "operation"
    data_dir = operation_root / "overlay" / "data"
    data_dir.mkdir(parents=True, mode=0o700)
    store, _bootstrap = AppStore.initialize_team_space(
        data_dir / "rcp.sqlite3",
        "Fresh Rehearsal Lab",
    )
    overlay = RehearsalOverlay(
        root=str(operation_root / "overlay"),
        data_dir=str(data_dir),
        database_path=str(data_dir / "rcp.sqlite3"),
        capture_id=str(uuid.uuid4()),
        sqlite_receipt_sha256="1" * 64,
        sqlite_snapshot_sha256="2" * 64,
        project_receipt_sha256="3" * 64,
        space_id=store.space_id,
        expected_startup_recovery=StartupRecoveryReadModel(
            active_operation_ids=(),
            stopping_experiment_operation_ids=(),
            report_episode_ids=(),
            auto_research_recovery_operation_ids=(),
            active_watcher_ids=(),
        ),
        projects=(),
        transfer_inbox_entries=(),
    )
    overlay_path = operation_root / "overlay.json"
    result_path = operation_root / "result.json"
    overlay_path.write_text(overlay.model_dump_json(), encoding="utf-8")
    overlay_path.chmod(0o600)

    exit_code = run_candidate_child(overlay_path, result_path)
    assert exit_code == 0, result_path.read_text(encoding="utf-8")

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "verified"
    assert result["space_id"] == store.space_id
    assert result["reads"] == ["/api/health", "/api/projects"]
    assert result["projects"] == []
    assert result["attempted_effects"] == []


def _future_schema_capture(
    tmp_path: Path,
) -> tuple[BackupSQLiteCaptureReceipt, str, BackupProjectFileCaptureReceipt, Path, Path]:
    """Copy-ready receipts for one fresh snapshot a candidate may migrate."""

    capture_id = str(uuid.uuid4())
    space_id = str(uuid.uuid4())
    capture_root = tmp_path / f"backup-{capture_id}"
    capture_root.mkdir(mode=0o700)
    snapshot = capture_root / "rcp.sqlite3"
    AppStore(snapshot)
    snapshot_bytes = snapshot.read_bytes()
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    sqlite_receipt_sha256 = "e" * 64
    captured_at = datetime.now(UTC)
    sqlite_receipt = BackupSQLiteCaptureReceipt(
        capture_id=capture_id,
        captured_at=captured_at,
        rcp_source_commit=BASE_COMMIT,
        space_id=space_id,
        space_name="Future schema lab",
        snapshot_path=str(snapshot),
        database_schema_sha256="f" * 64,
        sqlite_snapshot=BackupFileEntry(
            archive_path="database/rcp.sqlite3",
            source_relative_path="rcp.sqlite3",
            group="sqlite_snapshot",
            sha256=snapshot_sha256,
            size_bytes=len(snapshot_bytes),
        ),
        app_data_plan=BackupAppDataCapturePlan(
            data_dir=str(tmp_path / "live-data"),
            database_path=str(tmp_path / "live-data" / "rcp.sqlite3"),
            database_unavailable_reason=None,
            excluded_entries=(),
            deferred_entries=(),
            unclassified_entries=(),
        ),
        projects=(),
        status="complete",
    )
    project_receipt = BackupProjectFileCaptureReceipt(
        capture_id=capture_id,
        captured_at=captured_at,
        completed_at=captured_at,
        rcp_source_commit=BASE_COMMIT,
        space_id=space_id,
        sqlite_receipt_sha256=sqlite_receipt_sha256,
        sqlite_snapshot_sha256=snapshot_sha256,
        sqlite_capture_status="complete",
        projects=(),
        status="complete",
    )
    operation_root = tmp_path / "operation"
    operation_root.mkdir(mode=0o700)
    return sqlite_receipt, sqlite_receipt_sha256, project_receipt, capture_root, operation_root


def test_overlay_refuses_a_new_unclassified_database_path_column(
    tmp_path: Path,
) -> None:
    sqlite_receipt, sqlite_receipt_sha256, project_receipt, capture_root, operation_root = (
        _future_schema_capture(tmp_path)
    )

    def candidate_adds_unknown_path(database_path: Path) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE future_state (future_root TEXT)")

    with pytest.raises(CandidateRehearsalRefused, match="unclassified path columns"):
        build_rehearsal_overlay(
            operation_root,
            sqlite_receipt=sqlite_receipt,
            sqlite_receipt_sha256=sqlite_receipt_sha256,
            project_receipt=project_receipt,
            project_receipt_sha256="1" * 64,
            capture_root=capture_root,
            candidate_migrator=candidate_adds_unknown_path,
        )


def test_overlay_records_the_running_release_expectation_before_candidate_migration(
    tmp_path: Path,
) -> None:
    """A candidate that appends a ledger row must still rehearse.

    The running release cannot open a copy whose migration ledger is longer
    than its own, so its startup-recovery expectation has to be taken before
    the candidate migrates the copy. The lab update from 8dc68e1 to 0684bc4 was
    refused with "migration ledger is invalid" when it was taken afterwards.
    """

    sqlite_receipt, sqlite_receipt_sha256, project_receipt, capture_root, operation_root = (
        _future_schema_capture(tmp_path)
    )
    migrated: list[tuple[int, str]] = []

    def candidate_appends_a_ledger_row(database_path: Path) -> None:
        with sqlite3.connect(database_path) as connection:
            head = connection.execute(
                "SELECT MAX(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO storage_schema_migrations "
                "(migration_version, migration_name, completed_at) VALUES (?, ?, ?)",
                (head + 1, "future_candidate_v1", datetime.now(UTC).isoformat()),
            )
            migrated.append((head + 1, "future_candidate_v1"))

    overlay = build_rehearsal_overlay(
        operation_root,
        sqlite_receipt=sqlite_receipt,
        sqlite_receipt_sha256=sqlite_receipt_sha256,
        project_receipt=project_receipt,
        project_receipt_sha256="1" * 64,
        capture_root=capture_root,
        candidate_migrator=candidate_appends_a_ledger_row,
    )

    assert migrated == [(len(AppStore._STORAGE_SCHEMA_MIGRATIONS) + 1, "future_candidate_v1")]
    assert overlay.expected_startup_recovery == StartupRecoveryReadModel(
        active_operation_ids=(),
        stopping_experiment_operation_ids=(),
        report_episode_ids=(),
        auto_research_recovery_operation_ids=(),
        active_watcher_ids=(),
    )
    with sqlite3.connect(overlay.database_path) as connection:
        ledger = connection.execute(
            "SELECT migration_version, migration_name FROM storage_schema_migrations "
            "ORDER BY migration_version"
        ).fetchall()
    assert ledger[-1] == migrated[0]
