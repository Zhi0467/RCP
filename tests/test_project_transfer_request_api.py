from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from rcp import __version__
from rcp.api import create_app
from rcp.config import AGENT_EXECUTION_PROFILES
from rcp.core.transition_models import GraphHeadRef
from rcp.server_ops.github import parse_github_repository_ref
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
from rcp.setup import ProjectSetupRequest, SetupRepository
from rcp.storage import (
    AppStore,
    ProjectProvisioningGitCheckRecord,
    ProjectProvisioningMachineIntent,
    ProjectProvisioningProviderCheckRecord,
    ProjectProvisioningProviderIntent,
    ProjectTransferRepositorySource,
    ProjectTransferSourceConfiguration,
)

from .helpers import create_named_app

_GIT = "/Library/Developer/CommandLineTools/usr/bin/git"


def _source_configuration() -> ProjectTransferSourceConfiguration:
    return ProjectTransferSourceConfiguration(
        source_rcp_version="0.1.0.dev0+main",
        source_schema_generation=1,
        supported_archive_codecs=("rcp-transfer-v1",),
        machine_aliases=("laptop",),
        repositories=(
            ProjectTransferRepositorySource(
                alias="paper",
                repository=parse_github_repository_ref("git@github.com:OpenAI/RCP.git"),
                machine_alias="laptop",
            ),
        ),
        state_repository="paper",
        project_truth_scope=("paper",),
        default_run_truth_scope=("paper",),
        source_manifest_sha256="a" * 64,
    )


def _incoming_payload(project_id: str, *, request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "source_project_id": project_id,
        "name": "Transfer project",
        "state_repository": "paper",
        "project_truth_scope": ["paper"],
        "default_run_truth_scope": ["paper"],
        "machines": [
            ProjectProvisioningMachineIntent(
                alias="server",
                location="local",
                os_account="rcp",
                central_root=str(DEFAULT_SERVER_LAYOUT.projects_root),
            ).model_dump(mode="json")
        ],
        "repositories": [
            {
                "alias": "paper",
                "source": "https://github.com/openai/rcp.git",
                "machine_alias": "server",
            }
        ],
        "provider_checks": [
            ProjectProvisioningProviderIntent(
                profile=profile,
                provider="codex",
                runtime_id="codex:exec",
                model="gpt-5.6-luna",
                reasoning="medium",
                machine_alias="server",
            ).model_dump(mode="json")
            for profile in AGENT_EXECUTION_PROFILES
        ],
    }


