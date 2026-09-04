from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from rcp.sources import ImportedProviderSourceStore, project_cache_roots
from rcp.storage import AgentTaskRecord
from rcp.transfer import TransferArchiveEntry

from .helpers import create_named_app as create_app
from .test_project_membership import _create_project, _team_app


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_team_delete_removes_rcp_state_and_preserves_checkout_and_key(tmp_path: Path) -> None:
    app, client, store, _people, acting = _team_app(tmp_path, members=1)
    repository = tmp_path / "managed-checkout"
    project_id = _create_project(client, repository)
    data_dir = store.path.parent

    checkout_marker = repository / "source.txt"
    checkout_marker.write_text("managed checkout\n", encoding="utf-8")
    repository_before = _tree_digest(repository)
    deploy_key = tmp_path / "server-credentials" / project_id / "paper-repo" / "id_ed25519"
    deploy_key.parent.mkdir(parents=True)
    deploy_key.write_text("private key marker\n", encoding="utf-8")

    source_cache, _session_cache = project_cache_roots(data_dir, project_id)
    cache_entry = source_cache / "remote" / "cached-source.jsonl"
    cache_entry.parent.mkdir(parents=True)
    cache_entry.write_text('{"source":"rebuildable"}\n', encoding="utf-8")
    imported_payload = b'{"type":"assistant","text":"retained"}\n'
    imported_digest = hashlib.sha256(imported_payload).hexdigest()
    imported_capture = (
        tmp_path / "imported-capture" / "provider-history" / "codex" / imported_digest
    )
    imported_capture.parent.mkdir(parents=True)
    imported_capture.write_bytes(imported_payload)
    imported_sources = ImportedProviderSourceStore(data_dir, project_id)
    imported_sources.publish(
        tmp_path / "imported-capture",
        (
            TransferArchiveEntry(
                archive_path=f"provider-history/codex/{imported_digest}",
                group="provider_history",
                sha256=imported_digest,
                size_bytes=len(imported_payload),
            ),
        ),
    )
    assert imported_sources.project_root.exists()

    stage = data_dir / "run-stage" / "retained-team-task"
    stage.mkdir(parents=True)
    stage_marker = stage / "patch.json"
    stage_marker.write_text("{}\n", encoding="utf-8")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="retained-team-task",
            project_id=project_id,
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
            stage_root=str(stage),
        )
    )
    display = app.state.catalog._cached_snapshot_path(project_id)
    display.parent.mkdir(parents=True, exist_ok=True)
    display.write_text("display snapshot\n", encoding="utf-8")
    paper = app.state.catalog._paper_snapshot_path(project_id)
    paper.parent.mkdir(parents=True, exist_ok=True)
    paper.write_text("paper snapshot\n", encoding="utf-8")

    [card] = client.get("/api/projects").json()
    assert card["id"] == project_id
    assert card["can_delete"] is True
    assert card["delete_unavailable_reason"] is None
    assert "server-managed checkout and repository deploy key remain" in card["delete_confirmation"]
    assert "credentials are not revoked" in card["delete_confirmation"]

    deleted = client.delete(f"/api/projects/{project_id}")

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["project_id"] == project_id
    assert store.project(project_id) is None
    assert store.project_members(project_id) == []
    assert store.agent_task("retained-team-task") is None
    assert not stage.exists()
    assert not display.exists()
    assert not paper.exists()
    assert not source_cache.parent.exists()
    assert not imported_sources.project_root.exists()
    assert _tree_digest(repository) == repository_before
    assert deploy_key.read_text(encoding="utf-8") == "private key marker\n"

    restarted = TestClient(
        create_app(
            data_dir=data_dir,
            trusted_principal_resolver=lambda _request, opened: opened.space_user(acting[0]),
        )
    )
    assert restarted.get("/api/projects").json() == []


