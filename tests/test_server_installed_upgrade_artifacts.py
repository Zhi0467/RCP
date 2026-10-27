from __future__ import annotations

import hashlib
import os
import pwd
from types import SimpleNamespace

import rcp.storage.models as storage_models
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_runtime import ServerMetadata
from rcp.storage import AppStore
from tests.helpers import create_named_app, signed_in_client
from tests.server_installed_upgrade import seed_question_followup, seed_turn_artifact
from tests.supervisor_reboot_data import prepare_data


def test_installed_upgrade_turn_kept_through_http_is_capturable(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    account = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=tmp_path / "projects"),
    )
    fixture = prepare_data(data_dir, tmp_path / "projects", account=account)
    artifact = seed_turn_artifact(data_dir, fixture, "candidate")
    assert artifact["storage"] == "artifact_store"
    app = create_named_app(data_dir=data_dir)
    store = app.state.catalog.store
    base = f"/api/projects/{fixture['project_id']}"
    route = f"{base}/tasks/{artifact['operation_id']}/artifacts/{artifact['artifact_id']}"
    with signed_in_client(app, base_url="https://testserver") as client:
        protocol = client.get("/api/health").json()["team_shell_protocol"]["maximum"]
        client.headers["RCP-Team-Shell-Protocol"] = str(protocol)
        exchange = client.post("/api/team/session/exchange", json={"token": fixture["token"]})
        assert exchange.status_code == 200, exchange.text
        kept = client.post(f"{route}/keep", json={})
        assert kept.status_code == 200, kept.text
        assert kept.json()["kept_at"]
        assert kept.json().get("kept_filename") is None
        task = client.get(f"{base}/tasks/{artifact['operation_id']}").json()
        assert task["status"] == "succeeded"
        (descriptor,) = task["result"]["artifacts"]
        assert descriptor["kept_at"] == kept.json()["kept_at"]
        assert descriptor.get("kept_filename") is None
        saved = client.get(f"{base}/artifacts")
        assert saved.status_code == 200, saved.text
        assert any(item["artifact_id"] == artifact["artifact_id"] for item in saved.json())
        content = client.get(f"{route}/content")
        assert content.status_code == 200, content.text
        assert hashlib.sha256(content.content).hexdigest() == artifact["sha256"]
        metadata = ServerMetadata.create(
            data_dir,
            host="127.0.0.1",
            port=18421,
            owner_kind="cli",
            control_socket=data_dir / "control.sock",
            running_commit="a" * 40,
            web_build_id="sha256:" + "b" * 64,
        )
        capture = BackupCaptureCoordinator(store, data_dir, metadata).capture_sqlite()
        (project,) = capture.receipt.projects
        assert project.project_id == fixture["project_id"]
        assert project.status == "capturable", project


def test_installed_upgrade_followup_is_capturable_until_injected_rejection(tmp_path, monkeypatch):
    from rcp.server_ops import backup_capture
    from tests import server_installed_upgrade_hook as hook

    data_dir = tmp_path / "data"
    account = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=tmp_path / "projects"),
    )
    fixture = prepare_data(data_dir, tmp_path / "projects", account=account)
    followup = seed_question_followup(data_dir, fixture)
    app = create_named_app(data_dir=data_dir)
    store = app.state.catalog.store
    task = store.agent_task(followup["operation_id"])
    # The installed journey reads it back through the served task route.
    assert task is not None and task.status == "succeeded" and task.visible
    (question,) = store.list_questions(project_id=fixture["project_id"])
    origin_task = store.agent_task(question.origin.operation_id)
    assert origin_task is not None and origin_task.request["mode"] == "work"
    assert question.state == "answered"
    assert question.followup_operation_id == task.operation_id
    assert origin_task.dispatch_authority is not None
    assert origin_task.dispatch_authority.task_contract == question.origin.capability == "work_auto"
    assert origin_task.write_scope_fingerprint == question.origin.write_scope_fingerprint
    assert question.origin.write_scope_fingerprint is not None
    metadata = ServerMetadata.create(
        data_dir,
        host="127.0.0.1",
        port=18421,
        owner_kind="cli",
        control_socket=data_dir / "control.sock",
        running_commit="a" * 40,
        web_build_id="sha256:" + "b" * 64,
    )
    receipt = BackupCaptureCoordinator(store, data_dir, metadata).capture_sqlite().receipt
    (project,) = receipt.projects
    assert project.status == "capturable", project

    for name in ("inspect_backup_project_registration", "reinspect_uncaptured_projects"):
        monkeypatch.setattr(backup_capture, name, getattr(backup_capture, name))
    hook.reject_backup_inventory(fixture["project_id"])
    (rejected,) = backup_capture.reinspect_uncaptured_projects(receipt).projects
    assert rejected.status == "uncaptured"


