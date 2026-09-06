"""External deployment decisions and crash recovery, independent of RCP models."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Protocol

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.launch import validate_selected_receipt
from rcp_supervisor.limits import MAX_OPERATION_BYTES

TERMINAL = frozenset({"committed", "rolled_back", "aborted"})
EARLY = frozenset(
    {"preparing", "backup_ready", "entering_maintenance", "quiescent", "checkpoint_ready"}
)
ROLLBACK = (
    "rollback_started",
    "rollback_roots_complete",
    "previous_pointer_restored",
    "previous_verified",
)
PHASES = (
    TERMINAL
    | EARLY
    | frozenset(ROLLBACK)
    | {
        "activating",
        "pointer_switched",
        "candidate_verified",
        "candidate_chosen",
        "previous_chosen",
    }
)
_FIELDS = {
    "version",
    "operation_id",
    "kind",
    "phase",
    "previous",
    "target",
    "nonce",
    "checkpoint",
    "candidate_checkpoint",
    "previous_uninitialized",
    "previous_proof",
    "target_proof",
    "error",
}


class OperationBusy(SupervisorError):
    """A live coordinator owns the OS operation lock."""


class Runtime(Protocol):
    """Concrete bounded service/byte operations; all release decisions stay here."""

    def selected_release(self) -> dict: ...
    def protected_backup(self) -> None: ...
    def deployment_lock(self) -> AbstractContextManager: ...
    def enter_maintenance(self, operation: dict) -> dict: ...
    def abort_maintenance(self, operation: dict) -> None: ...
    def stop_service(self) -> None: ...
    def prepare(self, operation: dict, capture: dict) -> tuple[dict, dict, dict, dict | None]: ...
    def prepare_fresh_restore(self, operation: dict) -> tuple[dict, None, dict, dict]: ...
    def restore_roots(self, checkpoint: dict) -> None: ...
    def switch_pointer(self, release: dict, *, allowed: tuple[dict, ...]) -> None: ...
    def probe(self, release: dict, operation: dict, proof: dict | None) -> None: ...
    def select(self, release: dict) -> None: ...
    def start_service(self) -> None: ...


def _sha(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _private_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise SupervisorError("Operation storage path is not normalized and absolute.")
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise SupervisorError("Operation storage cannot traverse symbolic links.")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SupervisorError(
            "Operation storage must be a private directory owned by the coordinator."
        )


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path: Path) -> dict:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > MAX_OPERATION_BYTES
    ):
        raise SupervisorError("Deployment journal is not one bounded private file.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if os.fstat(source.fileno()) != info:
            raise SupervisorError("Deployment journal changed while opening it.")
        payload = source.read(MAX_OPERATION_BYTES + 1)
    if len(payload) > MAX_OPERATION_BYTES:
        raise SupervisorError("Deployment journal exceeds its resource limit.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SupervisorError("Deployment journal contains duplicate fields.")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SupervisorError("Deployment journal is invalid JSON.") from exc


def _proof(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() == {"path", "sha256"}
        and isinstance(value["path"], str)
        and Path(value["path"]).is_absolute()
        and ".." not in Path(value["path"]).parts
        and _sha(value["sha256"])
    )


class OperationStore:
    def __init__(
        self, directory: Path, releases_root: Path, *, status_path: Path | None = None
    ) -> None:
        self.directory = directory
        self.releases_root = releases_root
        self.status_path = status_path

    @contextmanager
    def locked(self) -> Iterator[None]:
        _private_directory(self.directory)
        descriptor = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise SupervisorError("The supervisor operation lock is unsafe.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OperationBusy("Another supervisor operation is already active.") from exc
            yield
        finally:
            os.close(descriptor)

    def validate(self, record: object) -> dict:
        if (
            not isinstance(record, dict)
            or record.keys() != _FIELDS
            or type(record["version"]) is not int
            or record["version"] != 1
        ):
            raise SupervisorError("Deployment journal format is unsupported.")
        try:
            identity = uuid.UUID(record["operation_id"])
            if identity.version != 4 or str(identity) != record["operation_id"]:
                raise ValueError
        except (ValueError, AttributeError, TypeError) as exc:
            raise SupervisorError("Deployment operation identity is invalid.") from exc
        if (
            record["kind"] not in ("update", "restore")
            or not isinstance(record["phase"], str)
            or record["phase"] not in PHASES
            or not _sha(record["nonce"])
        ):
            raise SupervisorError("Deployment operation state is unsupported.")
        for name in ("previous", "target"):
            validate_selected_receipt(record[name], releases_root=self.releases_root)
        if record["kind"] == "update" and record["previous"] == record["target"]:
            raise SupervisorError("An update must select a different release.")
        if type(record["previous_uninitialized"]) is not bool or (
            record["previous_uninitialized"] and record["kind"] != "restore"
        ):
            raise SupervisorError(
                "Only a fresh restore can begin without initialized application data."
            )
        for name in ("checkpoint", "candidate_checkpoint"):
            checkpoint = record[name]
            if checkpoint is not None and (
                not isinstance(checkpoint, dict)
                or checkpoint.keys() != {"directory", "sha256", "boundary_sha256"}
                or not isinstance(checkpoint["directory"], str)
                or not Path(checkpoint["directory"]).is_absolute()
                or ".." in Path(checkpoint["directory"]).parts
                or not _sha(checkpoint["sha256"])
                or not _sha(checkpoint["boundary_sha256"])
            ):
                raise SupervisorError("Deployment checkpoint identity is invalid.")
        for name in ("previous_proof", "target_proof"):
            if record[name] is not None and not _proof(record[name]):
                raise SupervisorError("Deployment application proof is invalid.")
        if record["phase"] not in EARLY - {"checkpoint_ready"} | {"aborted"} and any(
            record[name] is None
            for name in (
                "checkpoint",
                "target_proof",
                *(() if record["previous_uninitialized"] else ("previous_proof",)),
            )
        ):
            raise SupervisorError("Deployment state lacks its exact checked checkpoint.")
        if record["kind"] == "update" and record["candidate_checkpoint"] is not None:
            raise SupervisorError("An update cannot replace application data before migration.")
        if (
            record["kind"] == "restore"
            and record["phase"] not in EARLY - {"checkpoint_ready"} | {"aborted"}
            and record["candidate_checkpoint"] is None
        ):
            raise SupervisorError("A restore lacks its verified candidate checkpoint.")
        if record["error"] is not None and (
            not isinstance(record["error"], str) or len(record["error"]) > 4096
        ):
            raise SupervisorError("Deployment failure diagnostic is invalid.")
        return record

    def write(self, record: dict) -> None:
        self.validate(record)
        _private_directory(self.directory)
        path = self.directory / f"{record['operation_id']}.json"
        temporary = path.with_suffix(".tmp")
        if os.path.lexists(temporary):
            info = temporary.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise SupervisorError("Deployment journal staging is unsafe.")
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            + b"\n"
        )
        if len(payload) > MAX_OPERATION_BYTES:
            raise SupervisorError("Deployment journal exceeds its resource limit.")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync(self.directory)
        if self.status_path is not None:
            # Observational projection only: recovery always reads private journals.
            from rcp_supervisor.runtime import write_root_json

            write_root_json(
                self.status_path,
                {
                    "version": 1,
                    "operation_id": record["operation_id"],
                    "kind": record["kind"],
                    "phase": record["phase"],
                    "previous_build": record["previous"]["build"],
                    "target_build": record["target"]["build"],
                    "error": record["error"],
                    "active": record["phase"] not in TERMINAL,
                },
            )

    def active(self) -> dict | None:
        _private_directory(self.directory)
        active = []
        with os.scandir(self.directory) as paths:
            for count, entry in enumerate(paths):
                if count > 10000:
                    raise SupervisorError("Supervisor journal inventory exceeds its limit.")
                path = Path(entry.path)
                if path.name == "lock":
                    continue
                if path.suffix == ".tmp":
                    # An interrupted atomic write cannot supersede its fsynced
                    # journal. A new operation that never published is inert.
                    try:
                        identity = uuid.UUID(path.stem)
                        if str(identity) != path.stem or identity.version != 4:
                            raise ValueError
                    except ValueError as exc:
                        raise SupervisorError("Unknown supervisor staging file.") from exc
                    info = path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        raise SupervisorError("Supervisor journal staging is unsafe.")
                    continue
                if path.suffix != ".json":
                    raise SupervisorError("Unknown file in supervisor journal storage.")
                record = self.validate(_read(path))
                if path.name != f"{record['operation_id']}.json":
                    raise SupervisorError("Deployment journal name differs from its identity.")
                if record["phase"] not in TERMINAL:
                    active.append(record)
        if len(active) > 1:
            raise SupervisorError(
                "Multiple unfinished deployment journals require operator repair."
            )
        return active[0] if active else None


class Coordinator:
    def __init__(
        self,
        store: OperationStore,
        runtime: Runtime,
        *,
        boundary: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.boundary = boundary or (lambda _name: None)

    def _phase(self, operation: dict, phase: str, **changes) -> dict:
        updated = dict(operation, phase=phase, **changes)
        self.store.write(updated)
        self.boundary(phase)
        return updated

    def deploy(
        self,
        previous: dict,
        target: dict,
        *,
        kind: str = "update",
        previous_uninitialized: bool = False,
    ) -> dict:
        with self.store.locked(), ExitStack() as resources:
            if self.store.active() is not None:
                raise SupervisorError(
                    "Recover the unfinished supervisor operation before deploying."
                )
            if self.runtime.selected_release() != previous:
                raise SupervisorError(
                    "The selected release changed before deployment; rerun the operation."
                )
            operation = {
                "version": 1,
                "operation_id": str(uuid.uuid4()),
                "kind": kind,
                "phase": "preparing",
                "previous": previous,
                "target": target,
                "nonce": uuid.uuid4().hex + uuid.uuid4().hex,
                "checkpoint": None,
                "candidate_checkpoint": None,
                "previous_uninitialized": previous_uninitialized,
                "previous_proof": None,
                "target_proof": None,
                "error": None,
            }
            self.store.write(operation)
            self.boundary("preparing")
            try:
                if not previous_uninitialized:
                    self.runtime.protected_backup()
                operation = self._phase(operation, "backup_ready")
                resources.enter_context(self.runtime.deployment_lock())
                operation = self._phase(operation, "entering_maintenance")
                capture = (
                    None if previous_uninitialized else self.runtime.enter_maintenance(operation)
                )
                operation = self._phase(operation, "quiescent")
                self.runtime.stop_service()
                checkpoint, previous_proof, target_proof, candidate_checkpoint = (
                    self.runtime.prepare_fresh_restore(operation)
                    if previous_uninitialized
                    else self.runtime.prepare(operation, capture)
                )
                operation = self._phase(
                    operation,
                    "checkpoint_ready",
                    checkpoint=checkpoint,
                    previous_proof=previous_proof,
                    target_proof=target_proof,
                    candidate_checkpoint=candidate_checkpoint,
                )
                operation = self._phase(operation, "activating")
                if candidate_checkpoint is not None:
                    self.runtime.restore_roots(candidate_checkpoint)
                self.runtime.switch_pointer(target, allowed=(previous, target))
                operation = self._phase(operation, "pointer_switched")
                self.runtime.probe(target, operation, target_proof)
                operation = self._phase(operation, "candidate_verified")
                # This journal is authoritative before selection/start: no later
                # failure may restore old data over potentially accepted work.
                operation = self._phase(operation, "candidate_chosen")
                self.runtime.select(target)
                self.runtime.start_service()
                return self._phase(operation, "committed")
            except Exception as exc:
                # Read the durable state, not a stale local record when a write
                # completed immediately before its notification raised.
                operation = self.store.active()
                if operation is None:
                    raise
                operation = dict(operation, error=str(exc)[:4096])
                self.store.write(operation)
                if operation["phase"] in ("candidate_chosen", "previous_chosen"):
                    raise SupervisorError(
                        "The selected release failed to start; recovery must preserve its data."
                    ) from exc
                recovered = self._recover(operation, startup=False)
                if operation["kind"] == "restore":
                    from rcp_supervisor.restore import RestoreOperatorAction

                    if isinstance(exc, RestoreOperatorAction):
                        raise
                raise SupervisorError(
                    f"Deployment failed; recovery ended in {recovered['phase']}: {str(exc)[:2048]}"
                ) from exc

    def recover(self, *, startup: bool = False) -> dict | None:
        with self.store.locked():
            operation = self.store.active()
            if operation is None:
                return None
            with self.runtime.deployment_lock():
                return self._recover(operation, startup=startup)

    def _recover(self, operation: dict, *, startup: bool) -> dict:
        phase = operation["phase"]
        if phase in ("candidate_chosen", "previous_chosen"):
            release = operation["target"] if phase == "candidate_chosen" else operation["previous"]
            if not startup:
                self.runtime.stop_service()
            self.runtime.switch_pointer(
                release, allowed=(operation["previous"], operation["target"])
            )
            self.runtime.select(release)
            if phase == "previous_chosen" and operation["previous_uninitialized"]:
                return self._phase(operation, "rolled_back")
            # Proof belongs to the original boundary and is stale once work may
            # have been admitted. Verify current startup, never replay old data.
            self.runtime.probe(release, operation, None)
            if not startup:
                self.runtime.start_service()
            return self._phase(
                operation, "committed" if phase == "candidate_chosen" else "rolled_back"
            )
        if phase in EARLY:
            if operation["previous_uninitialized"]:
                if not startup:
                    self.runtime.stop_service()
                return self._phase(operation, "aborted")
            if not startup and phase in ("preparing", "backup_ready", "entering_maintenance"):
                self.runtime.abort_maintenance(operation)
                return self._phase(operation, "aborted")
            if not startup:
                self.runtime.stop_service()
            self.runtime.switch_pointer(operation["previous"], allowed=(operation["previous"],))
            self.runtime.probe(operation["previous"], operation, operation["previous_proof"])
            # Early abort cannot reverse data: nothing has activated yet.
            self.runtime.select(operation["previous"])
            operation = self._phase(operation, "aborted")
            if not startup:
                self.runtime.start_service()
            return operation
        if not startup:
            self.runtime.stop_service()
        if phase not in ROLLBACK:
            operation = self._phase(operation, "rollback_started")
        if operation["phase"] == "rollback_started":
            self.runtime.restore_roots(operation["checkpoint"])
            operation = self._phase(operation, "rollback_roots_complete")
        if operation["phase"] == "rollback_roots_complete":
            self.runtime.switch_pointer(
                operation["previous"], allowed=(operation["previous"], operation["target"])
            )
            operation = self._phase(operation, "previous_pointer_restored")
        if operation["phase"] == "previous_pointer_restored":
            if not operation["previous_uninitialized"]:
                self.runtime.probe(operation["previous"], operation, operation["previous_proof"])
            operation = self._phase(operation, "previous_verified")
        operation = self._phase(operation, "previous_chosen")
        self.runtime.select(operation["previous"])
        if not startup and not operation["previous_uninitialized"]:
            self.runtime.start_service()
        return self._phase(operation, "rolled_back")
