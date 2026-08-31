from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphHeadRef
from rcp.server_ops.github import parse_github_repository_ref
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    ProjectProvisioningGitCheckRecord,
    ProjectProvisioningMachineIntent,
    ProjectProvisioningProviderCheckRecord,
    ProjectProvisioningProviderIntent,
    ProjectProvisioningRepositoryIntent,
    ProjectRecord,
    ProjectTransferRepositorySource,
    ProjectTransferSourceConfiguration,
)
from rcp.storage.provisioning import project_transfer_source_configuration_sha256


def _actor(store: AppStore, name: str) -> AuthorizedHuman:
    if store.space_kind == "personal":
        owner = store.local_owner
        assert owner is not None
        member = store.rename_space_user(owner.user_id, name)
    else:
        member = store.preprovision_team_member(name)
    return AuthorizedHuman(
        space_id=store.space_id,
        user_id=member.user_id,
        display_name=name,
    )


def _source_configuration(**changes: object) -> ProjectTransferSourceConfiguration:
    values: dict[str, object] = {
        "source_rcp_version": "0.1.0.dev0+main",
        "source_schema_generation": 1,
        "supported_archive_codecs": ("rcp-transfer-v1", "rcp-transfer-v2"),
        "machine_aliases": ("laptop",),
        "repositories": (
            ProjectTransferRepositorySource(
                alias="paper",
                repository=parse_github_repository_ref("git@github.com:OpenAI/RCP.git"),
                machine_alias="laptop",
            ),
        ),
        "state_repository": "paper",
        "project_truth_scope": ("paper",),
        "default_run_truth_scope": ("paper",),
        "source_manifest_sha256": "a" * 64,
    }
    values.update(changes)
    return ProjectTransferSourceConfiguration.model_validate(values)


def _project(store: AppStore, project_id: str) -> ProjectRecord:
    return store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            home_space_id=store.space_id,
            locator=f"/tmp/{project_id}/research.yaml",
            name="Transfer project",
            state_location=f"/tmp/{project_id}/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )


def _machine() -> ProjectProvisioningMachineIntent:
    return ProjectProvisioningMachineIntent(
        alias="server",
        location="local",
        os_account="rcp",
        central_root=str(DEFAULT_SERVER_LAYOUT.projects_root),
    )


def _repository() -> ProjectProvisioningRepositoryIntent:
    return ProjectProvisioningRepositoryIntent(
        alias="paper",
        repository=parse_github_repository_ref("https://github.com/openai/rcp.git"),
        machine_alias="server",
    )


def _provider() -> ProjectProvisioningProviderIntent:
    return ProjectProvisioningProviderIntent(
        profile="seed",
        provider="codex",
        runtime_id="codex:exec",
        model="gpt-5.6-luna",
        reasoning="medium",
        machine_alias="server",
    )


def _incoming_request(
    target: AppStore,
    target_actor: AuthorizedHuman,
    project_id: str,
):
    return target.create_project_provisioning_request(
        kind="incoming_transfer",
        authorized_by=target_actor,
        machines=[_machine()],
        repositories=[_repository()],
        provider_checks=[_provider()],
        source_project_id=project_id,
        name="Transfer project",
        state_repository="paper",
        project_truth_scope=["paper"],
        default_run_truth_scope=["paper"],
    )


def _ready_incoming(target: AppStore, request_id: str):
    request = target.project_provisioning_request(request_id)
    assert request is not None
    running = target.transition_project_provisioning_request(
        request_id,
        receipt_id="setup-started",
        phase="setup_start",
        expected_revision=0,
        expected_status="waiting_for_server_setup",
        to_status="setup_in_progress",
        machines=request.machines,
        repositories=request.repositories,
        provider_checks=request.provider_checks,
    )
    checked_at = target.now()
    machines = [
        running.machines[0].model_copy(
            update={"resolved_central_root": running.machines[0].central_root}
        )
    ]
    repositories = [
        running.repositories[0].model_copy(
            update={
                "resolved_path": running.repositories[0].intended_path,
                "checkout_disposition": "request_created",
                "git_check": ProjectProvisioningGitCheckRecord(
                    status="ready",
                    commit="b" * 40,
                    write_verified=True,
                    deploy_key_label=(f"rcp:{target.space_id}:{request.proposed_project_id}:paper"),
                    public_key_fingerprint="SHA256:" + ("B" * 43),
                    checked_at=checked_at,
                ),
            }
        )
    ]
    providers = [
        ProjectProvisioningProviderCheckRecord(
            **_provider().model_dump(mode="json"),
            status="ready",
            checked_at=checked_at,
        )
    ]
    return target.transition_project_provisioning_request(
        request_id,
        receipt_id="preparation-ready",
        phase="final_review",
        expected_revision=1,
        expected_status="setup_in_progress",
        to_status="ready_for_review",
        machines=machines,
        repositories=repositories,
        provider_checks=providers,
    )


