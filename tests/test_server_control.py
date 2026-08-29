from __future__ import annotations

import json
import os
import socket
import stat
import struct
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from shutil import rmtree
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.limits import (
    SERVER_CONTROL_IO_TIMEOUT_SECONDS,
    SERVER_CONTROL_PROVIDER_CHECK_TIMEOUT_SECONDS,
)
from rcp.server_ops import control
from rcp.server_ops.control import (
    SERVER_CONTROL_MAX_REQUEST_BYTES,
    SERVER_CONTROL_OPERATIONS,
    SERVER_CONTROL_SOCKET_MODE,
    ServerControlClient,
    ServerControlError,
    ServerControlPeer,
    ServerControlProbeResult,
    ServerControlRequest,
    ServerControlServer,
)
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
from rcp.server_runtime import (
    ServerMetadata,
    ServerMetadataError,
    installed_control_socket_path,
    published_server_metadata,
)
from rcp.storage import AppStore


@pytest.fixture
def control_root() -> Path:
    path = Path(tempfile.mkdtemp(prefix="rcp-control-", dir="/tmp"))
    os.chown(path, os.geteuid(), os.getegid())
    path.chmod(0o700)
    try:
        yield path
    finally:
        rmtree(path)


def _team_app(tmp_path: Path, control_root: Path):
    data_dir = tmp_path / "data"
    AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Control lab")
    metadata = ServerMetadata.create(
        data_dir,
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        control_socket=control_root / "control.sock",
    )
    return data_dir, metadata, create_app(data_dir=data_dir, instance_metadata=metadata)


