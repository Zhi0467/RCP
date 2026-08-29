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
    SERVER_CONTROL_BACKUP_CAPTURE_TIMEOUT_SECONDS,
    SERVER_CONTROL_IO_TIMEOUT_SECONDS,
    SERVER_CONTROL_PROJECT_PROVISION_TIMEOUT_SECONDS,
    SERVER_CONTROL_PROVIDER_CHECK_TIMEOUT_SECONDS,
    SERVER_CONTROL_STOP_TIMEOUT_SECONDS,
)
from rcp.server_ops.models import SERVER_CLI_MAX_STEPS, ServerStep, redact_server_text
from rcp.server_runtime import ServerMetadata, read_server_metadata

SERVER_CONTROL_PROTOCOL_VERSION = 4
SERVER_CONTROL_MAX_REQUEST_BYTES = 64 * 1024
SERVER_CONTROL_MAX_RESPONSE_BYTES = 256 * 1024
SERVER_CONTROL_SOCKET_MODE = 0o600
SERVER_CONTROL_RUNTIME_MODE = 0o700
SERVER_CONTROL_MAX_SOCKET_PATH_BYTES = 99

_FRAME_HEADER = struct.Struct("!I")
_HEX_DIGEST = frozenset("0123456789abcdef")

ServerControlOperation = Literal[
    "probe",
    "provider_readiness_plan",
    "provider_readiness_check",
    "project_provision_plan",
    "project_provision_step",
    "backup_sqlite_capture",
]
SERVER_CONTROL_OPERATIONS: tuple[ServerControlOperation, ...] = (
    "probe",
    "provider_readiness_plan",
    "provider_readiness_check",
    "project_provision_plan",
    "project_provision_step",
    "backup_sqlite_capture",
)
ServerControlProjectStatus = Literal[
    "waiting_for_server_setup",
    "setup_in_progress",
    "operator_action_needed",
    "ready_for_review",
]


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
    operation: ServerControlOperation
    selector_kind: Literal["request", "project"] | None = None
    selector_id: str | None = None
    boundary_sha256: str | None = None
    target_id: str | None = None

    @model_validator(mode="after")
    def validate_ids(self) -> ServerControlRequest:
        _canonical_uuid4(self.request_id, label="control request id")
        _canonical_uuid4(self.instance_id, label="control instance id")
        if self.selector_id is not None:
            _canonical_uuid4(self.selector_id, label="control selector id")
        if self.operation in {"probe", "backup_sqlite_capture"}:
            if any(
                value is not None
                for value in (
                    self.selector_kind,
                    self.selector_id,
                    self.boundary_sha256,
                    self.target_id,
                )
            ):
                raise ValueError("selector-free control operations cannot carry selector fields")
        elif self.operation in {"provider_readiness_plan", "project_provision_plan"}:
            if self.selector_kind is None or self.selector_id is None:
                raise ValueError("control plan requires one selector")
            if self.operation == "project_provision_plan" and self.selector_kind != "request":
                raise ValueError("project provisioning plan requires one request selector")
            if self.boundary_sha256 is not None or self.target_id is not None:
                raise ValueError("control plan cannot carry a step boundary")
        elif any(
            value is None
            for value in (
                self.selector_kind,
                self.selector_id,
                self.boundary_sha256,
                self.target_id,
            )
        ):
            raise ValueError("control step requires its exact plan boundary")
        elif self.operation == "project_provision_step" and self.selector_kind != "request":
            raise ValueError("project provisioning step requires one request selector")
        for value, label in (
            (self.boundary_sha256, "control boundary"),
            (self.target_id, "control target"),
        ):
            if value is not None and (
                len(value) != 64 or any(character not in _HEX_DIGEST for character in value)
            ):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return self


class ServerControlProbeResult(_StrictModel):
    instance_id: str
    pid: int = Field(gt=0)
    data_dir_id: str
    space_id: str
    space_kind: Literal["team"] = "team"
    operations: tuple[ServerControlOperation, ...]

    @model_validator(mode="after")
    def validate_identity(self) -> ServerControlProbeResult:
        _canonical_uuid4(self.instance_id, label="control instance id")
        _canonical_uuid4(self.space_id, label="space id")
        if len(self.data_dir_id) != 64 or any(
            character not in _HEX_DIGEST for character in self.data_dir_id
        ):
            raise ValueError("data directory identity must be a lowercase SHA-256 digest")
        expected_order = tuple(
            operation for operation in SERVER_CONTROL_OPERATIONS if operation in self.operations
        )
        if (
            not self.operations
            or self.operations[0] != "probe"
            or self.operations != expected_order
        ):
            raise ValueError("control probe operations must be unique and in registry order")
        return self


