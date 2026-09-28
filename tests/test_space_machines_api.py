from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.config import load_manifest
from rcp.projects import fill_space_machines
from tests.helpers import create_named_app


@pytest.fixture
def app(manifest, tmp_path):
    return create_named_app(str(manifest.path), data_dir=tmp_path / "data")


def _store(app):
    return app.state.background_tasks.store


def _machine(client: TestClient, name: str) -> dict[str, object]:
    machines = client.get("/api/space/machines").json()["machines"]
    return next(machine for machine in machines if machine["name"] == name)


def test_registered_machines_fill_the_list_and_a_used_machine_cannot_be_deleted(app) -> None:
    client = TestClient(app)
    project_id = app.state.default_project_id
    laptop = _machine(client, "laptop")
    assert laptop["host"] == "" and laptop["in_use"] is True
    assert laptop["projects"] == [
        {"project_id": project_id, "project_name": "test-paper", "alias": "laptop"}
    ]
    assert client.delete(f"/api/space/machines/{laptop['machine_id']}").status_code == 409

    store = _store(app)
    store.delete_space_machine(str(laptop["machine_id"]))
    fill_space_machines(store)
    assert [machine.name for machine in store.space_machines()] == ["laptop"]


def test_a_new_machine_card_is_created_renamed_and_deleted(app) -> None:
    client = TestClient(app)
    body = {"name": "GPU", "host": "alice@gpu.example", "os_account": "alice"}
    created = client.post("/api/space/machines", json=body)
    assert created.status_code == 200, created.text
    machine = created.json()
    assert machine["in_use"] is False and machine["projects"] == []
    assert client.post("/api/space/machines", json=body).status_code == 409
    assert (
        client.post("/api/space/machines", json={**body, "host": "-oProxyCommand=sh"}).status_code
        == 422
    )

    path = f"/api/space/machines/{machine['machine_id']}"
    renamed = client.patch(path, json={"name": "Big GPU"})
    assert renamed.json()["name"] == "Big GPU"
    assert client.delete(path).status_code == 200
    assert client.delete(path).status_code == 404


def test_local_writable_paths_must_be_folders_outside_rcp_storage(app, tmp_path) -> None:
    client = TestClient(app)
    shared = tmp_path / "shared"
    shared.mkdir()
    path = f"/api/space/machines/{_machine(client, 'laptop')['machine_id']}"

    saved = client.patch(path, json={"writable_paths": [f"{shared}/", str(shared), str(tmp_path)]})
    assert saved.status_code == 200, saved.text
    # A grant containing the data folder is allowed; it stays read-only inside.
    assert saved.json()["writable_paths"] == sorted([str(shared), str(tmp_path)])

    inside_data = tmp_path / "data" / "inside"
    inside_data.mkdir()
    for refused in (tmp_path / "missing", inside_data, Path("relative")):
        assert client.patch(path, json={"writable_paths": [str(refused)]}).status_code == 422
    assert _machine(client, "laptop")["writable_paths"] == sorted([str(shared), str(tmp_path)])


def test_remote_writable_paths_are_checked_over_ssh_against_the_remote_home(
    app, monkeypatch
) -> None:
    client = TestClient(app)
    machine = client.post(
        "/api/space/machines",
        json={"name": "GPU", "host": "alice@gpu.example", "os_account": "alice"},
    ).json()
    requests: list[dict[str, object]] = []

    def runner(command, **_kwargs):
        request = json.loads(shlex.split(command[-1])[-1])
        requests.append(request)
        resolved = {path: path for path in request["paths"]}
        output = {"home": "/home/alice", "resolved": resolved}
        return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

    monkeypatch.setattr("rcp.setup.subprocess.run", runner)
    path = f"/api/space/machines/{machine['machine_id']}"
    saved = client.patch(path, json={"writable_paths": ["/data/shared/huggingface"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["writable_paths"] == ["/data/shared/huggingface"]
    assert requests[0]["mode"] == "check"

    refused = client.patch(path, json={"writable_paths": ["/home/alice/.rcp/stages"]})
    assert refused.status_code == 422


def test_directories_filter_before_paging_and_lock_rcp_storage(app, tmp_path, monkeypatch) -> None:
    client = TestClient(app)
    browse = tmp_path / "browse"
    for name in ("alpha", "Beta", "gamma"):
        (browse / name).mkdir(parents=True)
    for index in range(5):
        (browse / f"alpha-file-{index}").write_text("", encoding="utf-8")
    monkeypatch.setattr("rcp.setup.MACHINE_DIRECTORY_PAGE_SIZE", 2)
    path = f"/api/space/machines/{_machine(client, 'laptop')['machine_id']}/directories"

    first = client.post(path, json={"path": str(browse)}).json()
    assert [entry["name"] for entry in first["entries"]] == ["alpha", "Beta"]
    assert (first["total"], first["next_offset"]) == (3, 2)
    second = client.post(path, json={"path": str(browse), "offset": 2}).json()
    assert [entry["name"] for entry in second["entries"]] == ["gamma"]
    assert second["next_offset"] is None
    filtered = client.post(path, json={"path": str(browse), "filter": "ALP"}).json()
    assert [entry["name"] for entry in filtered["entries"]] == ["alpha"]
    assert filtered["parent"] == str(tmp_path)

    monkeypatch.setattr("rcp.setup.MACHINE_DIRECTORY_PAGE_SIZE", 200)
    top = client.post(path, json={"path": str(tmp_path)}).json()
    protected = {entry["name"]: entry["protected"] for entry in top["entries"]}
    assert protected["data"] is True and protected["browse"] is False
    assert client.post(path, json={"path": str(tmp_path / "missing")}).status_code == 422


def test_adding_a_space_machine_to_a_project_appends_it_to_the_manifest(app, manifest) -> None:
    client = TestClient(app)
    project_id = app.state.default_project_id
    machine = client.post(
        "/api/space/machines",
        json={"name": "GPU", "host": "alice@gpu.example", "os_account": "alice"},
    ).json()
    path = f"/api/projects/{project_id}/machines"

    added = client.post(path, json={"machine_id": machine["machine_id"], "alias": "gpu"})
    assert added.status_code == 200, added.text
    machines = load_manifest(manifest.path).machine_map
    assert (machines["gpu"].host, machines["gpu"].os_account) == ("alice@gpu.example", "alice")
    assert machines["laptop"].host == ""
    assert {repository.machine for repository in load_manifest(manifest.path).repositories} == {
        "laptop"
    }
    assert _machine(client, "GPU")["projects"] == [
        {"project_id": project_id, "project_name": "test-paper", "alias": "gpu"}
    ]

    again = client.post(path, json={"machine_id": machine["machine_id"], "alias": "gpu-two"})
    assert again.status_code == 422
    laptop = _machine(client, "laptop")
    assert (
        client.post(path, json={"machine_id": laptop["machine_id"], "alias": "gpu"}).status_code
        == 422
    )
    assert client.post(path, json={"machine_id": "missing", "alias": "x"}).status_code == 404