def test_team_lifespan_publishes_private_socket_without_opening_a_second_store(
    tmp_path: Path,
    control_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, metadata, app = _team_app(tmp_path, control_root)
    socket_path = Path(metadata.control_socket or "")

    with published_server_metadata(data_dir, metadata), TestClient(app):
        info = socket_path.lstat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == SERVER_CONTROL_SOCKET_MODE
        assert (info.st_uid, info.st_gid) == (os.geteuid(), os.getegid())

        def refuse_second_store(*_args, **_kwargs):
            raise AssertionError("the control client tried to open SQLite")

        monkeypatch.setattr(AppStore, "__init__", refuse_second_store)
        result = ServerControlClient.from_data_dir(
            data_dir,
            expected_server_uid=os.geteuid(),
        ).probe()

        assert result == ServerControlProbeResult(
            instance_id=metadata.instance_id,
            pid=os.getpid(),
            data_dir_id=metadata.data_dir_id,
            space_id=app.state.space_id,
            operations=SERVER_CONTROL_OPERATIONS,
        )
        assert set(result.model_dump()) == {
            "instance_id",
            "pid",
            "data_dir_id",
            "space_id",
            "space_kind",
            "operations",
        }

    assert not os.path.lexists(socket_path)


def test_control_probe_can_report_a_known_incomplete_operation_set() -> None:
    result = ServerControlProbeResult(
        instance_id=str(uuid.uuid4()),
        pid=os.getpid(),
        data_dir_id="d" * 64,
        space_id=str(uuid.uuid4()),
        operations=("probe",),
    )

    assert result.operations == ("probe",)
    with pytest.raises(ValueError, match="registry order"):
        ServerControlProbeResult(
            instance_id=str(uuid.uuid4()),
            pid=os.getpid(),
            data_dir_id="d" * 64,
            space_id=str(uuid.uuid4()),
            operations=("probe", "provider_readiness_check", "provider_readiness_plan"),
        )


def test_control_socket_is_refused_for_a_personal_or_non_cli_app(
    tmp_path: Path, control_root: Path
) -> None:
    personal_data = tmp_path / "personal"
    metadata = ServerMetadata.create(
        personal_data,
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        control_socket=control_root / "control.sock",
    )
    with pytest.raises(ValueError, match="only to an installed CLI-owned team service"):
        create_app(data_dir=personal_data, instance_metadata=metadata)

    team_data = tmp_path / "team"
    AppStore.initialize_team_space(team_data / "rcp.sqlite3", "Control lab")
    desktop = replace(
        metadata,
        owner_kind="desktop",
        data_dir_id=ServerMetadata.create(
            team_data, host="127.0.0.1", port=8421, owner_kind="desktop"
        ).data_dir_id,
    )
    with pytest.raises(ValueError, match="only to an installed CLI-owned team service"):
        create_app(data_dir=team_data, instance_metadata=desktop)


def test_provider_check_uses_its_bounded_operation_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = ServerMetadata.create(
        tmp_path / "data",
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        control_socket=tmp_path / "control.sock",
    )
    observed: list[float] = []

    class RefusingSocket:
        def settimeout(self, timeout: float) -> None:
            observed.append(timeout)

        def connect(self, _path: str) -> None:
            raise RuntimeError("stop after observing the timeout")

        def close(self) -> None:
            pass

    monkeypatch.setattr(control.socket, "socket", lambda *_args: RefusingSocket())
    client = ServerControlClient(
        metadata,
        expected_server_uid=os.geteuid(),
    )

    with pytest.raises(RuntimeError, match="stop after observing"):
        client.probe()
    with pytest.raises(RuntimeError, match="stop after observing"):
        client.check_provider_readiness(
            selector_kind="request",
            selector_id=str(uuid.uuid4()),
            boundary_sha256="a" * 64,
            target_id="b" * 64,
        )

    assert observed == [
        SERVER_CONTROL_IO_TIMEOUT_SECONDS,
        SERVER_CONTROL_PROVIDER_CHECK_TIMEOUT_SECONDS,
    ]


def test_installed_control_socket_is_discovered_only_for_the_service_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcp.server_ops.config as config_module
    import rcp.server_ops.layout as layout_module
    import rcp.server_runtime as runtime_module

    data_dir = tmp_path / "data"
    socket_path = Path("/run/rcp/control.sock")
    config_path = tmp_path / "server.toml"
    config_path.touch()
    monkeypatch.setattr(
        layout_module,
        "DEFAULT_SERVER_LAYOUT",
        replace(DEFAULT_SERVER_LAYOUT, config_path=config_path),
    )
    monkeypatch.setattr(
        config_module,
        "load_installed_server_config",
        lambda path: SimpleNamespace(
            service_account="rcp",
            paths=SimpleNamespace(data_dir=str(data_dir), control_socket=str(socket_path)),
        ),
    )
    account = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid())
    monkeypatch.setattr(runtime_module.pwd, "getpwnam", lambda _name: account)

    assert installed_control_socket_path(data_dir) == socket_path
    assert installed_control_socket_path(tmp_path / "other-data") is None

    account.pw_uid = os.geteuid() + 1
    with pytest.raises(ServerMetadataError, match="configured service account"):
        installed_control_socket_path(data_dir)


def test_unauthorized_os_peer_is_rejected_before_request_dispatch(control_root: Path) -> None:
    metadata = ServerMetadata.create(
        control_root / "data",
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        control_socket=control_root / "control.sock",
    )
    dispatched = False

    def handler(_request, _peer):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("unauthorized peers must not reach dispatch")

    server = ServerControlServer(
        control_root / "control.sock",
        instance_id=metadata.instance_id,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
        handler=handler,
        peer_resolver=lambda _connection: ServerControlPeer(
            pid=os.getpid(), uid=os.geteuid() + 1, gid=os.getegid()
        ),
    )
    server.start()
    try:
        with pytest.raises(ServerControlError) as caught:
            ServerControlClient(metadata, expected_server_uid=os.geteuid()).probe()
        assert caught.value.code == "unauthorized_peer"
        assert dispatched is False
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (struct.pack("!I", 1) + b"{", "invalid_request"),
        (struct.pack("!I", SERVER_CONTROL_MAX_REQUEST_BYTES + 1), "oversized_request"),
        (
            lambda instance_id: _framed_json(
                {
                    "protocol_version": 2,
                    "request_id": str(uuid.uuid4()),
                    "instance_id": instance_id,
                    "operation": "probe",
                    "member_id": str(uuid.uuid4()),
                }
            ),
            "invalid_request",
        ),
    ],
)
def test_malformed_oversized_and_member_claim_requests_fail_closed(
    control_root: Path,
    payload,
    expected_code: str,
) -> None:
    server, metadata = _standalone_server(control_root)
    server.start()
    try:
        raw = payload(metadata.instance_id) if callable(payload) else payload
        response = _raw_request(Path(metadata.control_socket or ""), raw)
        assert response["ok"] is False
        assert response["error"]["code"] == expected_code
    finally:
        server.stop()


