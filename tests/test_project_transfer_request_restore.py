from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rcp.core.transition_models import GraphHeadRef
from rcp.server_ops.restore import detach_restore_database
from rcp.storage import AppStore, ProjectTransferPhase, ProjectTransferRequestRecord
from tests.test_project_transfer_request_storage import (
    _archive_bound_pair,
    _linked_pair,
    _ready_incoming,
)

RESTORED_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
TARGET_NONTERMINAL_PHASES: tuple[ProjectTransferPhase, ...] = (
    "linked",
    "target_admitted",
    "source_released",
    "archive_bound",
    "target_activated",
    "cleanup_acknowledged",
)


def _protected_proof(store: AppStore, request_id: str) -> dict[str, object]:
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM project_transfer_proofs WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _target_at_phase(
    tmp_path: Path,
    phase: ProjectTransferPhase,
) -> tuple[AppStore, AppStore, ProjectTransferRequestRecord, ProjectTransferRequestRecord]:
    (
        source,
        target,
        source_actor,
        target_actor,
        configuration,
        source_request,
        target_request,
    ) = _linked_pair(tmp_path)
    if phase == "linked":
        return source, target, source_request, target_request

    _ready_incoming(target, target_request.request_id)
    target_request = target.record_target_project_transfer_admission(
        target_request.request_id,
        admitted_by=target_actor,
    )
    if phase == "target_admitted":
        return source, target, source_request, target_request

    assert target_request.target_admission_receipt is not None
    source_request = source.accept_target_project_transfer_admission(
        source_request.request_id,
        receipt=target_request.target_admission_receipt,
    )
    source_request = source.record_source_project_transfer_release(
        source_request.request_id,
        released_by=source_actor,
        revalidated_configuration=configuration,
        source_head=GraphHeadRef(revision=7, transition_id="c" * 64),
    )
    assert source_request.source_release_receipt is not None
    target_request = target.accept_source_project_transfer_release(
        target_request.request_id,
        receipt=source_request.source_release_receipt,
    )
    if phase == "source_released":
        return source, target, source_request, target_request

    fenced_head = GraphHeadRef(revision=8, transition_id="d" * 64)
    source_request = source.mark_source_project_transfer_fenced(
        source_request.request_id,
        source_head=fenced_head,
    )
    source.expose_project_transfer_proof(source_request.request_id)
    archive_sha256 = hashlib.sha256(b"one sealed transfer archive").hexdigest()
    source_request = source.bind_project_transfer_archive(
        source_request.request_id,
        archive_sha256=archive_sha256,
        archive_size_bytes=27,
    )
    target_request = target.bind_project_transfer_archive(
        target_request.request_id,
        archive_sha256=archive_sha256,
        archive_size_bytes=27,
        source_fence_head=fenced_head,
    )
    if phase == "archive_bound":
        return source, target, source_request, target_request

    target_request = target.mark_target_project_transfer_activated(target_request.request_id)
    if phase == "target_activated":
        return source, target, source_request, target_request

    target.expose_project_transfer_proof(target_request.request_id)
    target_request = target.acknowledge_project_transfer_proof(
        target_request.request_id,
        acknowledgement_sha256=hashlib.sha256(b"source accepted target proof").hexdigest(),
    )
    target_request = target.acknowledge_project_transfer_cleanup(target_request.request_id)
    assert phase == "cleanup_acknowledged"
    return source, target, source_request, target_request


@pytest.mark.parametrize("phase", TARGET_NONTERMINAL_PHASES)
def test_restore_freezes_each_nonterminal_target_phase_without_moving_its_boundary(
    tmp_path: Path,
    phase: ProjectTransferPhase,
) -> None:
    source, target, source_request, target_request = _target_at_phase(tmp_path, phase)
    source_before = source.project_transfer_request(source_request.request_id)
    target_before = target_request.model_dump(
        mode="json",
        exclude={"phase", "restore_resume_phase", "restore_diagnostic", "revision", "updated_at"},
    )
    proof_before = _protected_proof(target, target_request.request_id)

    detach_restore_database(
        target.path,
        confirmed_by="root@lab uid=0",
        detached_at=RESTORED_AT,
    )

    restored_target = target.project_transfer_request(target_request.request_id)
    assert restored_target is not None
    assert restored_target.phase == "operator_action_needed"
    assert restored_target.restore_resume_phase == phase
    assert "replacement-server archive" in restored_target.restore_diagnostic
    assert restored_target.revision == target_request.revision + 1
    assert restored_target.updated_at == RESTORED_AT.isoformat()
    assert (
        restored_target.model_dump(
            mode="json",
            exclude={
                "phase",
                "restore_resume_phase",
                "restore_diagnostic",
                "revision",
                "updated_at",
            },
        )
        == target_before
    )
    assert _protected_proof(target, target_request.request_id) == proof_before
    assert source.project_transfer_request(source_request.request_id) == source_before

    with pytest.raises(ValueError, match="not exposed"):
        target.expose_project_transfer_proof(target_request.request_id)

    detach_restore_database(
        target.path,
        confirmed_by="root@lab uid=0",
        detached_at=RESTORED_AT,
    )
    assert target.project_transfer_request(target_request.request_id) == restored_target
    assert _protected_proof(target, target_request.request_id) == proof_before


def test_restore_keeps_completed_target_and_fenced_source_records_unchanged(
    tmp_path: Path,
) -> None:
    source, target, source_request, target_request = _target_at_phase(
        tmp_path,
        "cleanup_acknowledged",
    )
    assert source_request.phase == "archive_bound"
    acknowledgment = target_request.proof_acknowledgement_sha256
    assert acknowledgment is not None
    target_request = target.consume_project_transfer_proof(
        target_request.request_id,
        acknowledgement_sha256=acknowledgment,
    )
    target_request = target.complete_project_transfer_request(target_request.request_id)
    source_before = source.project_transfer_request(source_request.request_id)
    target_before = target.project_transfer_request(target_request.request_id)
    proof_before = _protected_proof(target, target_request.request_id)

    detach_restore_database(
        target.path,
        confirmed_by="root@lab uid=0",
        detached_at=RESTORED_AT,
    )

    assert target.project_transfer_request(target_request.request_id) == target_before
    assert _protected_proof(target, target_request.request_id) == proof_before
    assert source.project_transfer_request(source_request.request_id) == source_before


@pytest.mark.parametrize("complete", [False, True])
def test_restore_invalidates_target_upload_and_refuses_reuse(
    tmp_path: Path,
    complete: bool,
) -> None:
    _source, target, _source_request, target_request = _archive_bound_pair(tmp_path)
    leased = target.begin_target_project_transfer_upload(target_request.request_id)
    if complete:
        target.complete_target_project_transfer_upload(
            target_request.request_id,
            lease_boundary_sha256=leased.lease_boundary_sha256,
        )

    detach_restore_database(
        target.path,
        confirmed_by="root@lab uid=0",
        detached_at=RESTORED_AT,
    )

    invalidated = target.target_project_transfer_upload(target_request.request_id)
    assert invalidated is not None
    assert invalidated.status == "invalidated"
    assert invalidated.lease_boundary_sha256 == leased.lease_boundary_sha256
    with pytest.raises(ValueError, match="restore re-entry"):
        target.begin_target_project_transfer_upload(target_request.request_id)
    with pytest.raises(ValueError, match="restore re-entry"):
        target.complete_target_project_transfer_upload(
            target_request.request_id,
            lease_boundary_sha256=leased.lease_boundary_sha256,
        )
