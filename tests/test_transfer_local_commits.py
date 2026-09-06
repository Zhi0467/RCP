from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rcp.agents import AgentLauncher
from rcp.project_transfer import capture_project_transfer_source
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore, ProjectTransferSourceConfiguration
from rcp.storage.provisioning import project_transfer_source_configuration_sha256
from rcp.transfer.archive import TransferArchiveEnvelope, TransferArchiveManifest
from rcp.transfer.configuration import build_transfer_target_configuration
from rcp.transfer.importer import import_project_transfer
from rcp.transfer.source import (
    _require_reviewed_source_unchanged,
    advance_source_project_transfer,
    source_transfer_export_path,
    stage_transfer_archive,
)

from .helpers import TASK_SETTLE_TIMEOUT, create_named_app, wait_until
from .test_project_transfer_request_api import _source_project
from .test_project_transfer_request_storage import _actor
from .test_transfer_archive_manifest import _entry, _manifest
from .test_transfer_import import _prepare_target_request


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, text: str) -> str:
    (repository / "code.txt").write_text(text)
    _git(repository, "add", "code.txt")
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        text,
    )
    return _git(repository, "rev-parse", "HEAD")


def _source(tmp_path):
    data = tmp_path / "personal"
    app = create_named_app(data_dir=data)
    store = app.state.background_tasks.store
    actor = _actor(store, "Z")
    project = _source_project(app, data)
    repository = data / "paper"
    base = _commit(repository, "published")
    return app, store, actor, project, repository, base


@pytest.mark.skipif(
    not os.environ.get("RCP_FROZEN_BACKEND"), reason="requires a built desktop backend"
)
def test_frozen_backend_prepares_local_unpushed_commits(tmp_path):
    _app, _store, _actor_value, project, repository, _base = _source(tmp_path)
    head = _commit(repository, "unpublished frozen-backend revision")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RCP_") and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    environment.update(RCP_DATA_DIR=str(tmp_path / "personal"), PATH=os.defpath)
    with (tmp_path / "backend.log").open("wb") as log:
        process = subprocess.Popen(
            [
                os.environ["RCP_FROZEN_BACKEND"],
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--web-assets",
                "prebuilt",
            ],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2.0) as client:

                def healthy():
                    assert process.poll() is None, (tmp_path / "backend.log").read_text()
                    try:
                        return client.get("/api/health").status_code == 200
                    except httpx.TransportError:
                        return False

                wait_until(healthy, timeout=TASK_SETTLE_TIMEOUT, detail="frozen backend startup")
                response = client.post(
                    "/api/project-transfers/source-requests",
                    json={
                        "request_id": str(uuid.uuid4()),
                        "project_id": project,
                        "target_space_id": str(uuid.uuid4()),
                        "include_local_commits": True,
                    },
                )
                assert response.status_code == 201, response.text
                configuration = response.json()["source_configuration"]
                assert configuration["repositories"][0]["source_commit"] == head
                assert configuration["supported_archive_codecs"] == ["rcp-transfer-v2"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=TASK_SETTLE_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=TASK_SETTLE_TIMEOUT)