class ServerControlProviderTarget(_StrictModel):
    target_id: str
    step: ServerStep

    @model_validator(mode="after")
    def validate_target(self) -> ServerControlProviderTarget:
        if len(self.target_id) != 64 or any(
            character not in _HEX_DIGEST for character in self.target_id
        ):
            raise ValueError("provider readiness target must be a lowercase SHA-256 digest")
        if self.step.state != "pending":
            raise ValueError("provider readiness plans require pending steps")
        return self


class ServerControlProviderPlanResult(_StrictModel):
    instance_id: str
    pid: int = Field(gt=0)
    data_dir_id: str
    space_id: str
    space_kind: Literal["team"] = "team"
    selector_kind: Literal["request", "project"]
    selector_id: str
    boundary_sha256: str
    targets: tuple[ServerControlProviderTarget, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_plan(self) -> ServerControlProviderPlanResult:
        _validate_provider_result_identity(
            self.instance_id,
            self.space_id,
            self.data_dir_id,
            self.selector_id,
            self.boundary_sha256,
        )
        if [target.step.number for target in self.targets] != list(range(1, len(self.targets) + 1)):
            raise ValueError("provider readiness plan steps must be consecutive")
        ids = [target.target_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("provider readiness plan targets must be unique")
        return self


class ServerControlProviderCheckResult(_StrictModel):
    instance_id: str
    pid: int = Field(gt=0)
    data_dir_id: str
    space_id: str
    space_kind: Literal["team"] = "team"
    selector_kind: Literal["request", "project"]
    selector_id: str
    target_id: str
    boundary_sha256: str
    next_boundary_sha256: str
    step: ServerStep

    @model_validator(mode="after")
    def validate_check(self) -> ServerControlProviderCheckResult:
        _validate_provider_result_identity(
            self.instance_id,
            self.space_id,
            self.data_dir_id,
            self.selector_id,
            self.boundary_sha256,
        )
        for value, label in (
            (self.target_id, "provider readiness target"),
            (self.next_boundary_sha256, "next provider readiness boundary"),
        ):
            if len(value) != 64 or any(character not in _HEX_DIGEST for character in value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        if self.step.state not in {
            "succeeded",
            "failed",
            "operator_action_needed",
            "unavailable",
        }:
            raise ValueError("provider readiness check requires one terminal step")
        return self


class ServerControlProjectTarget(_StrictModel):
    target_id: str
    step: ServerStep

    @model_validator(mode="after")
    def validate_target(self) -> ServerControlProjectTarget:
        if len(self.target_id) != 64 or any(
            character not in _HEX_DIGEST for character in self.target_id
        ):
            raise ValueError("project provisioning target must be a lowercase SHA-256 digest")
        if self.step.state != "pending":
            raise ValueError("project provisioning plans require pending steps")
        return self


class ServerControlProjectPlanResult(_StrictModel):
    instance_id: str
    pid: int = Field(gt=0)
    data_dir_id: str
    space_id: str
    space_kind: Literal["team"] = "team"
    request_id: str
    request_status: ServerControlProjectStatus
    revision: int = Field(ge=0)
    boundary_sha256: str
    targets: tuple[ServerControlProjectTarget, ...] = Field(
        min_length=1,
        max_length=SERVER_CLI_MAX_STEPS,
    )

    @model_validator(mode="after")
    def validate_plan(self) -> ServerControlProjectPlanResult:
        _validate_provider_result_identity(
            self.instance_id,
            self.space_id,
            self.data_dir_id,
            self.request_id,
            self.boundary_sha256,
        )
        if [target.step.number for target in self.targets] != list(range(1, len(self.targets) + 1)):
            raise ValueError("project provisioning plan steps must be consecutive")
        ids = [target.target_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("project provisioning plan targets must be unique")
        return self


class ServerControlProjectStepResult(_StrictModel):
    instance_id: str
    pid: int = Field(gt=0)
    data_dir_id: str
    space_id: str
    space_kind: Literal["team"] = "team"
    request_id: str
    request_status: ServerControlProjectStatus
    revision: int = Field(ge=0)
    target_id: str
    boundary_sha256: str
    next_boundary_sha256: str
    step: ServerStep

    @model_validator(mode="after")
    def validate_step(self) -> ServerControlProjectStepResult:
        _validate_provider_result_identity(
            self.instance_id,
            self.space_id,
            self.data_dir_id,
            self.request_id,
            self.boundary_sha256,
        )
        for value, label in (
            (self.target_id, "project provisioning target"),
            (self.next_boundary_sha256, "next project provisioning boundary"),
        ):
            if len(value) != 64 or any(character not in _HEX_DIGEST for character in value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        if self.step.state not in {
            "succeeded",
            "failed",
            "operator_action_needed",
            "unavailable",
        }:
            raise ValueError("project provisioning step requires one terminal step")
        return self


class ServerControlBackupCaptureResult(_StrictModel):
    instance_id: str
    pid: int = Field(gt=0)
    data_dir_id: str
    space_id: str
    space_kind: Literal["team"] = "team"
    capture_id: str
    receipt_path: str
    receipt_sha256: str
    snapshot_sha256: str
    status: Literal["complete", "partial"]
    project_count: int = Field(ge=0)
    uncaptured_project_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_capture(self) -> ServerControlBackupCaptureResult:
        for value, label in (
            (self.instance_id, "control instance id"),
            (self.space_id, "space id"),
            (self.capture_id, "backup capture id"),
        ):
            _canonical_uuid4(value, label=label)
        for value, label in (
            (self.data_dir_id, "data directory identity"),
            (self.receipt_sha256, "backup receipt digest"),
            (self.snapshot_sha256, "SQLite snapshot digest"),
        ):
            if len(value) != 64 or any(character not in _HEX_DIGEST for character in value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        path = Path(self.receipt_path)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path.name != "sqlite-capture.json"
            or path.parent.name != f"backup-{self.capture_id}"
        ):
            raise ValueError("backup receipt path is not bound to its capture identity")
        if self.uncaptured_project_count > self.project_count:
            raise ValueError("uncaptured project count exceeds the captured project inventory")
        if self.status == "complete" and self.uncaptured_project_count:
            raise ValueError("a complete backup capture cannot report uncaptured projects")
        return self


def _validate_provider_result_identity(
    instance_id: str,
    space_id: str,
    data_dir_id: str,
    selector_id: str,
    boundary_sha256: str,
) -> None:
    _canonical_uuid4(instance_id, label="control instance id")
    _canonical_uuid4(space_id, label="space id")
    _canonical_uuid4(selector_id, label="control selector id")
    for value, label in (
        (data_dir_id, "data directory identity"),
        (boundary_sha256, "control boundary"),
    ):
        if len(value) != 64 or any(character not in _HEX_DIGEST for character in value):
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")


class ServerControlFailure(_StrictModel):
    code: Literal[
        "invalid_request",
        "oversized_request",
        "operation_failed",
        "operation_refused",
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
    result: (
        ServerControlProbeResult
        | ServerControlProviderPlanResult
        | ServerControlProviderCheckResult
        | ServerControlProjectPlanResult
        | ServerControlProjectStepResult
        | ServerControlBackupCaptureResult
        | None
    ) = None
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
    ServerControlProbeResult
    | ServerControlProviderPlanResult
    | ServerControlProviderCheckResult
    | ServerControlProjectPlanResult
    | ServerControlProjectStepResult
    | ServerControlBackupCaptureResult,
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
        result = self._exchange(request)
        if not isinstance(result, ServerControlProbeResult):
            raise ServerControlError(
                "invalid_response",
                "The running RCP process returned the wrong control result.",
            )
        return result

    def provider_readiness_plan(
        self,
        *,
        selector_kind: Literal["request", "project"],
        selector_id: str,
    ) -> ServerControlProviderPlanResult:
        request = ServerControlRequest(
            request_id=str(uuid.uuid4()),
            instance_id=self.metadata.instance_id,
            operation="provider_readiness_plan",
            selector_kind=selector_kind,
            selector_id=selector_id,
        )
        result = self._exchange(request)
        if not isinstance(result, ServerControlProviderPlanResult):
            raise ServerControlError(
                "invalid_response",
                "The running RCP process returned the wrong provider plan.",
            )
        return result

    def check_provider_readiness(
        self,
        *,
        selector_kind: Literal["request", "project"],
        selector_id: str,
        boundary_sha256: str,
        target_id: str,
    ) -> ServerControlProviderCheckResult:
        request = ServerControlRequest(
            request_id=str(uuid.uuid4()),
            instance_id=self.metadata.instance_id,
            operation="provider_readiness_check",
            selector_kind=selector_kind,
            selector_id=selector_id,
            boundary_sha256=boundary_sha256,
            target_id=target_id,
        )
        result = self._exchange(request)
        if not isinstance(result, ServerControlProviderCheckResult):
            raise ServerControlError(
                "invalid_response",
                "The running RCP process returned the wrong provider check.",
            )
        return result

    def project_provision_plan(
        self,
        *,
        request_id: str,
    ) -> ServerControlProjectPlanResult:
        request = ServerControlRequest(
            request_id=str(uuid.uuid4()),
            instance_id=self.metadata.instance_id,
            operation="project_provision_plan",
            selector_kind="request",
            selector_id=request_id,
        )
        result = self._exchange(request)
        if not isinstance(result, ServerControlProjectPlanResult):
            raise ServerControlError(
                "invalid_response",
                "The running RCP process returned the wrong project provisioning plan.",
            )
        return result

    def advance_project_provision(
        self,
        *,
        request_id: str,
        boundary_sha256: str,
        target_id: str,
    ) -> ServerControlProjectStepResult:
        request = ServerControlRequest(
            request_id=str(uuid.uuid4()),
            instance_id=self.metadata.instance_id,
            operation="project_provision_step",
            selector_kind="request",
            selector_id=request_id,
            boundary_sha256=boundary_sha256,
            target_id=target_id,
        )
        result = self._exchange(request)
        if not isinstance(result, ServerControlProjectStepResult):
            raise ServerControlError(
                "invalid_response",
                "The running RCP process returned the wrong project provisioning step.",
            )
        return result

    def capture_backup_sqlite(self) -> ServerControlBackupCaptureResult:
        request = ServerControlRequest(
            request_id=str(uuid.uuid4()),
            instance_id=self.metadata.instance_id,
            operation="backup_sqlite_capture",
        )
        result = self._exchange(request)
        if not isinstance(result, ServerControlBackupCaptureResult):
            raise ServerControlError(
                "invalid_response",
                "The running RCP process returned the wrong backup capture result.",
            )
        return result

    def _exchange(
        self,
        request: ServerControlRequest,
    ) -> (
        ServerControlProbeResult
        | ServerControlProviderPlanResult
        | ServerControlProviderCheckResult
        | ServerControlProjectPlanResult
        | ServerControlProjectStepResult
        | ServerControlBackupCaptureResult
    ):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        timeout = SERVER_CONTROL_IO_TIMEOUT_SECONDS
        if request.operation == "backup_sqlite_capture":
            timeout = SERVER_CONTROL_BACKUP_CAPTURE_TIMEOUT_SECONDS
        elif request.operation == "provider_readiness_check":
            timeout = SERVER_CONTROL_PROVIDER_CHECK_TIMEOUT_SECONDS
        elif request.operation == "project_provision_step":
            timeout = SERVER_CONTROL_PROJECT_PROVISION_TIMEOUT_SECONDS
        connection.settimeout(timeout)
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
        try:
            result = _validated_control_result(request, response.result)
        except ValueError as exc:
            raise ServerControlError(
                "invalid_response",
                "The running RCP process returned a mismatched control result.",
            ) from exc
        if result.pid != peer.pid:
            raise ServerControlError(
                "wrong_server_identity",
                "The control response does not match the kernel-authenticated server process.",
            )
        return result


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
                result = _validated_control_result(request, self.handler(request, peer))
                if result.instance_id != self.instance_id:
                    raise ValueError("control handler returned a different process instance")
            except ServerControlError as exc:
                if exc.code != "operation_refused":
                    self._send_error(
                        connection,
                        request_id=request_id,
                        code="operation_failed",
                        message=(
                            "The named control operation failed inside the running RCP process."
                        ),
                    )
                    return
                self._send_error(
                    connection,
                    request_id=request_id,
                    code="operation_refused",
                    message=_safe_operation_refusal(str(exc)),
                )
                return
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
            "operation_refused",
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


def _validated_control_result(
    request: ServerControlRequest,
    result: ServerControlProbeResult
    | ServerControlProviderPlanResult
    | ServerControlProviderCheckResult
    | ServerControlProjectPlanResult
    | ServerControlProjectStepResult
    | ServerControlBackupCaptureResult,
) -> (
    ServerControlProbeResult
    | ServerControlProviderPlanResult
    | ServerControlProviderCheckResult
    | ServerControlProjectPlanResult
    | ServerControlProjectStepResult
    | ServerControlBackupCaptureResult
):
    if request.operation == "probe":
        if not isinstance(result, ServerControlProbeResult):
            raise ValueError("control probe returned another operation's result")
        return ServerControlProbeResult.model_validate(result)
    if request.operation == "provider_readiness_plan":
        if not isinstance(result, ServerControlProviderPlanResult):
            raise ValueError("provider readiness plan returned another operation's result")
        validated = ServerControlProviderPlanResult.model_validate(result)
        if (
            validated.selector_kind != request.selector_kind
            or validated.selector_id != request.selector_id
        ):
            raise ValueError("provider readiness plan returned another selector")
        return validated
    if request.operation == "provider_readiness_check":
        if not isinstance(result, ServerControlProviderCheckResult):
            raise ValueError("provider readiness check returned another operation's result")
        validated_provider = ServerControlProviderCheckResult.model_validate(result)
        if (
            validated_provider.selector_kind != request.selector_kind
            or validated_provider.selector_id != request.selector_id
            or validated_provider.boundary_sha256 != request.boundary_sha256
            or validated_provider.target_id != request.target_id
        ):
            raise ValueError("provider readiness check returned another planned target")
        return validated_provider
    if request.operation == "project_provision_plan":
        if not isinstance(result, ServerControlProjectPlanResult):
            raise ValueError("project provisioning plan returned another operation's result")
        validated_plan = ServerControlProjectPlanResult.model_validate(result)
        if validated_plan.request_id != request.selector_id:
            raise ValueError("project provisioning plan returned another request")
        return validated_plan
    if request.operation == "backup_sqlite_capture":
        if not isinstance(result, ServerControlBackupCaptureResult):
            raise ValueError("backup SQLite capture returned another operation's result")
        return ServerControlBackupCaptureResult.model_validate(result)
    if not isinstance(result, ServerControlProjectStepResult):
        raise ValueError("project provisioning step returned another operation's result")
    validated_step = ServerControlProjectStepResult.model_validate(result)
    if (
        validated_step.request_id != request.selector_id
        or validated_step.boundary_sha256 != request.boundary_sha256
        or validated_step.target_id != request.target_id
    ):
        raise ValueError("project provisioning step returned another planned target")
    return validated_step


def _safe_operation_refusal(message: str) -> str:
    safe = redact_server_text(message.strip())
    if (
        not safe
        or len(safe) > 240
        or any(ord(character) < 32 or ord(character) == 127 for character in safe)
    ):
        return "The named control operation was refused at its durable boundary."
    return safe


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
    body = _receive_body(connection, maximum=maximum)
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServerControlError("invalid_frame", "The control frame is not valid JSON.") from exc


def _receive_body(connection: socket.socket, *, maximum: int) -> bytes:
    header = _receive_exact(connection, _FRAME_HEADER.size)
    (size,) = _FRAME_HEADER.unpack(header)
    if size == 0:
        raise ServerControlError("invalid_frame", "The control frame is empty.")
    if size > maximum:
        raise ServerControlError("oversized_frame", "The control frame is too large.")
    return _receive_exact(connection, size)


def _receive_response(connection: socket.socket) -> ServerControlResponse:
    try:
        return ServerControlResponse.model_validate_json(
            _receive_body(connection, maximum=SERVER_CONTROL_MAX_RESPONSE_BYTES)
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
    "SERVER_CONTROL_OPERATIONS",
    "SERVER_CONTROL_MAX_REQUEST_BYTES",
    "SERVER_CONTROL_MAX_RESPONSE_BYTES",
    "SERVER_CONTROL_PROTOCOL_VERSION",
    "SERVER_CONTROL_RUNTIME_MODE",
    "SERVER_CONTROL_SOCKET_MODE",
    "ServerControlClient",
    "ServerControlBackupCaptureResult",
    "ServerControlError",
    "ServerControlHandler",
    "ServerControlPeer",
    "ServerControlProbeResult",
    "ServerControlProjectPlanResult",
    "ServerControlProjectStepResult",
    "ServerControlProjectTarget",
    "ServerControlProviderCheckResult",
    "ServerControlProviderPlanResult",
    "ServerControlProviderTarget",
    "ServerControlRequest",
    "ServerControlServer",
    "ServerControlUnavailable",
    "unix_peer_identity",
]
