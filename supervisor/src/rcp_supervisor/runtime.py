"""Bounded Linux adapters for the independent deployment coordinator."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.launch import read_selected_receipt, validate_selected_receipt
from rcp_supervisor.limits import (
    APP_COMMAND_TIMEOUT_SECONDS,
    CONTROL_MAX_RESPONSE_BYTES,
    INSTALL_TIMEOUT_SECONDS,
    MAINTENANCE_TIMEOUT_SECONDS,
    MAX_APP_OUTPUT_BYTES,
    PROBE_TIMEOUT_SECONDS,
    SERVICE_TIMEOUT_SECONDS,
)
from rcp_supervisor.root_tools import root_executable


@dataclass(frozen=True)
class Paths:
    config: Path = Path("/etc/rcp/server.toml")
    supervisor: Path = Path("/etc/rcp/supervisor")
    current: Path = Path("/etc/rcp/current")
    data_dir: Path = Path("/home/rcp/rcp-server/data")
    releases_root: Path = Path("/home/rcp/rcp-server/releases")
    checkpoints_root: Path = Path("/home/rcp/rcp-server/update-checkpoints")
    control_socket: Path = Path("/run/rcp/control.sock")
    service_home: Path = Path("/home/rcp")
    service_account: str = "rcp"
    service_unit: str = "rcp.service"
    port: int = 8421

    @property
    def selected(self) -> Path:
        return self.supervisor / "selected.json"

    @property
    def operations(self) -> Path:
        return self.supervisor / "operations"


DEFAULT_PATHS = Paths()


def selected_release(paths: Paths) -> dict:
    selected = read_selected_receipt(paths.selected, releases_root=paths.releases_root)
    info = paths.current.lstat()
    if (
        not stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or os.readlink(paths.current) != selected["release_directory"]
    ):
        raise SupervisorError("The current pointer and root-selected release disagree.")
    return selected


def _read_file(path: Path, *, uid: int, max_bytes: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > max_bytes
    ):
        raise SupervisorError(
            "An installed metadata file has unsafe ownership, permissions, or size."
        )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if os.fstat(source.fileno()) != info:
            raise SupervisorError("Installed metadata changed while opening it.")
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SupervisorError("Installed metadata exceeds its size limit.")
    return data


def read_config(paths: Paths, *, allow_legacy: bool = False) -> dict:
    try:
        config = tomllib.loads(_read_file(paths.config, uid=0, max_bytes=65536).decode())
    except (OSError, UnicodeError, ValueError) as exc:
        raise SupervisorError(
            "Installed supervisor configuration is unavailable or invalid."
        ) from exc
    if (
        config.get("schema_version") not in ({1, 2, 3} if allow_legacy else {3})
        or config.get("service_account") != paths.service_account
        or config.get("service_unit") != paths.service_unit
    ):
        raise SupervisorError(
            "This installation requires an explicit source-to-supervisor migration."
        )
    if any(
        config.get("paths", {}).get(name) != str(getattr(paths, attribute))
        for name, attribute in (
            ("data_dir", "data_dir"),
            ("releases_root", "releases_root"),
            ("update_checkpoints_root", "checkpoints_root"),
            ("current_release", "current"),
            ("control_socket", "control_socket"),
        )
    ):
        raise SupervisorError("Configured deployment paths differ from the installed layout.")
    if config["schema_version"] != 3:
        return config
    release = config.get("release")
    if (
        not isinstance(release, dict)
        or release.get("followed") != "stable"
        or not release.keys() <= {"followed", "pin"}
    ):
        raise SupervisorError("Installed release selection is unsupported.")
    return config


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_root_json(path: Path, document: dict) -> None:
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_mode & 0o022
        or path.parent.is_symlink()
    ):
        raise SupervisorError("Root release metadata parent is unsafe.")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            )
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class SystemRuntime:
    def __init__(
        self,
        paths: Paths = DEFAULT_PATHS,
        *,
        notify: Callable[[str], None] | None = None,
        startup: bool = False,
        restore_request: dict | None = None,
        allow_legacy_config: bool = False,
    ) -> None:
        if os.geteuid() != 0:
            raise SupervisorError("Deployment coordination must run as root.")
        self.paths = paths
        self.notify = notify or (lambda _message: None)
        self.startup = startup
        self.restore_request = restore_request
        self._deployment_lock_fd: int | None = None
        account = pwd.getpwnam(paths.service_account)
        self.uid, self.gid = account.pw_uid, account.pw_gid
        self.config = read_config(paths, allow_legacy=allow_legacy_config)

    @contextmanager
    def deployment_lock(self):
        """Exclude the complete external backup job during byte publication."""
        path = self.paths.data_dir.parent / ".backup-run.lock"
        for parent in path.parents:
            if parent.is_symlink():
                raise SupervisorError("Backup lock cannot traverse symbolic links.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.uid
                or info.st_gid != self.gid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise SupervisorError("Backup lock is not the private service-owned file.")
            deadline = time.monotonic() + MAINTENANCE_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SupervisorError(
                            "Timed out waiting for the running protected backup."
                        ) from None
                    time.sleep(0.1)
            if path.stat(follow_symlinks=False) != info:
                raise SupervisorError("Backup lock changed while acquiring deployment ownership.")
            self._deployment_lock_fd = descriptor
            yield
        finally:
            self._deployment_lock_fd = None
            os.close(descriptor)

    def environment(self, release: dict | None = None) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("PYTHON", "UV_", "PIP_", "RCP_"))
            and name
            not in {"VIRTUAL_ENV", "CONDA_PREFIX", "NOTIFY_SOCKET", "LISTEN_FDS", "LISTEN_PID"}
        }
        environment.update(
            HOME=str(self.paths.service_home),
            USER=self.paths.service_account,
            LOGNAME=self.paths.service_account,
            PATH=f"{self.paths.service_home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            RCP_DATA_DIR=str(self.paths.data_dir),
        )
        if release is not None:
            environment.update(
                RCP_DEPLOYED_COMMIT=release["commit"],
                RCP_DEPLOYED_VERSION=release["version_string"],
            )
        return environment

    def _stop_child(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=SERVICE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=SERVICE_TIMEOUT_SECONDS)

    @contextmanager
    def _log(self, label: str):
        directory = self.paths.supervisor / "logs"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise SupervisorError("Supervisor diagnostic storage is unsafe.")
        descriptor, name = tempfile.mkstemp(prefix=f"{label}-", suffix=".log", dir=directory)
        with os.fdopen(descriptor, "w+b") as output:
            try:
                yield output, name
            finally:
                output.flush()
                os.fsync(output.fileno())

    def owned_argv(self, argv: list[str]) -> list[str]:
        """Arm Linux parent-death ownership after the subprocess drops credentials."""
        return [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("child_exec.py")),
            str(os.getpid()),
            *argv,
        ]

    def _service_output(
        self,
        argv: list[str],
        request: dict | None = None,
        *,
        timeout: float,
        release: dict | None = None,
    ) -> bytes:
        payload = b"" if request is None else json.dumps(request, allow_nan=False).encode()
        if len(payload) > MAX_APP_OUTPUT_BYTES:
            raise SupervisorError("Application subprocess request exceeds its bound.")
        with self._log("output") as (output, _), self._log("error") as (error, error_path):
            process = subprocess.Popen(
                self.owned_argv(argv),
                pass_fds=(() if self._deployment_lock_fd is None else (self._deployment_lock_fd,)),
                stdin=subprocess.PIPE if request is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=error,
                user=self.uid,
                group=self.gid,
                extra_groups=[],
                cwd=self.paths.service_home,
                env=self.environment(release),
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout
                with selectors.DefaultSelector() as selector:
                    if process.stdin is not None:
                        os.set_blocking(process.stdin.fileno(), False)
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                    offset = 0
                    while process.poll() is None:
                        if (
                            sum(os.fstat(log.fileno()).st_size for log in (output, error))
                            > MAX_APP_OUTPUT_BYTES
                        ):
                            raise SupervisorError(
                                f"Application output exceeded its bound; inspect {error_path}."
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise SupervisorError(
                                f"Application subprocess exceeded its time limit; inspect {error_path}."
                            )
                        for key, _ in selector.select(min(0.1, remaining)):
                            try:
                                offset += os.write(key.fd, payload[offset : offset + 65536])
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                offset = len(payload)
                            if offset == len(payload):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                if process.returncode:
                    raise SupervisorError(
                        f"Application subprocess refused the deployment operation; inspect {error_path}."
                    )
                if (
                    sum(os.fstat(log.fileno()).st_size for log in (output, error))
                    > MAX_APP_OUTPUT_BYTES
                ):
                    raise SupervisorError(
                        f"Application output exceeded its bound; inspect {error_path}."
                    )
                output.seek(0)
                data = output.read(MAX_APP_OUTPUT_BYTES + 1)
                if len(data) > MAX_APP_OUTPUT_BYTES:
                    raise SupervisorError("Application subprocess output exceeded its bound.")
                return data
            finally:
                self._stop_child(process)
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()

    def service_json(
        self,
        argv: list[str],
        request: dict | None = None,
        *,
        timeout: float = APP_COMMAND_TIMEOUT_SECONDS,
        release: dict | None = None,
    ) -> dict:
        output = self._service_output(argv, request, timeout=timeout, release=release)
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError) as exc:
            raise SupervisorError("Application subprocess did not return one JSON result.") from exc
        if not isinstance(result, dict):
            raise SupervisorError("Application subprocess response is not an object.")
        return result

    @staticmethod
    def python(release: dict) -> str:
        return str(Path(release["release_directory"]) / ".venv/bin/python")

    def filesystem(self, action: str, request: dict) -> dict:
        return self.service_json(
            [sys.executable, "-I", "-m", "rcp_supervisor.fs_worker", action],
            request,
            timeout=INSTALL_TIMEOUT_SECONDS if action == "install" else APP_COMMAND_TIMEOUT_SECONDS,
        )

    def application(self, release: dict, action: str, request: dict | None = None) -> dict:
        return self.service_json(
            [
                self.python(release),
                "-I",
                "-m",
                "rcp.server_ops.deployment",
                action,
                *(["-"] if request is not None else []),
            ],
            request,
            release=release,
        )

    def require_capability(self, release: dict) -> None:
        capability = self.application(release, "capabilities")
        if (
            capability.get("version") != 1
            or capability.get("maintenance_protocol") != 10
            or not {"prepare", "validate"} <= set(capability.get("commands", []))
        ):
            raise SupervisorError(
                "The release lacks the required application maintenance contract."
            )

    def metadata(self) -> dict:
        try:
            metadata = json.loads(
                _read_file(self.paths.data_dir / "rcp-server.json", uid=self.uid, max_bytes=16384)
            )
        except (ValueError, OSError, UnicodeError) as exc:
            raise SupervisorError("Running server metadata is unavailable.") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("control_socket") != str(self.paths.control_socket)
            or type(metadata.get("pid")) is not int
            or metadata["pid"] <= 0
            or metadata.get("data_dir_id")
            != hashlib.sha256(str(self.paths.data_dir.resolve()).encode()).hexdigest()
        ):
            raise SupervisorError("Running server metadata names an unsupported control endpoint.")
        return metadata

    def control(
        self,
        operation: str,
        *,
        record: dict | None = None,
        proof: dict | None = None,
        timeout: float = 5,
    ) -> dict:
        metadata = self.metadata()
        request = {
            "protocol_version": 10,
            "request_id": str(uuid.uuid4()),
            "instance_id": metadata["instance_id"],
            "operation": operation,
        }
        if record is not None:
            request.update(selector_id=record["operation_id"], boundary_sha256=record["nonce"])
        if proof is not None:
            request.update(proof_path=proof["path"], proof_sha256=proof["sha256"])
        data = json.dumps(request).encode()
        if len(data) > 65536:
            raise SupervisorError("Private maintenance request exceeds its limit.")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.paths.control_socket))
            peer_pid, peer_uid, _gid = struct.unpack(
                "3i",
                client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
            )
            if peer_uid != self.uid or peer_pid != metadata["pid"]:
                raise SupervisorError(
                    "Private maintenance socket is not owned by the service account."
                )
            client.sendall(struct.pack("!I", len(data)) + data)

            def receive(count: int) -> bytes:
                chunks = bytearray()
                while len(chunks) < count:
                    chunk = client.recv(count - len(chunks))
                    if not chunk:
                        raise SupervisorError("Private maintenance response ended early.")
                    chunks.extend(chunk)
                return bytes(chunks)

            (length,) = struct.unpack("!I", receive(4))
            if not 1 <= length <= CONTROL_MAX_RESPONSE_BYTES:
                raise SupervisorError("Private maintenance response exceeds its limit.")
            result = json.loads(receive(length))
        if (
            not isinstance(result, dict)
            or result.get("request_id") != request["request_id"]
            or result.get("instance_id") != metadata["instance_id"]
            or result.get("protocol_version") != 10
            or result.get("ok") is not True
            or not isinstance(result.get("result"), dict)
            or result["result"].get("pid") != peer_pid
            or result["result"].get("instance_id") != metadata["instance_id"]
            or result["result"].get("data_dir_id") != metadata["data_dir_id"]
        ):
            raise SupervisorError("The application refused its private maintenance boundary.")
        return result["result"]

    def protected_backup(self, release: dict | None = None) -> None:
        current = release or read_selected_receipt(
            self.paths.selected, releases_root=self.paths.releases_root
        )
        output = self._service_output(
            [
                self.python(current),
                "-I",
                "-m",
                "rcp",
                "server",
                "backup",
                "run",
                "--machine-readable",
            ],
            timeout=APP_COMMAND_TIMEOUT_SECONDS,
            release=current,
        )
        try:
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            fields = {
                field["name"]: field["value"]
                for event in events
                for field in event.get("step", {}).get("fields", [])
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise SupervisorError("Protected backup did not return a readable receipt.") from exc
        if fields.get("backup_status") != "protected" or fields.get("uncaptured_projects") != 0:
            raise SupervisorError(
                "A complete verified protected backup is required before deployment."
            )

    def enter_maintenance(self, operation: dict) -> dict:
        result = self.control(
            "maintenance_enter", record=operation, timeout=MAINTENANCE_TIMEOUT_SECONDS
        )
        if (
            result.get("closed") is not True
            or result.get("quiescent") is not True
            or result.get("maintenance_id") != operation["operation_id"]
            or result.get("boundary_sha256") != operation["nonce"]
            or not isinstance(result.get("capture"), dict)
        ):
            raise SupervisorError("The application did not prove a quiescent capture boundary.")
        return result["capture"]

    def abort_maintenance(self, operation: dict) -> None:
        status = self.control("maintenance_status", record=operation)
        if status.get("closed") is False:
            return
        result = self.control(
            "maintenance_release", record=operation, timeout=APP_COMMAND_TIMEOUT_SECONDS
        )
        if result.get("closed") is not False:
            raise SupervisorError(
                "The application could not release its aborted maintenance boundary."
            )

    def _systemctl(self, action: str) -> None:
        subprocess.run(
            [root_executable("systemctl"), action, self.paths.service_unit],
            stdin=subprocess.DEVNULL,
            check=True,
            timeout=SERVICE_TIMEOUT_SECONDS,
        )

    def stop_service(self) -> None:
        if self.startup:
            raise SupervisorError("Startup recovery cannot recursively stop its own systemd unit.")
        self._systemctl("stop")
        result = subprocess.run(
            [
                root_executable("systemctl"),
                "show",
                "--property=MainPID",
                "--value",
                self.paths.service_unit,
            ],
            capture_output=True,
            check=True,
            timeout=SERVICE_TIMEOUT_SECONDS,
        )
        if result.stdout.strip() != b"0":
            raise SupervisorError("Systemd did not prove the application process stopped.")

    def start_service(self) -> None:
        if self.startup:
            raise SupervisorError("Startup recovery cannot recursively start its own systemd unit.")
        self._systemctl("start")
        selected = read_selected_receipt(
            self.paths.selected, releases_root=self.paths.releases_root
        )
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                metadata = self.metadata()
                if (
                    metadata.get("running_commit") == selected["commit"]
                    and metadata.get("app_version") == selected["version_string"]
                ):
                    self.control("probe")
                    return
            except (OSError, SupervisorError):
                pass
            time.sleep(0.1)
        raise SupervisorError("The selected application did not start with its verified identity.")

    def prepare(self, operation: dict, capture: dict) -> tuple[dict, dict, dict, dict | None]:
        workspace = self.paths.checkpoints_root / operation["operation_id"]
        prepared = self.application(
            operation["previous"],
            "prepare",
            {
                "version": 1,
                "data_dir": str(self.paths.data_dir),
                "output_dir": str(workspace / "prepared"),
                "sqlite_receipt_path": capture["receipt_path"],
                "sqlite_receipt_sha256": capture["receipt_sha256"],
            },
        )
        checked = self.application(
            operation["target"],
            "validate",
            {
                "version": 1,
                "proof_path": prepared["proof_path"],
                "proof_sha256": prepared["proof_sha256"],
                "output_dir": str(workspace / "validated"),
            },
        )
        candidate_checkpoint = None
        extra_previous_roots = []
        target_proof = {"path": checked["proof_path"], "sha256": checked["proof_sha256"]}
        if operation["kind"] == "restore":
            from rcp_supervisor.restore import prepare_restore

            candidate_checkpoint, target_proof, extra_previous_roots = prepare_restore(
                self, self.restore_request, operation, prepared
            )
        checkpoint = self.filesystem(
            "checkpoint",
            {
                "directory": str(workspace / "checkpoint"),
                "roots": [*prepared["roots"], *extra_previous_roots],
                "boundary_sha256": prepared["boundary_sha256"],
            },
        )
        return (
            checkpoint,
            {"path": prepared["proof_path"], "sha256": prepared["proof_sha256"]},
            target_proof,
            candidate_checkpoint,
        )

    def prepare_fresh_restore(self, operation: dict) -> tuple[dict, None, dict, dict]:
        from rcp_supervisor.restore import prepare_restore

        inspection = self.application(
            operation["previous"], "inspect", {"version": 1, "data_dir": str(self.paths.data_dir)}
        )
        if inspection.get("status") != "uninitialized":
            raise SupervisorError(
                "Fresh restore cannot replace initialized application data without its maintenance checkpoint."
            )
        workspace = self.paths.checkpoints_root / operation["operation_id"]
        self.filesystem("workspace", {"directory": str(workspace)})
        empty = workspace / "empty"
        self.filesystem("workspace", {"directory": str(empty)})
        prepared = {"roots": [{"live": str(self.paths.data_dir), "payload": str(empty)}]}
        candidate, proof, extra_previous_roots = prepare_restore(
            self, self.restore_request, operation, prepared
        )
        checkpoint = self.filesystem(
            "checkpoint",
            {
                "directory": str(workspace / "checkpoint"),
                "roots": [*prepared["roots"], *extra_previous_roots],
                "boundary_sha256": operation["nonce"],
            },
        )
        return checkpoint, None, proof, candidate

    def restore_roots(self, checkpoint: dict) -> None:
        self.filesystem("restore", checkpoint)

    def switch_pointer(self, release: dict, *, allowed: tuple[dict, ...]) -> None:
        validate_selected_receipt(release, releases_root=self.paths.releases_root)
        info = self.paths.current.lstat()
        if (
            not stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or os.readlink(self.paths.current)
            not in {item["release_directory"] for item in allowed}
        ):
            raise SupervisorError(
                "The installed release pointer differs from this authorized operation."
            )
        temporary = self.paths.current.with_name(f".current-{uuid.uuid4().hex}")
        try:
            temporary.symlink_to(release["release_directory"])
            os.replace(temporary, self.paths.current)
            _sync(self.paths.current.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def selected_release(self) -> dict:
        return selected_release(self.paths)

    def select(self, release: dict) -> None:
        validate_selected_receipt(release, releases_root=self.paths.releases_root)
        write_root_json(self.paths.selected, release)

    def prepare_control_directory(self) -> None:
        """Restore systemd's runtime directory for a probe outside the unit."""
        directory = self.paths.control_socket.parent
        if not directory.is_absolute() or ".." in directory.parts or directory == Path("/"):
            raise SupervisorError("The control runtime directory must be a fixed absolute path.")
        for ancestor in reversed(directory.parents):
            info = ancestor.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise SupervisorError(
                    "The control runtime directory requires protected root ancestry."
                )
        parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(parent_fd)
            if parent.st_uid != 0 or parent.st_mode & 0o022:
                raise SupervisorError("The control runtime parent is not protected by root.")
            created = False
            try:
                os.mkdir(directory.name, mode=0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            descriptor = os.open(
                directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            try:
                if created:
                    os.fchown(descriptor, self.uid, self.gid)
                    os.fchmod(descriptor, 0o700)
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or (info.st_uid, info.st_gid) != (self.uid, self.gid)
                    or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise SupervisorError(
                        "The existing control runtime directory has unsafe metadata."
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def probe(self, release: dict, operation: dict, proof: dict | None) -> None:
        self.require_capability(release)
        self.prepare_control_directory()
        with self._log("probe") as (log, _):
            process = subprocess.Popen(
                self.owned_argv(
                    [
                        self.python(release),
                        "-I",
                        "-m",
                        "rcp",
                        "serve",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(self.paths.port),
                        "--web-assets",
                        "prebuilt",
                        "--maintenance-id",
                        operation["operation_id"],
                        "--maintenance-boundary",
                        operation["nonce"],
                    ]
                ),
                pass_fds=(() if self._deployment_lock_fd is None else (self._deployment_lock_fd,)),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                user=self.uid,
                group=self.gid,
                extra_groups=[],
                cwd=self.paths.service_home,
                env=self.environment(release),
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
                while True:
                    if process.poll() is not None:
                        raise SupervisorError("The fenced application exited before verification.")
                    if (
                        time.monotonic() >= deadline
                        or os.fstat(log.fileno()).st_size > MAX_APP_OUTPUT_BYTES
                    ):
                        raise SupervisorError(
                            "The fenced application did not become ready within its bounds."
                        )
                    try:
                        metadata = self.metadata()
                        if (
                            metadata.get("pid") != process.pid
                            or metadata.get("running_commit") != release["commit"]
                            or metadata.get("app_version") != release["version_string"]
                        ):
                            raise SupervisorError(
                                "The fenced application identity does not match its selected release."
                            )
                        status = self.control("maintenance_status", record=operation)
                        if (
                            status.get("closed") is not True
                            or status.get("quiescent") is not True
                            or status.get("maintenance_id") != operation["operation_id"]
                            or status.get("boundary_sha256") != operation["nonce"]
                        ):
                            raise SupervisorError(
                                "The fenced application did not preserve closed admission."
                            )
                        break
                    except (OSError, SupervisorError):
                        time.sleep(0.1)
                if proof is not None:
                    verified = self.control(
                        "maintenance_verify",
                        record=operation,
                        proof=proof,
                        timeout=APP_COMMAND_TIMEOUT_SECONDS,
                    )
                    if (
                        not verified.get("verification_sha256")
                        or verified.get("closed") is not True
                    ):
                        raise SupervisorError("The application failed its final live-state proof.")
            finally:
                self._stop_child(process)
