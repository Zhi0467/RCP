from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from rcp.browser import BrowserReadiness
from rcp.config import load_manifest
from rcp.projects import fill_space_machines
from tests.helpers import create_named_app, signed_in_client
from tests.test_project_membership import _create_project, _team_app


@pytest.fixture(autouse=True)
def browser_probe(monkeypatch):
    monkeypatch.setattr("rcp.ssh_agent.confirm_key_evidence", lambda **kwargs: ())
    monkeypatch.setattr("rcp.ssh_agent.running_agent_socket", lambda: None)
    monkeypatch.setattr(
        "rcp.api.space_machines.readiness",
        lambda **kwargs: BrowserReadiness(status="not_installed"),
    )


@pytest.fixture
def app(manifest, tmp_path):
    return create_named_app(str(manifest.path), data_dir=tmp_path / "data")


def _store(app):
    return app.state.background_tasks.store


def _machine(client: TestClient, name: str) -> dict[str, object]:
    machines = client.get("/api/space/machines").json()["machines"]
    return next(machine for machine in machines if machine["name"] == name)


def test_registered_machines_fill_the_list_and_a_used_machine_cannot_be_deleted(app) -> None:
    client = signed_in_client(app)
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


def test_projects_naming_two_accounts_for_one_host_leave_its_card_with_none(app) -> None:
    store = _store(app)
    assert store.ensure_space_machines([("gpu.example", "alice", "gpu")]) == []
    assert store.ensure_space_machines([("gpu.example", "alice", "gpu")]) == []
    assert store.space_machine_for("gpu.example").os_account == "alice"
    # Neither manifest is picked at random; an empty account checks nothing.
    assert store.ensure_space_machines([("gpu.example", "bob", "gpu")]) == ["gpu.example"]
    assert store.space_machine_for("gpu.example").os_account == ""
    assert store.ensure_space_machines([("gpu.example", "alice", "gpu")]) == []
    assert store.space_machine_for("gpu.example").os_account == ""


def test_a_new_machine_card_is_created_renamed_and_deleted(app) -> None:
    client = signed_in_client(app)
    body = {"name": "GPU", "host": "alice@gpu.example", "os_account": "alice"}
    created = client.post("/api/space/machines", json=body)
    assert created.status_code == 200, created.text
    machine = created.json()
    assert machine["in_use"] is False and machine["projects"] == []
    assert client.post("/api/space/machines", json=body).status_code == 409
    assert client.post("/api/space/machines", json={**body, "os_account": "bob"}).status_code == 409
    assert client.post("/api/space/machines", json={**body, "host": ""}).status_code == 409
    assert (
        client.post("/api/space/machines", json={**body, "host": "-oProxyCommand=sh"}).status_code
        == 422
    )
    # The store holds one card per host even when a racing request skipped the check.
    with pytest.raises(ValueError):
        _store(app).create_space_machine(name="Other", host=body["host"], os_account="bob")

    path = f"/api/space/machines/{machine['machine_id']}"
    renamed = client.patch(path, json={"name": "Big GPU"})
    assert renamed.json()["name"] == "Big GPU"
    assert client.delete(path).status_code == 200
    assert client.delete(path).status_code == 404


def test_local_writable_paths_must_be_folders_outside_rcp_storage(app, manifest, tmp_path) -> None:
    client = signed_in_client(app)
    shared = tmp_path / "shared"
    shared.mkdir()
    path = f"/api/space/machines/{_machine(client, 'laptop')['machine_id']}"

    saved = client.patch(path, json={"writable_paths": [f"{shared}/", str(shared), str(tmp_path)]})
    assert saved.status_code == 200, saved.text
    # A grant containing the data folder is allowed; it stays read-only inside.
    assert saved.json()["writable_paths"] == sorted([str(shared), str(tmp_path)])

    inside_data = tmp_path / "data" / "inside"
    inside_data.mkdir()
    research = Path(load_manifest(manifest.path).repositories[0].path) / ".research"
    research.mkdir(exist_ok=True)
    # A clean spelling whose folder no launcher can mount.
    (tmp_path / "cache:v1").mkdir()
    (tmp_path / "cache").symlink_to(tmp_path / "cache:v1")
    for refused in (
        tmp_path / "missing",
        inside_data,
        research,
        Path("relative"),
        tmp_path / "cache",
    ):
        assert client.patch(path, json={"writable_paths": [str(refused)]}).status_code == 422
    assert _machine(client, "laptop")["writable_paths"] == sorted([str(shared), str(tmp_path)])

    # A retained /tmp stage stays read-only for launches, so it is neither saved nor offered.
    stage = Path("/tmp") / f"rcp-run.test-{uuid.uuid4().hex[:8]}"
    stage.mkdir(mode=0o700)
    try:
        assert client.patch(path, json={"writable_paths": [str(stage)]}).status_code == 422
        listing = client.post(
            f"{path}/directories", json={"path": "/tmp", "filter": stage.name}
        ).json()
        locked = {entry["name"]: entry["protected"] for entry in listing["entries"]}
        assert locked[stage.name] is True
    finally:
        stage.rmdir()


