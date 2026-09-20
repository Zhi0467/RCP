"""Membership and transport checks for project member terminals."""

from __future__ import annotations

import os
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rcp.api.app import create_app
from rcp.config import MachineConfig, RepositoryConfig
from rcp.storage import AppStore

from .helpers import wait_until
from .test_project_membership import _create_project, _team_app


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "/repositories", None),
        ("GET", "", None),
        ("POST", "", {"repository_id": "paper-repo"}),
        ("DELETE", "/unknown-session", None),
    ],
)
def test_every_terminal_http_route_hides_nonmember_projects(tmp_path, method, suffix, body):
    _app, client, _store, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    acting[0] = people[1].user_id

    response = client.request(method, f"/api/projects/{project_id}/terminals{suffix}", json=body)
    unknown = client.request(method, f"/api/projects/unknown-project/terminals{suffix}", json=body)

    assert response.status_code == unknown.status_code == 404
    assert response.json() == unknown.json() == {"detail": "Project not found"}


def test_terminal_websocket_checks_membership_independently(tmp_path):
    _app, client, _store, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    acting[0] = people[1].user_id

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(
            f"/api/projects/{project_id}/terminals/unknown-session/ws",
            headers={"Origin": "http://testserver"},
        ),
    ):
        pytest.fail("A nonmember was admitted to the terminal WebSocket")
    assert refused.value.code == 4404


def test_terminal_websocket_requires_its_own_team_authentication(tmp_path):
    AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Terminal tests")
    client = TestClient(create_app(data_dir=tmp_path), base_url="https://testserver")

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(
            "wss://testserver/api/projects/unknown-project/terminals/unknown-session/ws",
            headers={"Origin": "https://testserver"},
        ),
    ):
        pytest.fail("An unauthenticated browser was admitted to a terminal")
    assert refused.value.code == 4401


@pytest.mark.parametrize("origin", ["https://attacker.test", "null", ""])
def test_terminal_websocket_refuses_cross_origin_and_missing_origin(tmp_path, origin):
    _app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    headers = {"Origin": origin} if origin else {}

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(
            f"/api/projects/{project_id}/terminals/unknown-session/ws", headers=headers
        ),
    ):
        pytest.fail("A terminal WebSocket accepted an untrusted origin")
    assert refused.value.code == 4403


def test_terminal_websocket_resolves_real_team_session_cookie(manifest, tmp_path):
    data_dir = tmp_path / "authenticated-team"
    store, bootstrap = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Terminal tests")
    member, token = store.enroll_team_member(bootstrap, "Terminal member")
    app = create_app(str(manifest.path), data_dir=data_dir)
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200
    project_id = app.state.default_project_id
    assert store.is_project_member(project_id, member.user_id)

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(
            f"wss://testserver/api/projects/{project_id}/terminals/unknown-session/ws",
            headers={"Origin": "https://testserver"},
        ),
    ):
        pytest.fail("The unknown terminal session should be refused")
    # Authentication succeeded: the terminal lookup, rather than the HTTP
    # middleware, rejected this request.
    assert refused.value.code == 4404


def test_remote_repository_is_projected_as_unavailable_without_launch(tmp_path):
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    manifest = app.state.catalog.open(project_id).manifest
    manifest.machines.append(MachineConfig(alias="remote", host="compute.example"))
    manifest.repositories.append(
        RepositoryConfig(alias="remote-repo", machine="remote", path="/srv/repo")
    )

    response = client.get(f"/api/projects/{project_id}/terminals/repositories")

    assert response.status_code == 200, response.text
    repositories = {item["repository_id"]: item for item in response.json()}
    assert repositories["paper-repo"]["path"] == str(tmp_path / "repo")
    remote = repositories["remote-repo"]
    assert remote["eligible"] is False
    assert remote["path"] == "/srv/repo"
    assert remote["unavailable_reason"] == "PTY-over-SSH transport is not built."
    refused = client.post(
        f"/api/projects/{project_id}/terminals", json={"repository_id": "remote-repo"}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == remote["unavailable_reason"]
    assert client.get(f"/api/projects/{project_id}/terminals").json() == []


@pytest.fixture
def terminal_pty(monkeypatch):
    """Keep real session/transport owners, replacing only Linux process launch."""
    from rcp.terminals import launch

    slaves = []

    def start(_command, _unit):
        master, slave = os.openpty()
        os.set_blocking(master, False)
        slaves.append(slave)
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = lambda: setattr(process.poll, "return_value", 0)
        return process, master

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch, "launch", start)
    monkeypatch.setattr(launch, "stop_unit", lambda _unit: None)
    yield slaves
    for slave in slaves:
        os.close(slave)