def _linked_pair(tmp_path: Path):
    source = AppStore(tmp_path / "personal" / "rcp.sqlite3", space_kind="personal")
    target = AppStore(tmp_path / "team" / "rcp.sqlite3", space_kind="team")
    source_actor = _actor(source, "Z")
    target_actor = _actor(target, "Alice")
    project_id = str(uuid.uuid4())
    _project(source, project_id)
    configuration = _source_configuration()
    source_request = source.create_source_project_transfer_request(
        project_id=project_id,
        target_space_id=target.space_id,
        initiated_by=source_actor,
        source_configuration=configuration,
    )
    incoming = _incoming_request(target, target_actor, project_id)
    target_request = target.create_target_project_transfer_request(
        provisioning_request_id=incoming.request_id,
        source_request_id=source_request.request_id,
        source_project_id=source_request.project_id,
        source_space_id=source.space_id,
        initiated_by=target_actor,
        source_configuration=configuration,
        source_configuration_sha256=source_request.source_configuration_sha256,
        source_release_proof_sha256=source_request.source_release_proof_sha256,
        accepted_schema_generation=configuration.source_schema_generation,
        accepted_archive_codec="rcp-transfer-v1",
    )
    assert target_request.link_receipt is not None
    source_request = source.link_source_project_transfer_request(
        source_request.request_id,
        receipt=target_request.link_receipt,
    )
    return (
        source,
        target,
        source_actor,
        target_actor,
        configuration,
        source_request,
        target_request,
    )


def _released_pair(tmp_path: Path):
    (
        source,
        target,
        source_actor,
        target_actor,
        configuration,
        source_request,
        target_request,
    ) = _linked_pair(tmp_path)
    _ready_incoming(target, target_request.request_id)
    target_request = target.record_target_project_transfer_admission(
        target_request.request_id,
        admitted_by=target_actor,
    )
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
    return source, target, source_request, target_request


def test_source_release_atomically_fences_new_root_task_admission(tmp_path: Path) -> None:
    source, _target, source_request, _target_request = _released_pair(tmp_path)
    now = source.now()
    task = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=source_request.project_id,
        kind="refresh",
        status="queued",
        request={},
        created_at=now,
        updated_at=now,
        status_message="Waiting to refresh.",
    )

    with pytest.raises(ValueError, match="moving to its admitted team space"):
        source.create_agent_task(task)
    assert source.agent_task(task.operation_id) is None