def test_root_machine_peer_reaches_probe_without_becoming_a_member(control_root: Path) -> None:
    observed: list[ServerControlPeer] = []
    server, metadata = _standalone_server(
        control_root,
        peer_resolver=lambda _connection: ServerControlPeer(pid=os.getpid(), uid=0, gid=0),
        observed=observed,
    )
    server.start()
    try:
        result = ServerControlClient(metadata, expected_server_uid=os.geteuid()).probe()
        assert result.space_kind == "team"
        assert observed == [ServerControlPeer(pid=os.getpid(), uid=0, gid=0)]
        assert "member" not in json.dumps(result.model_dump())
        assert "user" not in json.dumps(result.model_dump())
    finally:
        server.stop()


def test_restart_recovers_a_safe_stale_socket(control_root: Path) -> None:
    socket_path = control_root / "control.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    os.chmod(socket_path, SERVER_CONTROL_SOCKET_MODE)
    stale.close()

    server, metadata = _standalone_server(control_root)
    server.start()
    try:
        assert ServerControlClient(
            metadata, expected_server_uid=os.geteuid()
        ).probe().instance_id == (metadata.instance_id)
    finally:
        server.stop()
    assert not os.path.lexists(socket_path)


def test_shutdown_does_not_remove_a_replacement_socket(control_root: Path) -> None:
    server, metadata = _standalone_server(control_root)
    socket_path = Path(metadata.control_socket or "")
    server.start()
    socket_path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        replacement.bind(str(socket_path))
        os.chmod(socket_path, SERVER_CONTROL_SOCKET_MODE)
        server.stop()
        assert socket_path.exists()
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
    finally:
        replacement.close()
        socket_path.unlink(missing_ok=True)


def _standalone_server(
    control_root: Path,
    *,
    peer_resolver=control.unix_peer_identity,
    observed: list[ServerControlPeer] | None = None,
) -> tuple[ServerControlServer, ServerMetadata]:
    metadata = ServerMetadata.create(
        control_root / "data",
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        control_socket=control_root / "control.sock",
    )

    def handler(request: ServerControlRequest, peer: ServerControlPeer):
        if observed is not None:
            observed.append(peer)
        return ServerControlProbeResult(
            instance_id=request.instance_id,
            pid=os.getpid(),
            data_dir_id=metadata.data_dir_id,
            space_id=str(uuid.uuid4()),
            operations=SERVER_CONTROL_OPERATIONS,
        )

    return (
        ServerControlServer(
            control_root / "control.sock",
            instance_id=metadata.instance_id,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
            handler=handler,
            peer_resolver=peer_resolver,
        ),
        metadata,
    )


def _framed_json(value: object) -> bytes:
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return struct.pack("!I", len(body)) + body


def _raw_request(path: Path, payload: bytes) -> dict[str, object]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(str(path))
        connection.sendall(payload)
        header = _receive_exact(connection, 4)
        (size,) = struct.unpack("!I", header)
        return json.loads(_receive_exact(connection, size))
    finally:
        connection.close()


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    body = b""
    while len(body) < size:
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise AssertionError("control server closed an incomplete response")
        body += chunk
    return body