def test_remote_writable_paths_are_checked_over_ssh_against_the_remote_home(
    app, monkeypatch
) -> None:
    client = signed_in_client(app)
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
    client = signed_in_client(app)
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
    # A shortcut is judged by where it lands, the same way saving judges it.
    (tmp_path / "data-link").symlink_to(tmp_path / "data")
    (tmp_path / "browse-link").symlink_to(browse)
    top = client.post(path, json={"path": str(tmp_path)}).json()
    protected = {entry["name"]: entry["protected"] for entry in top["entries"]}
    assert protected["data"] is True and protected["browse"] is False
    assert protected["data-link"] is True and protected["browse-link"] is False
    assert client.post(path, json={"path": str(tmp_path / "missing")}).status_code == 422


def test_adding_a_space_machine_to_a_project_appends_it_to_the_manifest(app, manifest) -> None:
    client = signed_in_client(app)
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


def test_team_machines_name_only_the_viewers_projects_and_need_an_account(tmp_path) -> None:
    _app, client, _store, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo", seat_member=people[0].user_id)
    (machine,) = client.get("/api/space/machines").json()["machines"]
    assert [project["project_id"] for project in machine["projects"]] == [project_id]

    acting[0] = people[1].user_id
    (hidden,) = client.get("/api/space/machines").json()["machines"]
    assert hidden["projects"] == [] and hidden["in_use"] is True

    body = {"name": "GPU", "host": "gpu.example"}
    assert client.post("/api/space/machines", json=body).status_code == 422
    assert client.post("/api/space/machines", json={**body, "os_account": "bob"}).status_code == 200


def test_a_project_machine_finds_its_card_by_host(app) -> None:
    store = _store(app)
    card = store.create_space_machine(name="GPU", host="gpu.example", os_account="alice")
    store.ensure_space_machines([("gpu.example", "", "gpu")])

    assert [machine.machine_id for machine in store.space_machines() if machine.host] == [
        card.machine_id
    ]
    assert store.space_machine_for("gpu.example") == card


def test_the_picker_locks_repository_state(app, manifest, tmp_path) -> None:
    client = signed_in_client(app)
    repository = Path(load_manifest(manifest.path).repositories[0].path)
    path = f"/api/space/machines/{_machine(client, 'laptop')['machine_id']}/directories"

    entries = client.post(path, json={"path": str(repository)}).json()["entries"]

    assert {entry["name"]: entry["protected"] for entry in entries}[".research"] is True

    # State kept elsewhere through a symlink is locked where it really lives.
    research = repository / ".research"
    elsewhere = tmp_path / "state-home"
    elsewhere.mkdir()
    shutil.move(research, elsewhere / "state")
    research.symlink_to(elsewhere / "state")
    (elsewhere / "ordinary").mkdir()
    entries = client.post(path, json={"path": str(elsewhere)}).json()["entries"]
    locked = {entry["name"]: entry["protected"] for entry in entries}
    assert locked == {"state": True, "ordinary": False}