def test_session_pty_reconnect_resize_and_end(tmp_path, terminal_pty):
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        opened = client.post(path, json={"repository_id": "paper-repo"})
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["session_id"]
        assert (
            client.post(path, json={"repository_id": "paper-repo"}).json()["session_id"]
            == session_id
        )
        assert len(terminal_pty) == 1
        socket_path = f"{path}/{session_id}/ws"
        with client.websocket_connect(
            socket_path, headers={"Origin": "http://testserver"}
        ) as socket:
            socket.send_json({"type": "input", "data": "terminal-input\n"})
            assert b"terminal-input" in socket.receive_bytes()
            socket.send_json({"type": "resize", "cols": 104, "rows": 33})
            wait_until(
                lambda: os.get_terminal_size(terminal_pty[0]) == (104, 33),
                detail="WebSocket resize did not reach the PTY",
            )
            assert client.get(path).json()[0]["state"] == "live"
        wait_until(lambda: app.state.services.terminals.get(project_id, session_id).state == "idle")
        assert client.get(path).json()[0]["session_id"] == session_id
        with client.websocket_connect(
            socket_path, headers={"Origin": "http://testserver"}
        ) as socket:
            assert b"terminal-input" in socket.receive_bytes()
            ended = client.delete(f"{path}/{session_id}")
            assert ended.status_code == 200, ended.text
            assert socket.receive_json()["type"] == "ended"
        assert client.get(path).json() == []
        assert client.delete(f"{path}/{session_id}").status_code == 404


def test_live_membership_loss_closes_socket_before_more_input(tmp_path, terminal_pty):
    app, client, store, people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        opened = client.post(path, json={"repository_id": "paper-repo"})
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["session_id"]
        with client.websocket_connect(
            f"{path}/{session_id}/ws", headers={"Origin": "http://testserver"}
        ) as socket:
            with store.connection() as connection:
                connection.execute(
                    "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
                    (project_id, people[0].user_id),
                )
            socket.send_json({"type": "input", "data": "must-not-reach-pty\n"})
            with pytest.raises(WebSocketDisconnect) as refused:
                socket.receive_bytes()
            assert refused.value.code == 4404
            os.set_blocking(terminal_pty[0], False)
            with pytest.raises(BlockingIOError):
                os.read(terminal_pty[0], 1024)
        client.portal.call(app.state.services.terminals.sweep)
        assert app.state.services.terminals.list(project_id) == []


def test_missing_systemd_is_reported_without_creating_a_shell(tmp_path, monkeypatch):
    from rcp.terminals import launch

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch.shutil, "which", lambda _name: None)
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        response = client.post(path, json={"repository_id": "paper-repo"})
        assert response.status_code == 409
        projected = client.get(f"{path}/repositories").json()[0]
        assert projected["eligible"] is False
        assert projected["reason"] == response.json()["detail"]
        assert "systemd-run" in response.json()["detail"]
        assert "not installed" in response.json()["detail"]
        assert client.get(path).json() == []
        assert app.state.services.terminals.list(project_id) == []