def test_team_delete_removes_invitation_transfer_and_provisioning_history(
    tmp_path: Path,
) -> None:
    _app, client, store, people, acting = _team_app(tmp_path, members=2)
    _creator, invitee = people
    project_id = _create_project(client, tmp_path / "managed-checkout")
    invitation = client.post(
        f"/api/projects/{project_id}/invitations",
        json={"user_id": invitee.user_id},
    )
    assert invitation.status_code == 201, invitation.text
    invitation_id = invitation.json()["invitation_id"]
    request_id = str(uuid.uuid4())
    now = store.now()
    digest = "a" * 64
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO project_provisioning_requests (
                request_id, kind, status, target_space_id, authorized_by_json,
                proposed_project_id, machines_json, repositories_json,
                provider_checks_json, revision, created_at, updated_at, completed_at
            ) VALUES (?, 'incoming_transfer', 'completed', ?, '{}', ?, '[]', '[]',
                      '[]', 1, ?, ?, ?)
            """,
            (request_id, store.space_id, project_id, now, now, now),
        )
        connection.execute(
            """
            INSERT INTO project_provisioning_step_receipts (
                request_id, receipt_id, phase, from_status, to_status,
                transition_sha256, resulting_revision, created_at
            ) VALUES (?, 'receipt', 'complete', 'ready_for_review', 'completed', ?, 1, ?)
            """,
            (request_id, digest, now),
        )
        connection.execute(
            """
            INSERT INTO project_transfer_requests (
                request_id, side, phase, project_id, source_space_id,
                target_space_id, record_json, revision, created_at, updated_at
            ) VALUES (?, 'target', 'completed', ?, 'source-space', ?, '{}', 1, ?, ?)
            """,
            (request_id, project_id, store.space_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO project_transfer_imports (
                request_id, project_id, archive_manifest_sha256,
                target_manifest_sha256, operational_payload_sha256, status,
                event_id_map_json, receipt_id_map_json, publication_sha256,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, 'complete', '{}', '{}', ?, ?, ?)
            """,
            (request_id, project_id, digest, digest, digest, digest, now, now),
        )
        connection.execute(
            """
            INSERT INTO project_transfer_import_configurations (
                request_id, receipt_json, created_at
            ) VALUES (?, '{}', ?)
            """,
            (request_id, now),
        )
        connection.execute(
            """
            INSERT INTO project_transfer_proofs (
                request_id, proof_kind, state, commitment_sha256, secret,
                acknowledgement_sha256, exposed_at, acknowledged_at, consumed_at
            ) VALUES (?, 'target_activation', 'consumed', ?, NULL, ?, ?, ?, ?)
            """,
            (request_id, digest, digest, now, now, now),
        )
        connection.execute(
            """
            INSERT INTO project_transfer_uploads (
                request_id, project_id, archive_sha256, archive_size_bytes,
                lease_boundary_sha256, status, receipt_json, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, 'complete', '{}', ?, ?)
            """,
            (request_id, project_id, digest, digest, now, now),
        )
        connection.execute(
            """
            INSERT INTO project_transfer_activations (
                target_request_id, project_id, receipt_json, activated_at
            ) VALUES (?, ?, '{}', ?)
            """,
            (request_id, project_id, now),
        )
        connection.execute(
            """
            INSERT INTO project_transfer_restore_reentries (
                target_request_id, restored_revision, receipt_json, created_at
            ) VALUES (?, 1, '{}', ?)
            """,
            (request_id, now),
        )
        connection.execute(
            "INSERT INTO _legacy_campaigns_archive(campaign_id, project_id) VALUES ('legacy', ?)",
            (project_id,),
        )
        for table in (
            "_legacy_campaign_invocations_archive",
            "_legacy_campaign_messages_archive",
            "_legacy_campaign_recoveries_archive",
            "_legacy_campaign_reports_archive",
        ):
            connection.execute(f"INSERT INTO {table}(campaign_id) VALUES ('legacy')")

    deleted = client.delete(f"/api/projects/{project_id}")

    assert deleted.status_code == 200, deleted.text
    acting[0] = invitee.user_id
    refused = client.post(f"/api/project-invitations/{invitation_id}/accept")
    assert refused.status_code == 404
    assert refused.json()["detail"] == "Invitation not found"
    with store.connection() as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for table in table_names:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "project_id" in columns:
                remaining = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
                assert remaining == 0, table
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM project_provisioning_requests WHERE proposed_project_id = ?",
                (project_id,),
            ).fetchone()[0]
            == 0
        )
        for table, column in (
            ("project_provisioning_step_receipts", "request_id"),
            ("project_transfer_import_configurations", "request_id"),
            ("project_transfer_proofs", "request_id"),
            ("project_transfer_restore_reentries", "target_request_id"),
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (request_id,)
                ).fetchone()[0]
                == 0
            )


def test_team_delete_refuses_an_active_task_before_touching_the_checkout(tmp_path: Path) -> None:
    app, client, store, _people, _acting = _team_app(tmp_path, members=1)
    repository = tmp_path / "managed-checkout"
    project_id = _create_project(client, repository)
    before = _tree_digest(repository)
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="active-team-delete-test",
            project_id=project_id,
            kind="seed",
            status="queued",
            request={},
            created_at=now,
            updated_at=now,
            status_message="queued",
        )
    )

    refused = client.delete(f"/api/projects/{project_id}")

    assert refused.status_code == 409
    assert refused.json()["detail"] == ("Pause the active agent task before deleting this project.")
    assert store.project(project_id) is not None
    assert _tree_digest(repository) == before