def _ready_incoming(store: AppStore, request_id: str) -> None:
    request = store.project_provisioning_request(request_id)
    assert request is not None
    running = store.transition_project_provisioning_request(
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
    checked_at = store.now()
    repositories = [
        running.repositories[0].model_copy(
            update={
                "resolved_path": running.repositories[0].intended_path,
                "checkout_disposition": "request_created",
                "git_check": ProjectProvisioningGitCheckRecord(
                    status="ready",
                    commit="b" * 40,
                    write_verified=True,
                    deploy_key_label=(f"rcp:{store.space_id}:{request.proposed_project_id}:paper"),
                    public_key_fingerprint="SHA256:" + ("B" * 43),
                    checked_at=checked_at,
                ),
            }
        )
    ]
    store.transition_project_provisioning_request(
        request_id,
        receipt_id="preparation-ready",
        phase="final_review",
        expected_revision=1,
        expected_status="setup_in_progress",
        to_status="ready_for_review",
        machines=[
            running.machines[0].model_copy(
                update={"resolved_central_root": running.machines[0].central_root}
            )
        ],
        repositories=repositories,
        provider_checks=[
            ProjectProvisioningProviderCheckRecord.model_validate(
                {
                    **provider.model_dump(mode="json"),
                    "status": "ready",
                    "binary_path": "/usr/local/bin/codex",
                    "version": "codex-cli 1.0",
                    "resolved_runtime_id": "codex:exec",
                    "execution_account": "rcp",
                    "checked_at": checked_at,
                }
            )
            for provider in running.provider_checks
        ],
    )


def _team_app(tmp_path: Path):
    data_dir = tmp_path / "team"
    store, bootstrap = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Team Lab")
    alice, alice_token = store.enroll_team_member(bootstrap, "Alice")
    _invitation, invitation_code = store.create_team_invitation(alice.user_id)
    bob, bob_token = store.enroll_team_member(invitation_code, "Bob")
    return data_dir, alice, alice_token, bob, bob_token, create_app(data_dir=data_dir)


def _source_project(app, root: Path) -> str:
    repository = root / "paper"
    repository.mkdir(parents=True)
    subprocess.run([_GIT, "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        [
            _GIT,
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            "git@github.com:OpenAI/RCP.git",
        ],
        check=True,
        capture_output=True,
    )
    card = app.state.setup.create(
        ProjectSetupRequest(
            name="Transfer project",
            repositories=[
                SetupRepository(
                    alias="paper",
                    location="local",
                    path=str(repository),
                    default_read=True,
                )
            ],
            state_repository="paper",
            confirmed=True,
        )
    )
    return str(card["id"])


def _set_origin(repository: Path, origin: str) -> None:
    subprocess.run(
        [_GIT, "-C", str(repository), "remote", "set-url", "origin", origin],
        check=True,
        capture_output=True,
    )


def test_authenticated_transfer_apis_link_confirm_and_keep_raw_proofs_native(
    tmp_path: Path,
) -> None:
    source_data = tmp_path / "personal"
    source_app = create_named_app(data_dir=source_data)
    source_store = source_app.state.background_tasks.store
    project_id = _source_project(source_app, source_data)

    team_data, alice, alice_token, bob, bob_token, team_app = _team_app(tmp_path)
    team_store = team_app.state.background_tasks.store
    with (
        TestClient(source_app, base_url="https://personal.test") as source_client,
        TestClient(team_app, base_url="https://team.test") as team_client,
    ):
        assert (
            team_client.post(
                "/api/team/session/exchange",
                json={"token": alice_token},
            ).status_code
            == 200
        )
        incoming_created = team_client.post(
            "/api/project-transfers/incoming-provisioning-requests",
            json=_incoming_payload(project_id, request_id=str(uuid.uuid4())),
        )
        assert incoming_created.status_code == 201
        incoming = incoming_created.json()
        assert (
            team_client.post(
                "/api/project-transfers/incoming-provisioning-requests",
                json=_incoming_payload(project_id, request_id=incoming["request_id"]),
            ).json()
            == incoming
        )
        changed_incoming = _incoming_payload(project_id, request_id=incoming["request_id"])
        changed_incoming["name"] = "Another transfer"
        assert (
            team_client.post(
                "/api/project-transfers/incoming-provisioning-requests",
                json=changed_incoming,
            ).status_code
            == 409
        )
        assert incoming["kind"] == "incoming_transfer"
        assert [
            item["request_id"]
            for item in team_client.get(
                "/api/project-transfers/incoming-provisioning-requests"
            ).json()
        ] == [incoming["request_id"]]
        assert (
            team_client.get(
                f"/api/project-transfers/incoming-provisioning-requests/{incoming['request_id']}"
            ).json()["proposed_project_id"]
            == project_id
        )
        source_created = source_client.post(
            "/api/project-transfers/source-requests",
            json={
                "request_id": str(uuid.uuid4()),
                "project_id": project_id,
                "target_space_id": team_store.space_id,
            },
        )
        assert source_created.status_code == 201
        source_request = source_created.json()
        assert (
            source_client.post(
                "/api/project-transfers/source-requests",
                json={
                    "request_id": source_request["request_id"],
                    "project_id": project_id,
                    "target_space_id": team_store.space_id,
                    "expected_source_configuration_sha256": source_request[
                        "source_configuration_sha256"
                    ],
                },
            ).json()
            == source_request
        )
        configuration = source_request["source_configuration"]
        assert configuration["source_rcp_version"] == __version__
        source_record = source_store.project(project_id)
        assert source_record is not None
        assert (
            configuration["source_manifest_sha256"]
            == hashlib.sha256(
                (Path(source_record.state_location) / "manifest.toml").read_bytes()
            ).hexdigest()
        )
        _set_origin(
            Path(source_record.state_location).parent,
            "https://github.com/openai/another.git",
        )
        assert (
            source_client.post(
                "/api/project-transfers/source-requests",
                json={
                    "request_id": source_request["request_id"],
                    "project_id": project_id,
                    "target_space_id": team_store.space_id,
                },
            ).json()
            == source_request
        )
        _set_origin(
            Path(source_record.state_location).parent,
            "git@github.com:OpenAI/RCP.git",
        )
        assert (
            source_client.post(
                "/api/project-transfers/source-requests",
                json={
                    "request_id": source_request["request_id"],
                    "project_id": project_id,
                    "target_space_id": str(uuid.uuid4()),
                },
            ).status_code
            == 409
        )

        target_created = team_client.post(
            "/api/project-transfers/target-requests",
            json={
                "provisioning_request_id": incoming["request_id"],
                "source_request_id": source_request["request_id"],
                "source_project_id": project_id,
                "source_space_id": source_store.space_id,
                "source_configuration": configuration,
                "source_configuration_sha256": source_request["source_configuration_sha256"],
                "source_release_proof_sha256": source_request["source_release_proof_sha256"],
                "accepted_schema_generation": 1,
                "accepted_archive_codec": "rcp-transfer-v1",
            },
        )
        assert target_created.status_code == 201
        target_request = target_created.json()
        assert (
            team_client.post(
                "/api/project-transfers/target-requests",
                json={
                    "provisioning_request_id": incoming["request_id"],
                    "source_request_id": source_request["request_id"],
                    "source_project_id": project_id,
                    "source_space_id": source_store.space_id,
                    "source_configuration": configuration,
                    "source_configuration_sha256": source_request["source_configuration_sha256"],
                    "source_release_proof_sha256": source_request["source_release_proof_sha256"],
                    "accepted_schema_generation": 1,
                    "accepted_archive_codec": "rcp-transfer-v1",
                },
            ).json()
            == target_request
        )
        assert target_request["link_receipt"]["project_id"] == project_id
        assert target_request["link_receipt"]["target_repositories"] == [
            {"alias": "paper", "repository": {"identity": "openai/rcp"}}
        ]

        linked = source_client.post(
            f"/api/project-transfers/source-requests/{source_request['request_id']}/link",
            json={"receipt": target_request["link_receipt"]},
        )
        assert linked.status_code == 200
        assert linked.json()["phase"] == "linked"
        assert (
            source_client.get("/api/project-transfers/requests").json()[0]["request_id"]
            == source_request["request_id"]
        )
        assert (
            team_client.get(
                f"/api/project-transfers/requests/{target_request['request_id']}"
            ).json()["linked_request_id"]
            == source_request["request_id"]
        )

        _ready_incoming(team_store, target_request["request_id"])
        admitted = team_client.post(
            f"/api/project-transfers/target-requests/{target_request['request_id']}/admit",
            json={},
        )
        assert admitted.status_code == 200
        target_request = admitted.json()
        assert target_request["target_admission_receipt"]["admitted_by"]["user_id"] == (
            alice.user_id
        )
        accepted_admission = source_client.post(
            "/api/project-transfers/source-requests/"
            f"{source_request['request_id']}/target-admission",
            json={"receipt": target_request["target_admission_receipt"]},
        )
        assert accepted_admission.status_code == 200

        source_head = source_app.state.catalog.open(project_id).history.head_ref()
        stale_configuration = source_client.post(
            f"/api/project-transfers/source-requests/{source_request['request_id']}/release",
            json={
                "expected_source_configuration_sha256": "f" * 64,
                "expected_source_head": source_head.model_dump(mode="json"),
            },
        )
        assert stale_configuration.status_code == 409
        stale_head = source_client.post(
            f"/api/project-transfers/source-requests/{source_request['request_id']}/release",
            json={
                "expected_source_configuration_sha256": source_request[
                    "source_configuration_sha256"
                ],
                "expected_source_head": GraphHeadRef(
                    revision=source_head.revision + 1,
                    transition_id="c" * 64,
                ).model_dump(mode="json"),
            },
        )
        assert stale_head.status_code == 409
        released = source_client.post(
            f"/api/project-transfers/source-requests/{source_request['request_id']}/release",
            json={
                "expected_source_configuration_sha256": source_request[
                    "source_configuration_sha256"
                ],
                "expected_source_head": source_head.model_dump(mode="json"),
            },
        )
        assert released.status_code == 200
        source_request = released.json()
        _set_origin(
            Path(source_record.state_location).parent,
            "https://github.com/openai/another.git",
        )
        assert (
            source_client.post(
                f"/api/project-transfers/source-requests/{source_request['request_id']}/release",
                json={
                    "expected_source_configuration_sha256": source_request[
                        "source_configuration_sha256"
                    ],
                    "expected_source_head": source_head.model_dump(mode="json"),
                },
            ).json()
            == source_request
        )
        _set_origin(
            Path(source_record.state_location).parent,
            "git@github.com:OpenAI/RCP.git",
        )
        accepted_release = team_client.post(
            f"/api/project-transfers/target-requests/{target_request['request_id']}/source-release",
            json={"receipt": source_request["source_release_receipt"]},
        )
        assert accepted_release.status_code == 200
        target_request = accepted_release.json()

        native_path = (
            "/api/native/project-transfers/target-requests/"
            f"{target_request['request_id']}/activation-proof"
        )
        cookie_only = team_client.get(native_path)
        assert cookie_only.status_code == 401
        assert cookie_only.json()["detail"]["code"] == "team_token_required"
        with TestClient(team_app, base_url="https://team.test") as native_client:
            premature = native_client.get(
                native_path,
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            assert premature.status_code == 409

        fenced_head = GraphHeadRef(
            revision=source_head.revision + 1,
            transition_id="d" * 64,
        )
        source_store.mark_source_project_transfer_fenced(
            source_request["request_id"],
            source_head=fenced_head,
        )
        source_secret = source_store.expose_project_transfer_proof(source_request["request_id"])
        archive_sha256 = hashlib.sha256(b"sealed transfer archive").hexdigest()
        source_bound = source_client.post(
            f"/api/project-transfers/requests/{source_request['request_id']}/archive",
            json={
                "archive_sha256": archive_sha256,
                "archive_size_bytes": 23,
            },
        )
        assert source_bound.status_code == 200
        with TestClient(team_app, base_url="https://team.test") as bob_client:
            assert (
                bob_client.post(
                    "/api/team/session/exchange",
                    json={"token": bob_token},
                ).status_code
                == 200
            )
            poisoned = bob_client.post(
                f"/api/project-transfers/requests/{target_request['request_id']}/archive",
                json={
                    "archive_sha256": "e" * 64,
                    "archive_size_bytes": 9,
                    "source_fence_head": fenced_head.model_dump(mode="json"),
                },
            )
        assert poisoned.status_code == 403
        target_bound = team_client.post(
            f"/api/project-transfers/requests/{target_request['request_id']}/archive",
            json={
                "archive_sha256": archive_sha256,
                "archive_size_bytes": 23,
                "source_fence_head": fenced_head.model_dump(mode="json"),
            },
        )
        assert target_bound.status_code == 200
        team_store.mark_target_project_transfer_activated(target_request["request_id"])
        session_count_before = _session_count(team_data / "rcp.sqlite3")

        with TestClient(team_app, base_url="https://team.test") as native_client:
            wrong_member = native_client.get(
                native_path,
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            assert wrong_member.status_code == 403
            retrieved = native_client.get(
                native_path,
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            repeated = native_client.get(
                native_path,
                headers={"Authorization": f"Bearer {alice_token}"},
            )
        assert retrieved.status_code == repeated.status_code == 200
        assert retrieved.content == repeated.content
        assert len(retrieved.content) == 32
        assert retrieved.headers["content-type"] == "application/octet-stream"
        assert retrieved.headers["cache-control"] == "no-store"
        assert (
            hashlib.sha256(retrieved.content).hexdigest()
            == (target_request["target_activation_proof_sha256"])
        )
        assert _session_count(team_data / "rcp.sqlite3") == session_count_before

        public_state = json.dumps(
            {
                "source": source_client.get(
                    f"/api/project-transfers/requests/{source_request['request_id']}"
                ).json(),
                "target": team_client.get(
                    f"/api/project-transfers/requests/{target_request['request_id']}"
                ).json(),
            },
            sort_keys=True,
        )
        assert source_secret.hex() not in public_state
        assert retrieved.content.hex() not in public_state

        wrong_proof = source_client.post(
            "/api/native/project-transfers/source-requests/"
            f"{source_request['request_id']}/target-activation-proof",
            content=b"wrong proof".ljust(32, b"!"),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert wrong_proof.status_code == 409
        verified = source_client.post(
            "/api/native/project-transfers/source-requests/"
            f"{source_request['request_id']}/target-activation-proof",
            content=retrieved.content,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert verified.status_code == 200
        acknowledgment = verified.json()
        repeated_verification = source_client.post(
            "/api/native/project-transfers/source-requests/"
            f"{source_request['request_id']}/target-activation-proof",
            content=retrieved.content,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert repeated_verification.json() == acknowledgment
        cleanup_path = (
            "/api/native/project-transfers/target-requests/"
            f"{target_request['request_id']}/cleanup-acknowledgment"
        )
        cookie_only_cleanup = team_client.post(
            cleanup_path,
            json={"acknowledgment": acknowledgment},
        )
        assert cookie_only_cleanup.status_code == 401
        with TestClient(team_app, base_url="https://team.test") as native_client:
            forged = dict(acknowledgment)
            forged["archive_sha256"] = "f" * 64
            forged_cleanup = native_client.post(
                cleanup_path,
                json={"acknowledgment": forged},
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            wrong_confirmer_cleanup = native_client.post(
                cleanup_path,
                json={"acknowledgment": acknowledgment},
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            cleaned = native_client.post(
                cleanup_path,
                json={"acknowledgment": acknowledgment},
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            repeated_cleanup = native_client.post(
                cleanup_path,
                json={"acknowledgment": acknowledgment},
                headers={"Authorization": f"Bearer {alice_token}"},
            )
        assert forged_cleanup.status_code == 409
        assert wrong_confirmer_cleanup.status_code == 403
        assert cleaned.status_code == repeated_cleanup.status_code == 200
        assert cleaned.json() == repeated_cleanup.json()
        assert cleaned.json()["phase"] == "completed"
        assert cleaned.json()["proof_state"] == "consumed"
        team_store.revoke_team_token(bob.user_id)
        with TestClient(team_app, base_url="https://team.test") as native_client:
            revoked = native_client.get(
                native_path,
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            consumed = native_client.get(
                native_path,
                headers={"Authorization": f"Bearer {alice_token}"},
            )
        assert revoked.status_code == 401
        assert consumed.status_code == 409


def test_transfer_coordination_routes_keep_space_and_session_boundaries(tmp_path: Path) -> None:
    source_app = create_named_app(data_dir=tmp_path / "personal")
    _team_data, _alice, alice_token, _bob, _bob_token, team_app = _team_app(tmp_path)
    with (
        TestClient(source_app, base_url="https://personal.test") as source_client,
        TestClient(team_app, base_url="https://team.test") as team_client,
    ):
        unauthenticated = team_client.get("/api/project-transfers/requests")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["detail"]["code"] == "team_identity_required"
        assert (
            team_client.post(
                "/api/team/session/exchange",
                json={"token": alice_token},
            ).status_code
            == 200
        )
        configuration = _source_configuration().model_dump(mode="json")
        wrong_source = team_client.post(
            "/api/project-transfers/source-requests",
            json={
                "request_id": str(uuid.uuid4()),
                "project_id": str(uuid.uuid4()),
                "target_space_id": str(uuid.uuid4()),
            },
        )
        wrong_target = source_client.post(
            "/api/project-transfers/target-requests",
            json={
                "provisioning_request_id": str(uuid.uuid4()),
                "source_request_id": str(uuid.uuid4()),
                "source_project_id": str(uuid.uuid4()),
                "source_space_id": str(uuid.uuid4()),
                "source_configuration": configuration,
                "source_configuration_sha256": "a" * 64,
                "source_release_proof_sha256": "b" * 64,
                "accepted_schema_generation": 1,
                "accepted_archive_codec": "rcp-transfer-v1",
            },
        )
    assert wrong_source.status_code == 404
    assert wrong_target.status_code == 404


def _session_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM team_sessions").fetchone()[0])