def test_running_work_is_visible_and_does_not_block_open(tmp_path, terminal_pty, monkeypatch):
    from rcp.storage import AgentTaskReceiptRecord, AgentTaskRecord

    app, client, store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    repository = app.state.catalog.open(project_id).manifest.repository_map["paper-repo"]
    running = AgentTaskRecord(
        operation_id="running-work",
        project_id=project_id,
        kind="project_chat",
        status="running",
        active=True,
        request={
            "mode": "work",
            "run_truth_scope": ["paper-repo"],
            "run_on": repository.machine,
        },
        created_at=store.now(),
        updated_at=store.now(),
        status_message="Updating the analysis",
    )
    tasks = [
        running,
        running.model_copy(update={"operation_id": "discuss", "request": {"mode": "discuss"}}),
        running.model_copy(
            update={"operation_id": "finished", "active": False, "status": "succeeded"}
        ),
        running.model_copy(update={"operation_id": "queued", "queued": True, "status": "queued"}),
        running.model_copy(
            update={"operation_id": "worktree", "request": {**running.request, "worktree": True}}
        ),
    ]
    monkeypatch.setattr(app.state.services.store, "all_project_agent_tasks", lambda _project: tasks)
    path = f"/api/projects/{project_id}/terminals"
    with client:
        repositories = client.get(f"{path}/repositories").json()
        expected = [{"operation_id": "running-work", "title": "Updating the analysis"}]
        assert repositories[0]["running_work"] == expected
        opened = client.post(path, json={"repository_id": "paper-repo"})
        assert opened.status_code == 200, opened.text
        assert opened.json()["running_work"] == expected
        assert client.get(path).json()[0]["running_work"] == expected

        # A recorded launch overrides the requested aliases: a Work turn in a
        # separate checkout must not be labelled as touching this repository.
        receipt = AgentTaskReceiptRecord(
            receipt_id=1,
            operation_id=running.operation_id,
            created_at=store.now(),
            tier="diagnostic",
            category="provider_launch",
            payload={"canonical_repository_roots": [str(tmp_path / "another-checkout")]},
        )
        monkeypatch.setattr(
            app.state.services.store,
            "agent_task_receipts",
            lambda operation_id: [receipt] if operation_id == running.operation_id else [],
        )
        assert client.get(path).json()[0]["running_work"] == []
        receipt.payload["canonical_repository_roots"] = [str((tmp_path / "repo").resolve())]
        assert client.get(path).json()[0]["running_work"] == expected


@pytest.mark.parametrize("os_name,expected", [("Linux", "mirrored"), ("Darwin", "cooperative")])
@pytest.mark.parametrize("space_kind", ["personal", "team"])
def test_projection_reports_machine_capability_independent_of_space(
    tmp_path, manifest, monkeypatch, os_name, expected, space_kind
):
    from rcp.terminals import launch

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: os_name)
    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    if space_kind == "team":
        app, client, _store, _people, _acting = _team_app(tmp_path)
        project_id = _create_project(client, tmp_path / "repo")
    else:
        app = create_app(str(manifest.path), data_dir=tmp_path / "personal")
        client = TestClient(app)
        project_id = app.state.default_project_id
    current = app.state.catalog.open(project_id).manifest
    current.machines.append(MachineConfig(alias="ssh", host="worker.invalid"))
    current.repositories.append(RepositoryConfig(alias="ssh-repo", machine="ssh", path="/repo"))
    repositories = client.get(f"/api/projects/{project_id}/terminals/repositories").json()
    local = repositories[0]
    assert local["eligible"] is True
    assert local["containment"] == expected
    assert local["backend_id"] == ("systemd_user" if expected == "mirrored" else "pty")
    assert local["backend_name"]
    assert local["machine_id"] == current.repositories[0].machine
    assert local["unavailable_reason"] is None
    if expected == "cooperative":
        assert "protection is unavailable" in local["reason"]
        assert "no filesystem fence" in local["reason"]
    else:
        assert "read-only mounts" in local["reason"]
        assert "accident resistance" in local["reason"]
    remote = repositories[-1]
    assert remote["eligible"] is False
    assert remote["containment"] is None
    assert remote["backend_id"] is None
    assert (
        remote["reason"] == remote["unavailable_reason"] == "PTY-over-SSH transport is not built."
    )


def test_cooperative_api_session_carries_missing_protection(tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Darwin")
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        response = client.post(path, json={"repository_id": "paper-repo"})
        assert response.status_code == 200, response.text
        session = response.json()
        assert session["containment"] == "cooperative"
        assert "protection is unavailable" in session["protection_notice"]
        assert "no filesystem fence" in session["protection_notice"]
        assert client.get(path).json()[0]["protection_notice"] == session["protection_notice"]
        assert client.delete(f"{path}/{session['session_id']}").status_code == 200