def test_linked_requests_keep_independent_raw_proofs_out_of_public_state(tmp_path: Path) -> None:
    (
        source,
        target,
        source_actor,
        target_actor,
        configuration,
        source_request,
        target_request,
    ) = _linked_pair(tmp_path)

    assert source_actor.user_id != target_actor.user_id
    assert source_request.phase == target_request.phase == "linked"
    assert source_request.linked_request_id == target_request.request_id
    assert target_request.linked_request_id == source_request.request_id
    assert source_request.source_configuration == configuration
    assert source_request.source_configuration_sha256 == (
        project_transfer_source_configuration_sha256(configuration)
    )
    assert source_request.source_release_proof_sha256 == (
        target_request.source_release_proof_sha256
    )
    assert source_request.target_activation_proof_sha256 == (
        target_request.target_activation_proof_sha256
    )
    assert source_request.source_release_proof_sha256 != (
        target_request.target_activation_proof_sha256
    )

    for store, request in ((source, source_request), (target, target_request)):
        with sqlite3.connect(store.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT record_json FROM project_transfer_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            proof = connection.execute(
                "SELECT secret, commitment_sha256 FROM project_transfer_proofs "
                "WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
        assert row is not None and proof is not None
        public = json.loads(row["record_json"])
        assert "secret" not in public
        assert len(proof["secret"]) == 32
        assert hashlib.sha256(proof["secret"]).hexdigest() == proof["commitment_sha256"]
        with pytest.raises(ValueError, match="not exposed"):
            store.expose_project_transfer_proof(request.request_id)

    assert AppStore(source.path).project_transfer_request(source_request.request_id) == (
        source_request
    )
    assert AppStore(target.path, space_kind="team").project_transfer_requests(side="target") == [
        target_request
    ]


def test_link_creation_is_exactly_idempotent_after_the_request_advances(tmp_path: Path) -> None:
    (
        source,
        target,
        source_actor,
        target_actor,
        configuration,
        source_request,
        target_request,
    ) = _linked_pair(tmp_path)

    repeated_source = source.create_source_project_transfer_request(
        request_id=source_request.request_id,
        project_id=source_request.project_id,
        target_space_id=target.space_id,
        initiated_by=source_actor,
        source_configuration=configuration,
    )
    repeated_target = target.create_target_project_transfer_request(
        provisioning_request_id=target_request.request_id,
        source_request_id=source_request.request_id,
        source_project_id=source_request.project_id,
        source_space_id=source.space_id,
        initiated_by=target_actor,
        source_configuration=configuration,
        source_configuration_sha256=source_request.source_configuration_sha256,
        source_release_proof_sha256=source_request.source_release_proof_sha256,
        accepted_schema_generation=1,
        accepted_archive_codec="rcp-transfer-v1",
    )
    assert target_request.link_receipt is not None
    repeated_link = source.link_source_project_transfer_request(
        source_request.request_id,
        receipt=target_request.link_receipt,
    )

    assert repeated_source == source_request
    assert repeated_target == target_request
    assert repeated_link == source_request
    assert len(source.project_transfer_requests()) == 1
    assert len(target.project_transfer_requests()) == 1
    with pytest.raises(ValueError, match="does not match"):
        source.link_source_project_transfer_request(
            source_request.request_id,
            receipt=target_request.link_receipt.model_copy(
                update={"target_request_id": str(uuid.uuid4())}
            ),
        )


def test_no_common_codec_or_stale_source_identity_fails_before_linking(tmp_path: Path) -> None:
    source = AppStore(tmp_path / "personal" / "rcp.sqlite3", space_kind="personal")
    target = AppStore(tmp_path / "team" / "rcp.sqlite3", space_kind="team")
    source_actor = _actor(source, "Z")
    target_actor = _actor(target, "Alice")
    project_id = str(uuid.uuid4())
    _project(source, project_id)
    configuration = _source_configuration()
    source_request = source.create_source_project_transfer_request(
        project_id=project_id,
        target_space_id=target.space_id,
        initiated_by=source_actor,
        source_configuration=configuration,
    )
    incoming = _incoming_request(target, target_actor, project_id)

    with pytest.raises(ValueError, match="did not offer"):
        target.create_target_project_transfer_request(
            provisioning_request_id=incoming.request_id,
            source_request_id=source_request.request_id,
            source_project_id=source_request.project_id,
            source_space_id=source.space_id,
            initiated_by=target_actor,
            source_configuration=configuration,
            source_configuration_sha256=source_request.source_configuration_sha256,
            source_release_proof_sha256=source_request.source_release_proof_sha256,
            accepted_schema_generation=1,
            accepted_archive_codec="rcp-transfer-v9",
        )
    assert target.project_transfer_requests() == []

    with pytest.raises(ValueError, match="does not match its incoming"):
        target.create_target_project_transfer_request(
            provisioning_request_id=incoming.request_id,
            source_request_id=source_request.request_id,
            source_project_id=str(uuid.uuid4()),
            source_space_id=source.space_id,
            initiated_by=target_actor,
            source_configuration=configuration,
            source_configuration_sha256=source_request.source_configuration_sha256,
            source_release_proof_sha256=source_request.source_release_proof_sha256,
            accepted_schema_generation=1,
            accepted_archive_codec="rcp-transfer-v1",
        )

    mismatched_repository = ProjectProvisioningRepositoryIntent(
        alias="paper",
        repository=parse_github_repository_ref("https://github.com/openai/other.git"),
        machine_alias="server",
    )
    other_target = AppStore(tmp_path / "other-team" / "rcp.sqlite3", space_kind="team")
    other_target_actor = _actor(other_target, "Alice")
    mismatched_incoming = other_target.create_project_provisioning_request(
        kind="incoming_transfer",
        authorized_by=other_target_actor,
        machines=[_machine()],
        repositories=[mismatched_repository],
        provider_checks=[_provider()],
        source_project_id=project_id,
        name="Transfer project",
        state_repository="paper",
        project_truth_scope=["paper"],
        default_run_truth_scope=["paper"],
    )
    with pytest.raises(ValueError, match="repositories do not match"):
        other_target.create_target_project_transfer_request(
            provisioning_request_id=mismatched_incoming.request_id,
            source_request_id=source_request.request_id,
            source_project_id=source_request.project_id,
            source_space_id=source.space_id,
            initiated_by=other_target_actor,
            source_configuration=configuration,
            source_configuration_sha256=source_request.source_configuration_sha256,
            source_release_proof_sha256=source_request.source_release_proof_sha256,
            accepted_schema_generation=1,
            accepted_archive_codec="rcp-transfer-v1",
        )

    stale_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="stale or belongs"):
        source.create_source_project_transfer_request(
            project_id=stale_id,
            target_space_id=target.space_id,
            initiated_by=source_actor,
            source_configuration=configuration,
        )


def test_both_human_receipts_bind_the_exact_review_without_creating_target_authority(
    tmp_path: Path,
) -> None:
    (
        source,
        target,
        source_actor,
        target_actor,
        configuration,
        source_request,
        target_request,
    ) = _linked_pair(tmp_path)
    ready = _ready_incoming(target, target_request.request_id)

    target_request = target.record_target_project_transfer_admission(
        target_request.request_id,
        admitted_by=target_actor,
    )
    assert target_request.phase == "target_admitted"
    assert target_request.target_admission_receipt is not None
    assert target_request.target_admission_receipt.target_preparation_revision == ready.revision
    assert target_request.target_admission_receipt.target_preparation_sha256 == (
        ready.final_review_digest
    )
    assert target.project(target_request.project_id) is None
    assert (
        target.record_target_project_transfer_admission(
            target_request.request_id,
            admitted_by=target_actor,
        )
        == target_request
    )

    forged = target_request.target_admission_receipt.model_copy(
        update={"source_configuration_sha256": "d" * 64}
    )
    with pytest.raises(ValueError, match="does not match"):
        source.accept_target_project_transfer_admission(
            source_request.request_id,
            receipt=forged,
        )
    source_request = source.accept_target_project_transfer_admission(
        source_request.request_id,
        receipt=target_request.target_admission_receipt,
    )
    drifted = configuration.model_copy(update={"source_manifest_sha256": "e" * 64})
    with pytest.raises(ValueError, match="changed after"):
        source.record_source_project_transfer_release(
            source_request.request_id,
            released_by=source_actor,
            revalidated_configuration=drifted,
            source_head=GraphHeadRef(revision=7, transition_id="c" * 64),
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
    assert source_request.phase == target_request.phase == "source_released"
    assert source_request.source_release_receipt.released_by == source_actor
    assert target_request.target_admission_receipt.admitted_by == target_actor
    assert target.project(target_request.project_id) is None


def test_proofs_expose_only_at_their_boundaries_then_consume_to_receipts(
    tmp_path: Path,
) -> None:
    source, target, source_request, target_request = _released_pair(tmp_path)
    assert source_request.source_release_receipt is not None
    fenced_head = GraphHeadRef(revision=8, transition_id="d" * 64)
    source_request = source.mark_source_project_transfer_fenced(
        source_request.request_id,
        source_head=fenced_head,
    )
    source_secret = source.expose_project_transfer_proof(source_request.request_id)
    assert source.expose_project_transfer_proof(source_request.request_id) == source_secret
    assert hashlib.sha256(source_secret).hexdigest() == (source_request.source_release_proof_sha256)
    source_ack = hashlib.sha256(b"target verified source release proof").hexdigest()
    with pytest.raises(ValueError, match="before its boundary"):
        source.acknowledge_project_transfer_proof(
            source_request.request_id,
            acknowledgement_sha256=source_ack,
        )

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
    target_request = target.mark_target_project_transfer_activated(target_request.request_id)
    target_secret = target.expose_project_transfer_proof(target_request.request_id)
    assert target.expose_project_transfer_proof(target_request.request_id) == target_secret
    assert hashlib.sha256(target_secret).hexdigest() == (
        target_request.target_activation_proof_sha256
    )

    target_ack = hashlib.sha256(b"source verified target activation proof").hexdigest()
    source.acknowledge_project_transfer_proof(
        source_request.request_id,
        acknowledgement_sha256=source_ack,
    )
    with pytest.raises(ValueError, match="before cleanup"):
        source.consume_project_transfer_proof(
            source_request.request_id,
            acknowledgement_sha256=source_ack,
        )
    source.acknowledge_project_transfer_cleanup(source_request.request_id)
    source.consume_project_transfer_proof(
        source_request.request_id,
        acknowledgement_sha256=source_ack,
    )
    source_request = source.complete_project_transfer_request(source_request.request_id)

    target.acknowledge_project_transfer_proof(
        target_request.request_id,
        acknowledgement_sha256=target_ack,
    )
    target.acknowledge_project_transfer_cleanup(target_request.request_id)
    target.consume_project_transfer_proof(
        target_request.request_id,
        acknowledgement_sha256=target_ack,
    )
    target_request = target.complete_project_transfer_request(target_request.request_id)

    assert source_request.phase == target_request.phase == "completed"
    assert source_request.proof_state == target_request.proof_state == "consumed"
    for store, request, acknowledgment in (
        (source, source_request, source_ack),
        (target, target_request, target_ack),
    ):
        with sqlite3.connect(store.path) as connection:
            connection.row_factory = sqlite3.Row
            proof = connection.execute(
                "SELECT * FROM project_transfer_proofs WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
        assert proof is not None
        assert proof["secret"] is None
        assert proof["commitment_sha256"] in {
            request.source_release_proof_sha256,
            request.target_activation_proof_sha256,
        }
        assert proof["acknowledgement_sha256"] == acknowledgment
        with pytest.raises(ValueError, match="already consumed"):
            store.expose_project_transfer_proof(request.request_id)
        assert store.complete_project_transfer_request(request.request_id) == request


def test_archive_and_proof_retries_reject_different_boundaries(tmp_path: Path) -> None:
    source, target, source_request, target_request = _released_pair(tmp_path)
    assert source_request.source_release_receipt is not None
    fenced_head = GraphHeadRef(revision=8, transition_id="d" * 64)
    source.mark_source_project_transfer_fenced(
        source_request.request_id,
        source_head=fenced_head,
    )
    source.expose_project_transfer_proof(source_request.request_id)
    archive = "f" * 64
    bound = source.bind_project_transfer_archive(
        source_request.request_id,
        archive_sha256=archive,
        archive_size_bytes=100,
    )
    assert (
        source.bind_project_transfer_archive(
            source_request.request_id,
            archive_sha256=archive,
            archive_size_bytes=100,
        )
        == bound
    )
    with pytest.raises(ValueError, match="another archive"):
        source.bind_project_transfer_archive(
            source_request.request_id,
            archive_sha256="0" * 64,
            archive_size_bytes=100,
        )
    with pytest.raises(ValueError, match="not ready to activate"):
        target.mark_target_project_transfer_activated(target_request.request_id)


def test_corrupt_public_or_protected_transfer_state_fails_loudly(tmp_path: Path) -> None:
    source, _target, _source_actor, _target_actor, _config, source_request, _target_request = (
        _linked_pair(tmp_path)
    )
    with sqlite3.connect(source.path) as connection:
        connection.execute(
            "UPDATE project_transfer_proofs SET commitment_sha256 = ? WHERE request_id = ?",
            ("0" * 64, source_request.request_id),
        )
    with pytest.raises(RuntimeError, match="does not match"):
        source.project_transfer_request(source_request.request_id)