@pytest.mark.parametrize("include", [False, True])
def test_source_create_binds_choice_and_preserves_legacy_wire(tmp_path, include):
    protocol = json.loads(
        (Path(__file__).parent / "fixtures" / "team_shell_protocol_v3.json").read_text()
    )["native_transfer"]
    app, store, _actor_value, project, repository, _base = _source(tmp_path)
    head = _commit(repository, "unpublished")
    request_id = str(uuid.uuid4())
    body = dict(
        request_id=request_id,
        project_id=project,
        target_space_id=str(uuid.uuid4()),
        include_local_commits=include,
    )
    with TestClient(app) as client:
        response = client.post("/api/project-transfers/source-requests", json=body)
        assert response.status_code == 201, response.text
        configuration = response.json()["source_configuration"]
        repo = configuration["repositories"][0]
        assert configuration["supported_archive_codecs"] == [
            protocol["archive_codecs"][int(include)]
        ]
        assert (protocol["repository_commit_field"] in repo) == include
        if include:
            assert repo["source_commit"] == head
            assert configuration["supported_archive_codecs"] == ["rcp-transfer-v2"]
        else:
            assert "source_commit" not in repo
            assert configuration["supported_archive_codecs"] == ["rcp-transfer-v1"]
        assert client.post("/api/project-transfers/source-requests", json=body).status_code == 201
        response = client.post(
            "/api/project-transfers/source-requests",
            json={**body, "include_local_commits": not include},
        )
        assert response.status_code == 409
    captured = store.project_transfer_request(request_id)
    assert (
        captured.source_configuration_sha256
        == hashlib.sha256(
            json.dumps(
                configuration,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("include", [False, True])
def test_export_and_import_restore_opted_revision_without_publishing_git(
    tmp_path,
    monkeypatch,
    include,
):
    app, source, actor, project, repository, base = _source(tmp_path)
    published = tmp_path / "published.git"
    _git(tmp_path, "clone", "--bare", str(repository), str(published))
    head = _commit(repository, "unpublished")
    # Uncommitted work is neither discarded on the source nor copied to the target.
    (repository / "code.txt").write_text("dirty source")
    (repository / "untracked.txt").write_text("not a committed file")
    service = app.state.catalog.open(project)
    config, graph_head = capture_project_transfer_source(service, include_local_commits=include)
    target_data = tmp_path / "team"
    target = AppStore(target_data / "rcp.sqlite3", space_kind="team")
    target_actor = _actor(target, "Alice")
    request = source.create_source_project_transfer_request(
        project_id=project,
        target_space_id=target.space_id,
        initiated_by=actor,
        source_configuration=config,
    )
    provisioning = _prepare_target_request(
        target,
        actor=target_actor,
        source=config,
        project_id=project,
        central_root=tmp_path / "central",
        monkeypatch=monkeypatch,
    )
    destination = Path(provisioning.repositories[0].resolved_path)
    _git(destination, "clone", "--no-hardlinks", str(published), ".")
    _git(destination, "checkout", "--detach", base)
    _git(destination, "remote", "set-url", "origin", "https://example.invalid/not-pushed.git")
    target_request = target.create_target_project_transfer_request(
        provisioning_request_id=provisioning.request_id,
        source_request_id=request.request_id,
        source_project_id=project,
        source_space_id=source.space_id,
        initiated_by=target_actor,
        source_configuration=config,
        source_configuration_sha256=request.source_configuration_sha256,
        source_release_proof_sha256=request.source_release_proof_sha256,
        accepted_schema_generation=config.source_schema_generation,
        accepted_archive_codec=config.supported_archive_codecs[0],
    )
    source.link_source_project_transfer_request(
        request.request_id, receipt=target_request.link_receipt
    )
    target_request = target.record_target_project_transfer_admission(
        target_request.request_id,
        admitted_by=target_actor,
    )
    source.accept_target_project_transfer_admission(
        request.request_id,
        receipt=target_request.target_admission_receipt,
    )
    released = source.record_source_project_transfer_release(
        request.request_id,
        released_by=actor,
        revalidated_configuration=config,
        source_head=graph_head,
    )
    target.accept_source_project_transfer_release(
        target_request.request_id,
        receipt=released.source_release_receipt,
    )
    captured = advance_source_project_transfer(source, app.state.catalog, request.request_id)
    archive_root = tmp_path / "decoded"
    readback = stage_transfer_archive(
        source_transfer_export_path(app.state.catalog.data_dir, request.request_id),
        archive_root,
    )
    assert readback.manifest.schema_version == (2 if include else 1)
    assert bool([e for e in readback.manifest.entries if e.group == "repository_git"]) == include
    target.bind_project_transfer_archive(
        target_request.request_id,
        archive_sha256=captured.archive_sha256,
        archive_size_bytes=captured.archive_size_bytes,
        source_fence_head=captured.source_fence_head,
    )
    configuration = build_transfer_target_configuration(
        provisioning,
        config,
        target_request.link_receipt,
        readback.manifest,
        archive_root,
    )
    catalog = ProjectCatalog(target_data, target, AgentLauncher())
    arguments = dict(
        archive=readback.manifest,
        envelope=readback.envelope,
        archive_root=archive_root,
        target_configuration=configuration,
    )
    receipt = import_project_transfer(catalog, **arguments)
    assert receipt.status == "complete"
    assert import_project_transfer(catalog, **arguments) == receipt
    assert _git(destination, "rev-parse", "HEAD") == (head if include else base)
    assert (destination / "code.txt").read_text() == ("unpublished" if include else "published")
    assert not (destination / "untracked.txt").exists()
    assert (
        _git(destination, "remote", "get-url", "origin") == "https://example.invalid/not-pushed.git"
    )
    assert _git(repository, "rev-parse", "HEAD") == head
    assert _git(published, "rev-parse", "HEAD") == base
    assert (repository / "code.txt").read_text() == "dirty source"
    assert (repository / "untracked.txt").is_file()
    assert (destination / ".research" / "patches" / "000002.json").read_bytes() == (
        repository / ".research" / "patches" / "000002.json"
    ).read_bytes()
    # Git restoration does not itself make the target project active.
    assert target.project(project) is None


def test_legacy_configuration_digest_does_not_gain_null_commit(tmp_path):
    app, _store, _actor_value, project, _repository, _base = _source(tmp_path)
    configuration, _head = capture_project_transfer_source(app.state.catalog.open(project))
    old_payload = configuration.model_dump(mode="json")
    assert all("source_commit" not in repo for repo in old_payload["repositories"])
    restored = ProjectTransferSourceConfiguration.model_validate_json(json.dumps(old_payload))
    assert restored.model_dump(mode="json") == old_payload
    assert project_transfer_source_configuration_sha256(restored) == (
        project_transfer_source_configuration_sha256(configuration)
    )


def test_git_head_drift_refuses_the_reviewed_source_boundary(tmp_path):
    app, _store, _actor_value, project, repository, _base = _source(tmp_path)
    service = app.state.catalog.open(project)
    configuration, graph_head = capture_project_transfer_source(service, include_local_commits=True)
    request = SimpleNamespace(source_configuration=configuration)
    _require_reviewed_source_unchanged(service, request)
    _commit(repository, "changed after review")
    with pytest.raises(ValueError, match="source configuration changed"):
        _require_reviewed_source_unchanged(service, request)
    assert service.history.head_ref() == graph_head


def test_commit_choice_requires_every_repository_and_only_new_codec(tmp_path):
    app, _store, _actor_value, project, _repository, _base = _source(tmp_path)
    configuration, _head = capture_project_transfer_source(
        app.state.catalog.open(project),
        include_local_commits=True,
    )
    payload = configuration.model_dump(mode="json")
    payload["supported_archive_codecs"] = ["rcp-transfer-v1", "rcp-transfer-v2"]
    with pytest.raises(ValidationError, match="every reviewed HEAD and v2"):
        ProjectTransferSourceConfiguration.model_validate_json(json.dumps(payload))
    payload["supported_archive_codecs"] = ["rcp-transfer-v2"]
    payload["repositories"][0].pop("source_commit")
    with pytest.raises(ValidationError, match="requires reviewed repository commits"):
        ProjectTransferSourceConfiguration.model_validate_json(json.dumps(payload))


def test_git_bundle_requires_new_codec_and_envelope():
    old = _manifest()
    entry = _entry("repositories/paper.bundle", "repository_git")
    payload = old.model_dump(mode="json")
    payload["entries"] = sorted(
        [*payload["entries"], entry.model_dump(mode="json")], key=lambda item: item["archive_path"]
    )
    payload["payload_size_bytes"] += entry.size_bytes
    with pytest.raises(ValidationError, match="only v2"):
        TransferArchiveManifest.model_validate_json(json.dumps(payload))
    payload.update(schema_version=2, archive_codec="rcp-transfer-v2")
    manifest = TransferArchiveManifest.model_validate_json(json.dumps(payload))
    envelope = TransferArchiveEnvelope.bind(
        manifest, archive_sha256="a" * 64, archive_size_bytes=100
    )
    assert envelope.archive_codec == "rcp-transfer-v2"
    envelope.verify_manifest(manifest)
    with pytest.raises(ValueError, match="does not match"):
        envelope.model_copy(update={"archive_codec": "rcp-transfer-v1"}).verify_manifest(manifest)
