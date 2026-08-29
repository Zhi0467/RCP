"""Private, kernel-authenticated control transport for one installed team service."""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import struct
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.limits import (
    SERVER_CONTROL_ACCEPT_POLL_INTERVAL_SECONDS,
    SERVER_CONTROL_IO_TIMEOUT_SECONDS,
    SERVER_CONTROL_STOP_TIMEOUT_SECONDS,
)
from rcp.server_runtime import ServerMetadata, read_server_metadata

SERVER_CONTROL_PROTOCOL_VERSION = 1
SERVER_CONTROL_MAX_REQUEST_BYTES = 64 * 1024
SERVER_CONTROL_MAX_RESPONSE_BYTES = 256 * 1024
SERVER_CONTROL_SOCKET_MODE = 0o600
SERVER_CONTROL_RUNTIME_MODE = 0o700
SERVER_CONTROL_MAX_SOCKET_PATH_BYTES = 99

_FRAME_HEADER = struct.Struct("!I")
_HEX_DIGEST = frozenset("0123456789abcdef")


class ServerControlError(RuntimeError):
    """A bounded control request was refused or could not complete."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServerControlUnavailable(ServerControlError):
    """The private installed-service transport is unavailable."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _canonical_uuid4(value: str, *, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{label} must be a lowercase, hyphenated canonical UUID4")
    return value


class ServerControlRequest(_StrictModel):
    protocol_version: Literal[SERVER_CONTROL_PROTOCOL_VERSION] = SERVER_CONTROL_PROTOCOL_VERSION
    request_id: str
    instance_id: str
    operation: Literal["probe"]

    @model_validator(mode="after")
    def validate_ids(self) -> ServerControlRequest:
        _canonical_uuid4(self.request_id, label="control request id")
        _canonical_uuid4(self.instance_id, label="control instance id")
        return self


class ServerControlProbeResult(_StrictModel):
    instance_id: str
    pid: int = Field(gt=0)
    data_dir_id: str
    space_id: str
    space_kind: Literal["team"] = "team"

    @model_validator(mode="after")
    def validate_identity(self) -> ServerControlProbeResult:
        _canonical_uuid4(self.instance_id, label="control instance id")
        _canonical_uuid4(self.space_id, label="space id")
        if len(self.data_dir_id) != 64 or any(
            character not in _HEX_DIGEST for character in self.data_dir_id
        ):
            raise ValueError("data directory identity must be a lowercase SHA-256 digest")
        return self


class ServerControlFailure(_StrictModel):
    code: Literal[
        "invalid_request",
        "oversized_request",
        "operation_failed",
        "unauthorized_peer",
        "wrong_instance",
    ]
    message: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_message(self) -> ServerControlFailure:
        if any(ord(character) < 32 or ord(character) == 127 for character in self.message):
            raise ValueError("control error messages must be one safe line")
        return self


class ServerControlResponse(_StrictModel):
    protocol_version: Literal[SERVER_CONTROL_PROTOCOL_VERSION] = SERVER_CONTROL_PROTOCOL_VERSION
    request_id: str | None
    instance_id: str
    ok: bool
    result: ServerControlProbeResult | None = None
    error: ServerControlFailure | None = None

    @model_validator(mode="after")
    def validate_response(self) -> ServerControlResponse:
        _canonical_uuid4(self.instance_id, label="control instance id")
        if self.request_id is not None:
            _canonical_uuid4(self.request_id, label="control request id")
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("successful control responses require exactly one result")
        if not self.ok and (self.result is not None or self.error is None):
            raise ValueError("control responses must contain exactly one result or error")
        if self.result is not None and self.request_id is None:
            raise ValueError("successful control responses require a request id")
        return self


@dataclass(frozen=True)
class ServerControlPeer:
    pid: int
    uid: int
    gid: int | None

    def __post_init__(self) -> None:
        if (
            isinstance(self.pid, bool)
            or self.pid <= 0
            or isinstance(self.uid, bool)
            or self.uid < 0
            or (self.gid is not None and (isinstance(self.gid, bool) or self.gid < 0))
        ):
            raise ValueError("control peer credentials must contain valid kernel ids")