def test_browser_readiness_and_explicit_install(app, monkeypatch, tmp_path) -> None:
    calls = []

    def install(**kwargs):
        calls.append(kwargs)
        return BrowserReadiness(status="ready")

    monkeypatch.setattr("rcp.api.space_machines.install_browser", install)
    client = signed_in_client(app)
    machine = _machine(client, "laptop")
    assert "browser" not in machine
    browser = client.get(f"/api/space/machines/{machine['machine_id']}/browser")
    assert browser.json()["status"] == "not_installed"
    assert calls == []
    response = client.post(f"/api/space/machines/{machine['machine_id']}/browser/install", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert len(calls) == 1
    assert calls[0]["host"] == machine["host"]
    assert calls[0]["os_account"] == machine["os_account"]
    assert calls[0]["data_dir"] == tmp_path / "data"
    assert client.post("/api/space/machines/absent/browser/install", json={}).status_code == 404
    assert len(calls) == 1


@pytest.fixture
def hidden_policy(monkeypatch):
    """Isolate the Settings contract while the policy slice is developed separately."""
    from pydantic import TypeAdapter

    from rcp.core.models import HiddenReadPath

    calls = []

    class Rejected(ValueError):
        code = "protected_overlap"

    def validate(folders, *, protected_roots):
        calls.append((folders, protected_roots))
        result = sorted(set(TypeAdapter(list[HiddenReadPath]).validate_python(folders)))
        for path in result:
            candidate = Path(path)
            for root in protected_roots:
                protected = Path(root)
                if candidate.is_relative_to(protected) or protected.is_relative_to(candidate):
                    raise Rejected("Folder overlaps a required path.")
        return result

    from rcp.agents.hidden_read import SYSTEM_RUNTIME_ROOTS

    module = ModuleType("rcp.agents.hidden_read")
    module.validate_machine_hidden_folders = validate
    module.SYSTEM_RUNTIME_ROOTS = SYSTEM_RUNTIME_ROOTS
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


def test_machine_hidden_folders_round_trip_and_atomic_refusal(
    app, manifest, tmp_path, hidden_policy, monkeypatch
) -> None:
    from rcp.projects import ProjectCatalog

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(
        ProjectCatalog, "provider_targets", lambda self: [("claude", "", str(agents / "claude"))]
    )
    client = signed_in_client(app)
    machine = _machine(client, "laptop")
    machine_id = machine["machine_id"]
    url = f"/api/space/machines/{machine_id}"
    secret = tmp_path / "private"
    secret.mkdir()
    saved = client.patch(url, json={"hidden_folders": [str(secret), str(secret)]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["hidden_folders"] == [str(secret.resolve())]
    assert _store(app).space_machine(machine_id).hidden_folders == [str(secret.resolve())]
    assert client.patch(url, json={"name": "Renamed"}).json()["hidden_folders"] == [
        str(secret.resolve())
    ]

    checkout = Path(load_manifest(manifest.path).repositories[0].path)
    # A configured provider binary's folder is refused at save, as launch would refuse it.
    for folder in (checkout, checkout / ".research", checkout.parent, agents):
        refused = client.patch(url, json={"name": "Not saved", "hidden_folders": [str(folder)]})
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "protected_overlap"
        assert _store(app).space_machine(machine_id).name == "Renamed"
        assert _store(app).space_machine(machine_id).hidden_folders == [str(secret.resolve())]
    assert client.patch(url, json={"hidden_folders": []}).json()["hidden_folders"] == []


@pytest.mark.parametrize("remote", [False, True])
def test_machine_hidden_folders_validate_host_resolved_symlinks(
    app, monkeypatch, hidden_policy, remote
) -> None:
    client = signed_in_client(app)
    if remote:
        machine = client.post(
            "/api/space/machines",
            json={
                "name": "GPU",
                "host": "worker@gpu.example",
                "os_account": "worker",
            },
        ).json()
    else:
        machine = _machine(client, "laptop")
    calls = []

    def check(host, request, *, os_account):
        calls.append((host, os_account))
        if "protect" in request:
            return {"protected_targets": request["protect"]}
        return {
            "home": "/home/worker",
            "resolved": {
                path: ("/home/worker/.rcp/stages" if path == "/private-link" else path)
                for path in request["paths"]
            },
        }

    monkeypatch.setattr("rcp.api.space_machines.run_machine_directory_request", check)
    response = client.patch(
        f"/api/space/machines/{machine['machine_id']}", json={"hidden_folders": ["/private-link"]}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "protected_overlap"
    assert calls and all(host == machine["host"] for host, _ in calls)
    assert _store(app).space_machine(machine["machine_id"]).hidden_folders == []


def test_team_member_can_edit_machine_hidden_folders(tmp_path, hidden_policy) -> None:
    _app, client, store, people, acting = _team_app(tmp_path)
    _create_project(client, tmp_path / "repo", seat_member=people[0].user_id)
    machine = client.get("/api/space/machines").json()["machines"][0]
    secret = tmp_path / "member-secret"
    secret.mkdir()
    acting[0] = people[1].user_id
    response = client.patch(
        f"/api/space/machines/{machine['machine_id']}", json={"hidden_folders": [str(secret)]}
    )
    assert response.status_code == 200, response.text
    assert store.space_machine(machine["machine_id"]).hidden_folders == [str(secret.resolve())]


def test_machine_hidden_folders_upgrade_preserves_existing_card(app) -> None:
    from rcp.storage import AppStore

    store = _store(app)
    machine = store.space_machines()[0]
    store.update_space_machine(machine.machine_id, writable_paths=["/shared"])
    with store.connection() as connection:
        connection.execute("ALTER TABLE space_machines DROP COLUMN hidden_folders_json")
        connection.execute(
            "DELETE FROM storage_schema_migrations WHERE migration_name = ?",
            ("machine_hidden_folders_v1",),
        )
    upgraded = AppStore(store.path)
    card = upgraded.space_machine(machine.machine_id)
    assert card.writable_paths == ["/shared"]
    assert card.hidden_folders == []
    upgraded.update_space_machine(machine.machine_id, hidden_folders=["/private"])
    upgraded = AppStore(store.path)
    assert upgraded.space_machine(machine.machine_id).hidden_folders == ["/private"]


@pytest.mark.parametrize("host", ["", "remote.example"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_settings_projects_defaults_and_only_local_readiness(
    app, monkeypatch, host, ready, platform
):
    from rcp.core.models import HiddenReadStatus

    monkeypatch.setattr("rcp.server_ops.doctor.sys.platform", platform)
    status = HiddenReadStatus(
        status="enforced" if ready else "unhidden",
        reasons=() if ready else ("wrapper_unavailable",),
    )
    calls = []

    def probe():
        calls.append(True)
        return status

    monkeypatch.setattr("rcp.api.space_machines.cached_hidden_read_readiness", probe)
    machine = next((card for card in _store(app).space_machines() if card.host == host), None)
    if machine is None:
        machine = _store(app).create_space_machine(
            name="Projection", host=host, os_account="worker"
        )
    _store(app).update_space_machine(machine.machine_id, hidden_folders=["/private-secret"])
    record = _machine(signed_in_client(app), machine.name)
    projection = record["hidden_read"]
    assert projection["user_folders"] == record["hidden_folders"] == ["/private-secret"]
    assert "~/.config/rcp/claude-setup-token" in projection["default_paths"]
    assert "effective_scope" not in projection
    reasons = set(status.reasons)
    if platform == "darwin":
        reasons.add("browser_unwrapped_macos")
    expected = HiddenReadStatus(
        status="unhidden" if reasons else "enforced", reasons=tuple(sorted(reasons))
    )
    assert projection["readiness"] == (None if host else expected.model_dump(mode="json"))
    calls.clear()
    from rcp.api.space_machines import _hidden_read_projection

    _hidden_read_projection(machine, _store(app).path.parent)
    assert len(calls) == (0 if host else 1)


def test_local_wrapper_readiness_cache_expires(monkeypatch):
    from rcp.agents import hidden_read
    from rcp.limits import HIDDEN_READ_READINESS_TTL_SECONDS

    clock = [0.0]
    calls = []
    monkeypatch.setattr(hidden_read, "_readiness_cache", None)
    monkeypatch.setattr(hidden_read.time, "monotonic", lambda: clock[0])

    def probe():
        calls.append(True)
        return {"ready": len(calls) == 1, "reason": "wrapper_unavailable"}

    monkeypatch.setattr(hidden_read, "probe_hidden_read_wrapper", probe)
    assert hidden_read.cached_hidden_read_readiness().status == "enforced"
    assert hidden_read.cached_hidden_read_readiness().status == "enforced"
    assert len(calls) == 1
    clock[0] += HIDDEN_READ_READINESS_TTL_SECONDS
    assert hidden_read.cached_hidden_read_readiness().status == "unhidden"
    assert len(calls) == 2