def test_a_project_registered_beside_a_running_server_is_readable(tmp_path, monkeypatch):
    # The installed-upgrade journey seeds its project from another process
    # while the server runs; the server must not need a restart to read it.
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    account = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=tmp_path / "projects"),
    )
    store, code = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Running server")
    _member, token = store.enroll_team_member(code, "Researcher")
    app = create_named_app(data_dir=data_dir)
    with signed_in_client(app, base_url="https://testserver") as client:
        protocol = client.get("/api/health").json()["team_shell_protocol"]["maximum"]
        client.headers["RCP-Team-Shell-Protocol"] = str(protocol)
        exchange = client.post("/api/team/session/exchange", json={"token": token})
        assert exchange.status_code == 200, exchange.text
        assert client.get("/api/projects").status_code == 200
        fixture = prepare_data(data_dir, tmp_path / "projects", account=account, member_token=token)

        response = client.get(f"/api/projects/{fixture['project_id']}")

        assert response.status_code == 200, response.text


def test_installed_upgrade_legacy_paper_preserves_three_distinct_versions(tmp_path, monkeypatch):
    from pathlib import Path

    from rcp.config import load_manifest
    from rcp.paper import PaperService
    from tests.server_upgrade_paper import ANCESTOR, CANONICAL, DRAFT, seed_legacy_paper

    data_dir = tmp_path / "data"
    account = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=tmp_path / "projects"),
    )
    fixture = prepare_data(data_dir, tmp_path / "projects", account=account)
    result = seed_legacy_paper(data_dir, fixture)
    assert result["ancestor_stored"]
    for _ in range(2):
        store = AppStore(data_dir / "rcp.sqlite3")
        record = store.project(fixture["project_id"])
        assert record is not None
        paper = PaperService(
            load_manifest(Path(record.locator)), store, project_id=record.project_id
        )
        snapshot = paper.snapshot()
        assert snapshot.content == DRAFT and snapshot.incoming_content == CANONICAL
        assert snapshot.sync_state == "behind"
        with store.connection() as connection:
            assert (
                connection.execute(
                    "SELECT ancestor_content FROM paper_drafts WHERE project_id = ?",
                    (record.project_id,),
                ).fetchone()[0]
                == ANCESTOR
            )
        store.close()


def test_current_paper_upgrade_fixture_retains_named_binary_before_unpublished_rename():
    import io
    import json
    from copy import deepcopy
    from email.parser import BytesParser
    from email.policy import default

    from tests.server_upgrade_paper import (
        ANCESTOR,
        CANONICAL,
        DRAFT,
        EDITED_FIGURE,
        seed_current_paper,
    )

    # S1 is built in another worktree. This fake implements only its documented
    # REST boundary; the installed gate above exercises the actual candidate.
    tree = {"saved_revision": 1, "published_revision": 0, "hold": "legacy", "files": []}
    versions = {
        "legacy": [
            {"text_sha256": hashlib.sha256(text.encode()).hexdigest()}
            for text in (ANCESTOR, CANONICAL, DRAFT)
        ]
    }
    contents = {}

    def request(path, *, body=None, method="GET", cookie="", content_type="application/json"):
        suffix = path.removeprefix("/api/projects/project/paper")
        payload = {}
        if method != "GET":
            tree["saved_revision"] += 1
            if content_type.startswith("multipart/"):
                message = BytesParser(policy=default).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
                )
                parts = {
                    part.get_param("name", header="content-disposition"): part
                    for part in message.iter_parts()
                }
                name = parts["path"].get_payload(decode=True).decode()
                data = parts["file"].get_payload(decode=True)
                if suffix == "/uploads":
                    file = {"file_id": "figure", "path": name, "kind": "binary"}
                    tree["files"].append(file)
                else:
                    file = next(item for item in tree["files"] if item["path"] == name)
                file["blob_sha256"] = hashlib.sha256(data).hexdigest()
                contents[file["file_id"]] = data
            elif suffix == "/files":
                tree["files"].append({"file_id": f"text-{len(tree['files'])}", **body})
            elif suffix == "/history":
                versions["named"] = deepcopy(tree["files"])
                payload = {"version_id": "named"}
            elif suffix.endswith("/rename"):
                file = next(
                    item for item in tree["files"] if suffix == f"/files/{item['file_id']}/rename"
                )
                file["path"] = body["path"]
        elif suffix == "/tree":
            payload = tree
        elif suffix == "/history":
            payload = [{"version_id": key} for key in versions]
        elif suffix.startswith("/history/"):
            payload = {"manifest": versions[suffix.removeprefix("/history/")]}
        elif suffix.endswith("/raw"):
            return io.BytesIO(contents[suffix.removeprefix("/files/").removesuffix("/raw")])
        else:
            raise AssertionError((path, method))
        return io.BytesIO(json.dumps(payload).encode())

    result = seed_current_paper(
        request, {"project_id": "project", "paper": {"ancestor_stored": True}}, "fixture-session"
    )
    assert result["saved_revision"] > tree["published_revision"]
    assert result["figure_sha256"] == hashlib.sha256(EDITED_FIGURE).hexdigest()