ServerControlHandler = Callable[
    [ServerControlRequest, ServerControlPeer],
    ServerControlProbeResult,
]
PeerResolver = Callable[[socket.socket], ServerControlPeer]


def unix_peer_identity(connection: socket.socket) -> ServerControlPeer:
    """Read credentials supplied by the Unix-domain socket implementation."""

    if os.name == "posix" and hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid, uid, gid = struct.unpack("3i", raw)
        return ServerControlPeer(pid=pid, uid=uid, gid=gid)
    if sys.platform == "darwin":
        pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
        credential = connection.getsockopt(0, 1, 8)
        _version, uid = struct.unpack("II", credential[:8])
        return ServerControlPeer(pid=pid, uid=uid, gid=None)
    raise ServerControlUnavailable(
        "peer_credentials_unavailable",
        "This operating system cannot authenticate private control-socket peers.",
    )


class ServerControlClient:
    """One-request client that discovers the socket without opening SQLite."""

    def __init__(
        self,
        metadata: ServerMetadata,
        *,
        expected_server_uid: int,
        peer_resolver: PeerResolver = unix_peer_identity,
    ) -> None:
        if metadata.control_socket is None:
            raise ServerControlUnavailable(
                "control_socket_unavailable",
                "The running RCP process does not publish an installed-service control socket.",
            )
        if isinstance(expected_server_uid, bool) or expected_server_uid < 0:
            raise ValueError("the expected control-server uid must be a nonnegative integer")
        self.metadata = metadata
        self.socket_path = Path(metadata.control_socket)
        self.expected_server_uid = expected_server_uid
        self.peer_resolver = peer_resolver

    @classmethod
    def from_data_dir(
        cls,
        data_dir: Path,
        *,
        expected_server_uid: int,
        peer_resolver: PeerResolver = unix_peer_identity,
    ) -> ServerControlClient:
        return cls(
            read_server_metadata(data_dir),
            expected_server_uid=expected_server_uid,
            peer_resolver=peer_resolver,
        )

    def probe(self) -> ServerControlProbeResult:
        request = ServerControlRequest(
            request_id=str(uuid.uuid4()),
            instance_id=self.metadata.instance_id,
            operation="probe",
        )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(SERVER_CONTROL_IO_TIMEOUT_SECONDS)
        try:
            connection.connect(str(self.socket_path))
            peer = self.peer_resolver(connection)
            if peer.uid != self.expected_server_uid:
                raise ServerControlUnavailable(
                    "wrong_server_identity",
                    "The control socket is not owned by the expected RCP service process.",
                )
            _send_model(
                connection,
                request,
                maximum=SERVER_CONTROL_MAX_REQUEST_BYTES,
            )
            response = _receive_response(connection)
        except ServerControlError:
            raise
        except (OSError, TimeoutError) as exc:
            raise ServerControlUnavailable(
                "control_socket_unavailable",
                "The running RCP control socket is unavailable.",
            ) from exc
        finally:
            connection.close()
        if response.instance_id != request.instance_id:
            raise ServerControlError(
                "wrong_instance",
                "The control response came from a different RCP process instance.",
            )
        if response.request_id not in {None, request.request_id}:
            raise ServerControlError(
                "wrong_request",
                "The control response does not match this request.",
            )
        if not response.ok:
            assert response.error is not None
            raise ServerControlError(response.error.code, response.error.message)
        assert response.result is not None
        if response.result.pid != peer.pid:
            raise ServerControlError(
                "wrong_server_identity",
                "The control response does not match the kernel-authenticated server process.",
            )
        return response.result


