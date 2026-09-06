from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import pytest

import rcp.server_ops.restore as restore_code
from rcp.core.models import AuthorizedHuman
from rcp.server_ops.backup_capture import _database_schema_sha256
from rcp.server_ops.github import parse_github_repository_ref
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
from rcp.server_ops.restore import SUPPORTED_RESTORE_DATABASE_SCHEMAS
from rcp.storage import (
    AppStore,
    ProjectProvisioningMachineIntent,
    ProjectProvisioningProviderIntent,
    ProjectProvisioningRepositoryIntent,
)

CAPTURED_AT = datetime(2026, 8, 29, 12, tzinfo=UTC)


def _provisioning_request(store: AppStore):
    member = store.preprovision_team_member("Alice")
    request = store.create_project_provisioning_request(
        kind="create_team_project",
        authorized_by=AuthorizedHuman(
            space_id=store.space_id,
            user_id=member.user_id,
            display_name=member.display_name,
        ),
        name="Restored project",
        state_repository="paper",
        project_truth_scope=["paper"],
        default_run_truth_scope=["paper"],
        machines=[
            ProjectProvisioningMachineIntent(
                alias="server",
                location="local",
                os_account="rcp",
                central_root=str(DEFAULT_SERVER_LAYOUT.projects_root),
            )
        ],
        repositories=[
            ProjectProvisioningRepositoryIntent(
                alias="paper",
                repository=parse_github_repository_ref("git@github.com:OpenAI/RCP-paper.git"),
                machine_alias="server",
            )
        ],
        provider_checks=[
            ProjectProvisioningProviderIntent(
                profile="seed",
                provider="codex",
                runtime_id="codex:exec",
                model="gpt-test",
                reasoning="medium",
                machine_alias="server",
            )
        ],
    )
    return store.transition_project_provisioning_request(
        request.request_id,
        receipt_id="machine-started",
        phase="provisioning_start",
        expected_revision=request.revision,
        expected_status=request.status,
        to_status="setup_in_progress",
        machines=request.machines,
        repositories=request.repositories,
        provider_checks=request.provider_checks,
    )


def test_provisioning_restore_detachment_is_idempotent_and_keeps_step_receipts(
    tmp_path: Path,
) -> None:
    store, _bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Lease lab")
    running = _provisioning_request(store)
    receipts = store.project_provisioning_step_receipts(running.request_id)

    store.detach_restored_lifecycle(
        diagnostic="Replacement restore invalidated old machine authority.",
        confirmed_by="root@lab uid=0",
        detached_at=CAPTURED_AT.isoformat(),
    )
    first = store.project_provisioning_request(running.request_id)
    assert first is not None
    assert first.status == "operator_action_needed"
    assert first.revision == running.revision + 1
    assert store.project_provisioning_step_receipts(running.request_id) == receipts

    store.detach_restored_lifecycle(
        diagnostic="Replacement restore invalidated old machine authority.",
        confirmed_by="root@lab uid=0",
        detached_at=CAPTURED_AT.isoformat(),
    )

    assert store.project_provisioning_request(running.request_id) == first
    assert store.project_provisioning_step_receipts(running.request_id) == receipts


def test_provisioning_restore_detachment_rolls_back_a_stale_transition(
    tmp_path: Path, monkeypatch
) -> None:
    store, _bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Lease lab")
    running = _provisioning_request(store)
    original = store._transition_project_provisioning_to_restore_reentry

    def make_transition_stale(connection, *, current, **values) -> None:
        connection.execute(
            """
            UPDATE project_provisioning_requests
            SET revision = revision + 1
            WHERE request_id = ?
            """,
            (current.request_id,),
        )
        original(connection, current=current, **values)

    monkeypatch.setattr(
        store,
        "_transition_project_provisioning_to_restore_reentry",
        make_transition_stale,
    )
    with pytest.raises(RuntimeError, match="changed during restore detachment"):
        store.detach_restored_lifecycle(
            diagnostic="Replacement restore invalidated old machine authority.",
            confirmed_by="root@lab uid=0",
            detached_at=CAPTURED_AT.isoformat(),
        )

    assert store.project_provisioning_request(running.request_id) == running


def test_restore_schema_registry_covers_current_and_immutable_upgrade_boundaries(
    tmp_path: Path,
) -> None:
    current, _bootstrap = AppStore.initialize_team_space(tmp_path / "current.sqlite3", "Current")
    assert _database_schema_sha256(current) in SUPPORTED_RESTORE_DATABASE_SCHEMAS
    for fixture in sorted(
        path for path in Path("tests/fixtures/server_upgrade").iterdir() if path.is_dir()
    ):
        compressed = fixture / "data" / "rcp.sqlite3.gz"
        database = tmp_path / f"{fixture.name}.sqlite3"
        database.write_bytes(gzip.decompress(compressed.read_bytes()))
        assert (
            restore_code._database_schema_sha256(  # noqa: SLF001 - compatibility boundary
                database
            )
            in SUPPORTED_RESTORE_DATABASE_SCHEMAS
        )
