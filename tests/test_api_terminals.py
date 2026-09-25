"""Membership and transport checks for project member terminals."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rcp.api.app import create_app
from rcp.api.dependencies import get_project_service
from rcp.config import MachineConfig, RepositoryConfig
from rcp.storage import AppStore
from rcp.terminals import launch
from rcp.terminals.probe import TerminalProbe

from .helpers import wait_until
from .test_project_membership import _create_project as _create_membership_project
from .test_project_membership import _team_app
from .test_team_project_provisioning import _test_server_layout


def _create_project(client, repository_path, **kwargs):
    project_id = _create_membership_project(client, repository_path, **kwargs)
    client.app.state.server_layout = _test_server_layout(repository_path.parent / "installation")
    key = client.app.state.server_layout.project_deploy_key_path(project_id, "paper-repo")
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("terminal-fixture-key")
    git = repository_path / ".git"
    (git / "objects").mkdir(parents=True, exist_ok=True)
    (git / "refs").mkdir(exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    return project_id


@pytest.fixture(autouse=True)
def remote_probe(monkeypatch):
    """No API fixture may reach a real SSH host, including cold-cache work."""
    probe = Mock(
        return_value=TerminalProbe(None, "unreachable", "SSH connection refused by test double.")
    )
    monkeypatch.setattr("rcp.terminals.probe.probe_remote_terminal", probe)
    popen = subprocess.Popen

    def guarded_popen(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and Path(command[0]).name == "ssh":
            pytest.fail("API terminal test attempted a real SSH connection")
        return popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    return probe


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "/repositories", None),
        ("GET", "", None),
        ("POST", "", {"repository_id": "paper-repo"}),
        ("POST", "/probe", None),
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
    assert response.json() == unknown.json()


def _lifecycle_frame(socket):
    """The next JSON frame, skipping PTY bytes still queued ahead of it.

    A PTY read can split output across frames, so a binary frame can sit
    between the output a test consumed and the lifecycle frame it waits for.
    """
    while True:
        message = socket.receive()
        if message.get("type") == "websocket.close":
            raise AssertionError(f"socket closed before a lifecycle frame: {message}")
        if "text" in message:
            return json.loads(message["text"])


def _open_terminal(client, path, repository_id="paper-repo"):
    opened = client.post(path, json={"repository_id": repository_id})
    assert opened.status_code == 200, opened.text
    return opened.json()


def _repoint(app, project_id, tmp_path):
    """Register the alias on another checkout, so its running shell is stale."""
    moved = tmp_path / "moved-checkout"
    (moved / ".research").mkdir(parents=True)
    manifest = get_project_service(app.state.services.catalog, project_id).manifest
    manifest.repository_map["paper-repo"].path = str(moved)
    return moved


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


@pytest.mark.parametrize(
    "state,diagnostic",
    [
        ("unreachable", "SSH connection refused."),
        ("authentication_failed", "Permission denied (publickey)."),
        ("host_key_failed", "Host key verification failed."),
        ("incapable", "The execution account requires a lingering systemd user manager."),
    ],
)
def test_remote_probe_failure_is_projected_without_launch(
    tmp_path, remote_probe, monkeypatch, state, diagnostic
):
    remote_probe.return_value = TerminalProbe(
        "Linux" if state == "incapable" else None, state, diagnostic
    )
    launcher = Mock(side_effect=AssertionError("Unavailable remote must not launch a shell"))
    monkeypatch.setattr("rcp.terminals.remote.start_remote", launcher)
    monkeypatch.setattr("rcp.terminals.backends.TerminalBackend.start", launcher)
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    manifest = app.state.catalog.open(project_id).manifest
    manifest.machines.append(MachineConfig(alias="remote", host="compute.example"))
    manifest.repositories.append(
        RepositoryConfig(alias="remote-repo", machine="remote", path="/srv/repo")
    )

    path = f"/api/projects/{project_id}/terminals"
    with client:
        refused = client.post(path, json={"repository_id": "remote-repo"})
        assert refused.status_code == 409
        response = client.get(f"{path}/repositories")
        assert response.status_code == 200, response.text
        repositories = {item["repository_id"]: item for item in response.json()}
        assert repositories["paper-repo"]["path"] == str(tmp_path / "repo")
        remote = repositories["remote-repo"]
        assert remote["eligible"] is False
        assert remote["path"] == "/srv/repo"
        assert remote["probe_state"] == state
        assert remote["containment"] is None
        assert remote["unavailable_reason"] == diagnostic == refused.json()["detail"]
        assert client.get(path).json() == []
        launcher.assert_not_called()
        remote_probe.assert_called_once()


@pytest.fixture
def terminal_pty(monkeypatch):
    """Keep real session/transport owners, replacing only Linux process launch."""

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
        session_id = _open_terminal(client, path)["session_id"]
        assert _open_terminal(client, path)["session_id"] == session_id
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
            assert _lifecycle_frame(socket)["type"] == "ended"
        assert client.get(path).json() == []
        assert client.delete(f"{path}/{session_id}").status_code == 404


def test_live_membership_loss_closes_socket_before_more_input(tmp_path, terminal_pty):
    app, client, store, people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        session_id = _open_terminal(client, path)["session_id"]
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


def test_live_membership_loss_stops_output_too(tmp_path, terminal_pty, monkeypatch):
    """Input is not the only direction that must fail closed: a revoked viewer
    must stop receiving PTY bytes as well.
    """
    monkeypatch.setattr("rcp.api.terminals.TERMINAL_OUTPUT_ADMISSION_INTERVAL_SECONDS", 0.0)
    app, client, store, people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        session_id = _open_terminal(client, path)["session_id"]
        with client.websocket_connect(
            f"{path}/{session_id}/ws", headers={"Origin": "http://testserver"}
        ) as socket:
            with store.connection() as connection:
                connection.execute(
                    "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
                    (project_id, people[0].user_id),
                )
            os.write(terminal_pty[0], b"secret output after revocation\n")
            with pytest.raises(WebSocketDisconnect) as refused:
                for _ in range(4):
                    assert b"secret output" not in socket.receive_bytes()
            assert refused.value.code == 4404
        client.portal.call(app.state.services.terminals.sweep)
        assert app.state.services.terminals.list(project_id) == []


def test_a_live_session_is_returned_even_when_its_machine_now_probes_badly(
    tmp_path, terminal_pty, monkeypatch
):
    """Open-or-return-existing: launch prerequisites gate a launch, not handing
    back a session that is already running.
    """
    from rcp.terminals.backends import TerminalCapability

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        opened = _open_terminal(client, path)

        async def unavailable(machine, probe):
            return TerminalCapability(None, "Machine temporarily unavailable.")

        monkeypatch.setattr(app.state.services.terminals, "capability", unavailable)
        again = client.post(path, json={"repository_id": "paper-repo"})
        assert again.status_code == 200, again.text
        assert again.json()["session_id"] == opened["session_id"]


def test_a_repointed_alias_retires_its_shell_even_when_the_new_machine_fails(
    tmp_path, terminal_pty, monkeypatch
):
    """The inverse of returning a live session: once the alias names another
    checkout, the running shell is no longer what was asked for. Every check
    after that belongs to the new registration, so one of them failing must
    not answer with an error while the old shell stays listed and attachable
    on the checkout the alias has left.
    """
    from rcp.terminals.backends import TerminalCapability

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        _open_terminal(client, path)

        _repoint(app, project_id, tmp_path)

        async def unavailable(machine, probe):
            return TerminalCapability(None, "Machine temporarily unavailable.")

        monkeypatch.setattr(app.state.services.terminals, "capability", unavailable)
        refused = client.post(path, json={"repository_id": "paper-repo"})
        assert refused.status_code == 409, refused.text
        listed = client.get(path)
        assert listed.status_code == 200, listed.text
        assert [session["session_id"] for session in listed.json()] == []


def test_a_stale_session_that_cannot_be_stopped_is_an_operational_failure(
    tmp_path, terminal_pty, monkeypatch
):
    """Retiring a repointed session happens before the route's own error
    mapping, so a stop that fails has to be mapped where it is raised. The
    session is correctly kept for retry either way; what is at stake is
    whether the member is told the machine is busy or that RCP broke.
    """

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        _open_terminal(client, path)

        _repoint(app, project_id, tmp_path)

        def unstoppable(unit):
            raise RuntimeError("systemctl user manager unavailable")

        monkeypatch.setattr(launch, "stop_unit", unstoppable)
        refused = client.post(path, json={"repository_id": "paper-repo"})
        assert refused.status_code == 503, refused.text
        assert "systemctl" in refused.json()["detail"]


@pytest.mark.parametrize("change", ["repointed", "unregistered"])
def test_a_stale_alias_stops_listing_and_attaching_without_an_open_request(
    tmp_path, terminal_pty, change
):
    """The listing is what a member watches, and the Open control is hidden
    for any repository that already has a session. A session whose alias has
    stopped naming it therefore hides the only control that would replace it,
    so nothing but the listing itself can settle the registration.
    """

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        session_id = _open_terminal(client, path)["session_id"]
        assert [item["session_id"] for item in client.get(path).json()] == [session_id]

        manifest = get_project_service(app.state.services.catalog, project_id).manifest
        if change == "repointed":
            moved = tmp_path / "moved-checkout"
            (moved / ".research").mkdir(parents=True)
            manifest.repository_map["paper-repo"].path = str(moved)
        else:
            # repository_map is computed from repositories, so the alias has
            # to leave the list it is computed from.
            manifest.repositories = [
                item for item in manifest.repositories if item.alias != "paper-repo"
            ]

        # No POST is issued: the UI could not offer one.
        assert client.get(path).json() == []
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(
                f"{path}/{session_id}/ws", headers={"Origin": "http://testserver"}
            ),
        ):
            pytest.fail("A stale alias was attached to")
        assert refused.value.code == 4404
        receipt = json.loads(
            (app.state.services.terminals.directory / f"{session_id}.json").read_text()
        )
        assert receipt["termination_reason"] == f"repository_{change}"


def test_a_repoint_during_an_open_is_settled_by_the_socket_itself(tmp_path, terminal_pty):
    """A POST reads the manifest once, and the probe and launch behind it can
    outlast a repoint, so the session it answers with may already name a
    checkout the project has stopped naming. The socket the UI attaches
    through is reached by id, so it settles the registration itself rather
    than trusting that a poll already has: the stale shell is retired instead
    of handed over.
    """

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        session_id = _open_terminal(client, path)["session_id"]
        moved = _repoint(app, project_id, tmp_path)

        # No listing intervenes: the socket is the first thing to look.
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(
                f"{path}/{session_id}/ws", headers={"Origin": "http://testserver"}
            ),
        ):
            pytest.fail("A shell on the checkout the alias left was handed over")
        assert refused.value.code == 4404
        receipt = json.loads(
            (app.state.services.terminals.directory / f"{session_id}.json").read_text()
        )
        assert receipt["termination_reason"] == "repository_repointed"
        assert receipt["declared_path"] != str(moved)


def test_a_stale_alias_that_cannot_be_stopped_stops_listing_and_attaching(
    tmp_path, terminal_pty, monkeypatch
):
    """Retiring a repointed session can fail on its machine, and the listing
    and the socket only log that. The decision to end it stands, so it must
    stop being one a member can list or attach to while the stop is retried;
    otherwise they keep a shell in a checkout the project no longer registers.
    """

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        session_id = _open_terminal(client, path)["session_id"]
        _repoint(app, project_id, tmp_path)

        def unstoppable(unit):
            raise RuntimeError("systemctl user manager unavailable")

        monkeypatch.setattr(launch, "stop_unit", unstoppable)
        assert client.get(path).json() == []
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(
                f"{path}/{session_id}/ws", headers={"Origin": "http://testserver"}
            ),
        ):
            pytest.fail("A shell the project no longer registers was attached to")
        assert refused.value.code == 4404
        # The runtime is kept, so the stop it could not finish is retried.
        assert session_id in app.state.services.terminals.sessions
        monkeypatch.setattr(launch, "stop_unit", lambda unit: None)


def test_a_viewer_is_let_go_when_retirement_fails(tmp_path, terminal_pty, monkeypatch):
    """Refusing the next attach does nothing for a browser already attached:
    its socket reads a queue without consulting the manager again, and its own
    recheck is of membership. A stale session whose stop failed has to let its
    viewer go rather than keep feeding it a checkout the project no longer
    registers.
    """

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    path = f"/api/projects/{project_id}/terminals"
    with client:
        session_id = _open_terminal(client, path)["session_id"]
        with client.websocket_connect(
            f"{path}/{session_id}/ws", headers={"Origin": "http://testserver"}
        ) as socket:
            _repoint(app, project_id, tmp_path)

            def unstoppable(unit):
                raise RuntimeError("systemctl user manager unavailable")

            monkeypatch.setattr(launch, "stop_unit", unstoppable)
            # The listing settles the registration, and the stop fails.
            assert client.get(path).json() == []
            assert _lifecycle_frame(socket)["type"] == "ended"
        assert session_id in app.state.services.terminals.sessions
        monkeypatch.setattr(launch, "stop_unit", lambda unit: None)


def test_missing_systemd_is_reported_without_creating_a_shell(tmp_path, monkeypatch):

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
        opened = _open_terminal(client, path)
        assert opened["running_work"] == expected
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
    remote = repositories[-1]
    assert remote["eligible"] is False
    assert remote["containment"] is None
    assert remote["backend_id"] is None
    assert remote["probe_state"] == "pending"
    assert remote["reason"] == remote["unavailable_reason"]


def test_cooperative_api_session_carries_missing_protection(tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Darwin")
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    from rcp.terminals import manager, profile
    from rcp.terminals.backends import TerminalBackend

    start = TerminalBackend.start
    captured = {}

    def capture_start(self, **kwargs):
        captured.update(kwargs)
        return start(self, **kwargs)

    write_identity = manager.write_git_identity

    def capture_identity(*args, git_path):
        assert f"PATH={git_path}" in profile.shell_environment({})
        return write_identity(*args, git_path=git_path)

    monkeypatch.setattr(manager, "write_git_identity", capture_identity)
    monkeypatch.setattr(TerminalBackend, "start", capture_start)
    path = f"/api/projects/{project_id}/terminals"
    with client:
        response = client.post(path, json={"repository_id": "paper-repo"})
        assert response.status_code == 200, response.text
        session = response.json()
        identity_path = captured["git_environment"]["GIT_CONFIG_SYSTEM"]
        assert identity_path in captured["git_read_paths"]
        assert "GIT_SSH_COMMAND" not in captured["git_environment"]
        assert _people[0].display_name in Path(identity_path).read_text()
        assert f"{_people[0].user_id}@members.rcp.invalid" in Path(identity_path).read_text()
        assert session["containment"] == "cooperative"
        assert client.get(path).json()[0]["protection_notice"] == session["protection_notice"]
        assert client.delete(f"{path}/{session['session_id']}").status_code == 200


def _register_remote(app, project_id, *, repositories=1):
    manifest = app.state.catalog.open(project_id).manifest
    machine = MachineConfig(alias="remote", host="compute.example")
    manifest.machines.append(machine)
    manifest.repositories.extend(
        RepositoryConfig(alias=f"remote-{index}", machine=machine.alias, path=f"/srv/repo-{index}")
        for index in range(repositories)
    )
    return machine


def test_remote_projection_probes_once_per_machine_and_refreshes(tmp_path, remote_probe):
    release = threading.Event()
    ready = TerminalProbe("Linux", "reachable", "Remote prerequisites available.")

    def probe(_machine):
        assert release.wait(5), "Projection blocked on its capability probe"
        return ready

    remote_probe.side_effect = probe
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    machine = _register_remote(app, project_id, repositories=2)
    path = f"/api/projects/{project_id}/terminals"
    with client:
        try:
            pending = client.get(f"{path}/repositories").json()[1:]
            assert len(pending) == 2
            assert all(item["probe_state"] == "pending" for item in pending)
            assert all(not item["eligible"] for item in pending)
            assert all(item["reason"] for item in pending)
        finally:
            release.set()
        client.portal.call(app.state.services.terminals.probes.ensure, machine)
        for _ in range(2):
            projected = client.get(f"{path}/repositories").json()[1:]
            assert all(item["probe_state"] == "reachable" for item in projected)
            assert all(item["eligible"] for item in projected)
            assert all(item["os_name"] == "Linux" for item in projected)
            assert all(item["containment"] == "mirrored" for item in projected)
        remote_probe.assert_called_once()

        remote_probe.side_effect = None
        remote_probe.return_value = TerminalProbe(
            None, "authentication_failed", "Permission denied (publickey)."
        )
        refreshed = client.post(f"{path}/probe")
        assert refreshed.status_code == 200, refreshed.text
        assert all(item["probe_state"] == "pending" for item in refreshed.json()[1:])
        client.portal.call(app.state.services.terminals.probes.ensure, machine)
        unavailable = client.get(f"{path}/repositories").json()[1:]
        assert all(item["probe_state"] == "authentication_failed" for item in unavailable)
        assert all(item["reason"] == "Permission denied (publickey)." for item in unavailable)
        assert remote_probe.call_count == 2


@pytest.mark.parametrize(
    "local_os,remote_os,profile",
    [("Darwin", "Linux", "mirrored"), ("Linux", "Darwin", "cooperative")],
)
def test_remote_projection_uses_probed_os(
    tmp_path, remote_probe, monkeypatch, local_os, remote_os, profile
):
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: local_os)
    monkeypatch.setattr("rcp.terminals.launch.availability_diagnostic", lambda: None)
    remote_probe.return_value = TerminalProbe(
        remote_os, "reachable", "Remote prerequisites available."
    )
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    machine = _register_remote(app, project_id)
    with client:
        client.portal.call(app.state.services.terminals.probes.ensure, machine)
        remote = client.get(f"/api/projects/{project_id}/terminals/repositories").json()[-1]
        assert remote["eligible"]
        assert remote["os_name"] == remote_os
        assert remote["containment"] == profile


@pytest.fixture
def remote_pty(monkeypatch, remote_probe):
    """Keep HTTP, WS, ownership, and PTY readers real; double remote operations."""
    remote_probe.return_value = TerminalProbe(
        "Linux", "reachable", "Remote prerequisites available."
    )
    opened = []

    def canonical_directories(_stage, paths, *, require_writable):
        return {path: path for path in paths}, "/home/execution-account"

    def start(host, **settings):
        master, slave = os.openpty()
        os.set_blocking(master, False)
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = lambda: setattr(process.poll, "return_value", 0)
        opened.append((process, slave, host, settings))
        return process, master

    monkeypatch.setattr(
        "rcp.transport.run_stage.RemoteRunStage.canonical_directories", canonical_directories
    )
    monkeypatch.setattr("rcp.terminals.remote.start_remote", start)
    monkeypatch.setattr("rcp.terminals.remote.stop_remote_unit", lambda *args: None)
    yield opened
    for _process, slave, _host, _settings in opened:
        os.close(slave)


@pytest.mark.parametrize("completion", [False, True], ids=["link-drop", "shell-exit-255"])
def test_remote_websocket_distinguishes_link_drop_from_shell_exit(
    tmp_path, remote_pty, completion, monkeypatch
):
    from rcp.agents.write_scope import registered_repository_roots
    from rcp.transport.remote_terminal import EXIT_PREFIX, EXIT_SUFFIX

    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    machine = _register_remote(app, project_id)
    monkeypatch.setattr(
        app.state.catalog,
        "repository_ownership_inventory",
        lambda: registered_repository_roots(
            app.state.catalog.open(project_id).manifest, project_id=project_id
        ),
    )
    path = f"/api/projects/{project_id}/terminals"
    with client:
        opened = _open_terminal(client, path, "remote-0")
        session_id = opened["session_id"]
        assert opened["containment"] == "mirrored"
        process, slave, host, settings = remote_pty[0]
        assert host == machine.host
        assert settings["repository"] == Path("/srv/repo-0")
        assert "/srv/repo-0/.research" in settings["protected_paths"]
        socket_path = f"{path}/{session_id}/ws"
        with client.websocket_connect(
            socket_path, headers={"Origin": "http://testserver"}
        ) as socket:
            os.write(slave, b"remote-output\n")
            assert b"remote-output" in socket.receive_bytes()
            socket.send_json({"type": "resize", "cols": 100, "rows": 40})
            wait_until(lambda: os.get_terminal_size(slave) == (100, 40))
            if completion:
                os.write(slave, EXIT_PREFIX + b"255" + EXIT_SUFFIX)
                wait_until(
                    lambda: (
                        app.state.services.terminals.sessions[session_id].completion.exit_code
                        == 255
                    )
                )
            process.poll.return_value = 255
            client.portal.call(app.state.services.terminals.sweep)
            ended = _lifecycle_frame(socket)
            assert ended["type"] == "ended"
        assert client.get(path).json() == []
        metadata = json.loads(
            (app.state.services.terminals.directory / f"{session_id}.json").read_text()
        )
        assert metadata["termination_reason"] == ("shell_exited" if completion else "link_dropped")
        assert metadata["ended_at"]
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(socket_path, headers={"Origin": "http://testserver"}),
        ):
            pytest.fail("An ended SSH session was silently reconnected")
        assert refused.value.code == 4404
        assert len(remote_pty) == 1


def test_remote_symlinked_repository_keeps_its_work_collision_warning(monkeypatch):
    """A receipt names the far side's canonical root, which we cannot resolve.

    Matching a remote repository by declared path alone would silently drop the
    running-Work warning for a symlinked checkout, so the declared run scope
    stays a second chance.
    """
    from rcp.api import terminal_projection

    class _Task:
        active, queued, kind = True, False, "chat"
        operation_id, status_message = "op-1", "Refit the damping window"
        request = {"mode": "work", "run_truth_scope": ["code"], "run_on": "remote-1"}

    class _Receipt:
        payload = {"canonical_repository_roots": ["/canonical/elsewhere/code"]}

    class _Store:
        def all_project_agent_tasks(self, project_id):
            return [_Task()]

        def agent_task_receipts(self, operation_id):
            return [_Receipt()]

    class _Repo:
        alias, machine, path = "code", "remote-1", "/symlinked/code"

    class _Machine:
        host = "example.invalid"

    class _Manifest:
        repositories = [_Repo()]
        machine_map = {"remote-1": _Machine()}

    work = terminal_projection.running_repository_work(_Store(), "p1", _Manifest())
    assert [entry["operation_id"] for entry in work["code"]] == ["op-1"]


def test_terminal_missing_deploy_key_names_provisioning_action(tmp_path, terminal_pty):
    app, client, _store, _people, _acting = _team_app(tmp_path)
    project_id = _create_membership_project(client, tmp_path / "repo")
    with client:
        response = client.post(
            f"/api/projects/{project_id}/terminals", json={"repository_id": "paper-repo"}
        )
    assert response.status_code == 503
    assert not terminal_pty