class ServerControlServer:
    """Single-process owner of one private Unix-domain control socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        instance_id: str,
        owner_uid: int,
        owner_gid: int,
        handler: ServerControlHandler,
        peer_resolver: PeerResolver = unix_peer_identity,
    ) -> None:
        _canonical_uuid4(instance_id, label="control instance id")
        if any(isinstance(value, bool) or value < 0 for value in (owner_uid, owner_gid)):
            raise ValueError("control socket owner ids must be nonnegative integers")
        self.socket_path = _validated_socket_path(socket_path)
        self.instance_id = instance_id
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.handler = handler
        self.peer_resolver = peer_resolver
        self._listener: socket.socket | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the server control socket is already started")
        _validate_runtime_directory(
            self.socket_path.parent,
            owner_uid=self.owner_uid,
            owner_gid=self.owner_gid,
        )
        self._recover_stale_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, SERVER_CONTROL_SOCKET_MODE)
            info = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != self.owner_uid
                or info.st_gid != self.owner_gid
                or stat.S_IMODE(info.st_mode) != SERVER_CONTROL_SOCKET_MODE
            ):
                raise ServerControlUnavailable(
                    "unsafe_control_socket",
                    "The private control socket has unsafe ownership or mode.",
                )
            listener.listen(16)
            listener.settimeout(SERVER_CONTROL_ACCEPT_POLL_INTERVAL_SECONDS)
            self._listener = listener
            self._socket_identity = (info.st_dev, info.st_ino)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._serve,
                name="rcp-server-control",
                daemon=False,
            )
            self._thread.start()
        except Exception:
            listener.close()
            self._remove_owned_socket()
            self._listener = None
            self._socket_identity = None
            raise

    def stop(self) -> None:
        thread = self._thread
        listener = self._listener
        if thread is None:
            return
        self._stop.set()
        if listener is not None:
            listener.close()
        thread.join(timeout=SERVER_CONTROL_STOP_TIMEOUT_SECONDS)
        if thread.is_alive():
            raise RuntimeError("the private control server did not stop at a durable boundary")
        self._remove_owned_socket()
        self._thread = None
        self._listener = None
        self._socket_identity = None

    def _serve(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set() or exc.errno in {errno.EBADF, errno.EINVAL}:
                    return
                continue
            with connection:
                connection.settimeout(SERVER_CONTROL_IO_TIMEOUT_SECONDS)
                self._serve_one(connection)

    def _serve_one(self, connection: socket.socket) -> None:
        request_id: str | None = None
        try:
            peer = self.peer_resolver(connection)
            if peer.uid not in {0, self.owner_uid}:
                self._send_error(
                    connection,
                    request_id=None,
                    code="unauthorized_peer",
                    message="This operating-system account cannot use the RCP control socket.",
                )
                return
            raw = _receive_json(connection, maximum=SERVER_CONTROL_MAX_REQUEST_BYTES)
            try:
                request = ServerControlRequest.model_validate(raw)
            except Exception:
                self._send_error(
                    connection,
                    request_id=None,
                    code="invalid_request",
                    message="The control request has an unsupported shape.",
                )
                return
            request_id = request.request_id
            if request.instance_id != self.instance_id:
                self._send_error(
                    connection,
                    request_id=request_id,
                    code="wrong_instance",
                    message="The control request names a different RCP process instance.",
                )
                return
            try:
                result = ServerControlProbeResult.model_validate(self.handler(request, peer))
                if result.instance_id != self.instance_id:
                    raise ValueError("control handler returned a different process instance")
            except Exception:
                self._send_error(
                    connection,
                    request_id=request_id,
                    code="operation_failed",
                    message="The named control operation failed inside the running RCP process.",
                )
                return
            _send_model(
                connection,
                ServerControlResponse(
                    request_id=request_id,
                    instance_id=self.instance_id,
                    ok=True,
                    result=result,
                ),
                maximum=SERVER_CONTROL_MAX_RESPONSE_BYTES,
            )
        except ServerControlError as exc:
            code = "oversized_request" if exc.code == "oversized_frame" else "invalid_request"
            self._send_error(
                connection,
                request_id=request_id,
                code=code,
                message=(
                    "The control request exceeds its fixed size limit."
                    if code == "oversized_request"
                    else "The control request is malformed or incomplete."
                ),
            )
        except (OSError, TimeoutError):
            return

    def _send_error(
        self,
        connection: socket.socket,
        *,
        request_id: str | None,
        code: Literal[
            "invalid_request",
            "oversized_request",
            "operation_failed",
            "unauthorized_peer",
            "wrong_instance",
        ],
        message: str,
    ) -> None:
        with suppress(OSError, ServerControlError, TimeoutError):
            _send_model(
                connection,
                ServerControlResponse(
                    request_id=request_id,
                    instance_id=self.instance_id,
                    ok=False,
                    error=ServerControlFailure(code=code, message=message),
                ),
                maximum=SERVER_CONTROL_MAX_RESPONSE_BYTES,
            )

    def _recover_stale_socket(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != self.owner_uid
            or info.st_gid != self.owner_gid
            or stat.S_IMODE(info.st_mode) != SERVER_CONTROL_SOCKET_MODE
        ):
            raise ServerControlUnavailable(
                "unsafe_control_socket",
                "The existing control-socket path is not a safe stale RCP socket.",
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(SERVER_CONTROL_IO_TIMEOUT_SECONDS)
        try:
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise ServerControlUnavailable(
                    "control_socket_unavailable",
                    "The existing control-socket path cannot be recovered safely.",
                ) from exc
        else:
            raise ServerControlUnavailable(
                "control_socket_occupied",
                "Another process already owns the installed RCP control socket.",
            )
        finally:
            probe.close()
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ServerControlUnavailable(
                "control_socket_changed",
                "The existing control-socket path changed during recovery.",
            )
        self.socket_path.unlink()

    def _remove_owned_socket(self) -> None:
        identity = self._socket_identity
        if identity is None:
            return
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            self.socket_path.unlink()


def _validated_socket_path(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("the control socket path must be absolute and normalized")
    if len(os.fsencode(path)) > SERVER_CONTROL_MAX_SOCKET_PATH_BYTES:
        raise ValueError("the control socket path is too long for the supported Unix kernels")
    return path


def _validate_runtime_directory(path: Path, *, owner_uid: int, owner_gid: int) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ServerControlUnavailable(
            "runtime_directory_unavailable",
            "The installed RCP runtime directory is unavailable.",
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != owner_uid
        or info.st_gid != owner_gid
        or stat.S_IMODE(info.st_mode) != SERVER_CONTROL_RUNTIME_MODE
    ):
        raise ServerControlUnavailable(
            "unsafe_runtime_directory",
            "The installed RCP runtime directory has unsafe ownership or mode.",
        )


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ServerControlError("incomplete_frame", "The control frame is incomplete.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_json(connection: socket.socket, *, maximum: int) -> object:
    header = _receive_exact(connection, _FRAME_HEADER.size)
    (size,) = _FRAME_HEADER.unpack(header)
    if size == 0:
        raise ServerControlError("invalid_frame", "The control frame is empty.")
    if size > maximum:
        raise ServerControlError("oversized_frame", "The control frame is too large.")
    body = _receive_exact(connection, size)
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServerControlError("invalid_frame", "The control frame is not valid JSON.") from exc


def _receive_response(connection: socket.socket) -> ServerControlResponse:
    try:
        return ServerControlResponse.model_validate(
            _receive_json(connection, maximum=SERVER_CONTROL_MAX_RESPONSE_BYTES)
        )
    except ServerControlError:
        raise
    except Exception as exc:
        raise ServerControlError(
            "invalid_response",
            "The running RCP process returned an invalid control response.",
        ) from exc


def _send_model(connection: socket.socket, model: BaseModel, *, maximum: int) -> None:
    body = model.model_dump_json().encode("utf-8")
    if not body or len(body) > maximum:
        raise ServerControlError("oversized_frame", "The control frame exceeds its size limit.")
    connection.sendall(_FRAME_HEADER.pack(len(body)) + body)


__all__ = [
    "SERVER_CONTROL_MAX_REQUEST_BYTES",
    "SERVER_CONTROL_MAX_RESPONSE_BYTES",
    "SERVER_CONTROL_PROTOCOL_VERSION",
    "SERVER_CONTROL_RUNTIME_MODE",
    "SERVER_CONTROL_SOCKET_MODE",
    "ServerControlClient",
    "ServerControlError",
    "ServerControlHandler",
    "ServerControlPeer",
    "ServerControlProbeResult",
    "ServerControlRequest",
    "ServerControlServer",
    "ServerControlUnavailable",
    "unix_peer_identity",
]
