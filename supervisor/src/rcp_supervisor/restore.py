"""Protected archive decryption and resumable human review outside the application."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import stat
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import (
    IDENTITY_TIMEOUT_SECONDS,
    MAX_APP_OUTPUT_BYTES,
    MAX_RESTORE_ARCHIVE_BYTES,
    MAX_RESTORE_PROGRESS_BYTES,
    MAX_RESTORE_PROGRESS_STEPS,
    MAX_SELECTED_RECEIPT_BYTES,
    RESTORE_TIMEOUT_SECONDS,
)
from rcp_supervisor.root_tools import root_executable
from rcp_supervisor.runtime import _read_file, _sync, write_root_json


class RestoreOperatorAction(SupervisorError):
    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def _hash(path: Path, *, uid: int | None = None) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _hash_descriptor(descriptor, uid=uid)
    finally:
        os.close(descriptor)


def _hash_descriptor(descriptor: int, *, uid: int | None = None) -> str:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_mode & 0o022
        or (uid is not None and before.st_uid != uid)
    ):
        raise SupervisorError("Protected archive input has unsafe metadata.")
    if before.st_size > MAX_RESTORE_ARCHIVE_BYTES:
        raise SupervisorError("Protected archive exceeds its byte bound.")
    digest = hashlib.sha256()
    deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
    total = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        total += len(chunk)
        if total > MAX_RESTORE_ARCHIVE_BYTES or time.monotonic() >= deadline:
            raise SupervisorError("Protected archive read exceeded its byte/time bound.")
        digest.update(chunk)
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise SupervisorError("Protected archive input changed during verification.")
    return digest.hexdigest()


def _directory(path: Path, gid: int) -> None:
    if not os.path.lexists(path):
        path.mkdir(mode=0o710)
        os.chown(path, 0, gid)
        path.chmod(0o710)
        _sync(path.parent)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != gid
        or stat.S_IMODE(info.st_mode) != 0o710
    ):
        raise SupervisorError("Restore preparation storage has unsafe ownership or mode.")


def _tool_output(executable: str, arguments: list[str]) -> bytes:
    process = subprocess.Popen(
        [executable, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    result = bytearray()
    deadline = time.monotonic() + IDENTITY_TIMEOUT_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SupervisorError("Recovery identity command exceeded its time bound.")
                for key, _ in selector.select(remaining):
                    chunk = os.read(
                        key.fd, min(65536, MAX_SELECTED_RECEIPT_BYTES - len(result) + 1)
                    )
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    result.extend(chunk)
                    if len(result) > MAX_SELECTED_RECEIPT_BYTES:
                        raise SupervisorError(
                            "Recovery identity command exceeded its output bound."
                        )
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            raise SupervisorError("Recovery identity command failed.")
        return bytes(result)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()


def _decrypt(
    archive: Path, identity: Path, destination: Path, gid: int, expected_archive_sha256: str
) -> tuple[str, str]:
    for parent in identity.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SupervisorError("Recovery identity traverses an unsafe root directory.")
    info = identity.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise SupervisorError("Recovery identity must be one root-owned mode-0600 regular file.")
    process = None
    try:
        age = root_executable("age")
        keygen = root_executable("age-keygen")
        version = _tool_output(age, ["--version"])
        if not version.strip().lstrip(b"v").startswith(b"1."):
            raise SupervisorError("Protected restore requires age 1.x.")
        recipient = _tool_output(keygen, ["-y", str(identity)]).strip()
        if not recipient.startswith(b"age1") or len(recipient) != 62 or not recipient.isalnum():
            raise SupervisorError("Recovery identity is not one native X25519 age identity.")
        archive_fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(archive_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > MAX_RESTORE_ARCHIVE_BYTES
            ):
                raise SupervisorError("Encrypted archive has unsafe metadata.")
            digest = _hash_descriptor(archive_fd)
            os.lseek(archive_fd, 0, os.SEEK_SET)
            if digest != expected_archive_sha256:
                raise SupervisorError("Encrypted archive changed before decryption.")
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            log = os.open(
                destination.with_suffix(".stderr"),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                process = subprocess.Popen(
                    [age, "--decrypt", "--identity", str(identity)],
                    stdin=archive_fd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
                sizes = {"archive": 0, "stderr": 0}
                with selectors.DefaultSelector() as selector:
                    for pipe, kind in ((process.stdout, "archive"), (process.stderr, "stderr")):
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, selectors.EVENT_READ, kind)
                    while selector.get_map():
                        if time.monotonic() >= deadline:
                            raise SupervisorError(
                                "Protected archive decryption exceeded its time bound."
                            )
                        for item, _ in selector.select(min(1, max(0, deadline - time.monotonic()))):
                            chunk = os.read(item.fd, 1024 * 1024)
                            if not chunk:
                                selector.unregister(item.fileobj)
                                continue
                            sizes[item.data] += len(chunk)
                            limit = (
                                MAX_RESTORE_ARCHIVE_BYTES
                                if item.data == "archive"
                                else MAX_APP_OUTPUT_BYTES
                            )
                            if sizes[item.data] > limit:
                                raise SupervisorError(
                                    "Protected archive decryption exceeded its output bound."
                                )
                            view = memoryview(chunk)
                            while view:
                                count = os.write(
                                    descriptor if item.data == "archive" else log, view
                                )
                                if count <= 0:
                                    raise OSError("short restore output write")
                                view = view[count:]
                if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                    raise SupervisorError(
                        "Protected archive decryption failed; inspect the root-owned stderr file."
                    )
                after = os.fstat(archive_fd)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise SupervisorError("Encrypted archive changed during decryption.")
                os.fchown(descriptor, 0, gid)
                os.fchmod(descriptor, 0o640)
                os.fsync(descriptor)
                os.fsync(log)
            finally:
                os.close(descriptor)
                os.close(log)
        finally:
            os.close(archive_fd)
        _sync(destination.parent)
        return _hash(destination, uid=0), hashlib.sha256(recipient).hexdigest()
    except (subprocess.SubprocessError, OSError) as exc:
        raise SupervisorError(
            "Protected archive decryption failed; retained root-owned diagnostics require inspection."
        ) from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if process is not None:
            process.stdout.close()
            process.stderr.close()


def prepare_restore(runtime, request: dict, operation: dict, prepared_previous: dict):
    if not isinstance(request, dict):
        raise SupervisorError("Restore requires its explicit protected archive request.")
    required = {"archive_path", "identity_file", "confirmed_data_dir", "confirmed_by"}
    optional = {
        "old_authority_disposition",
        "confirm_old_authority",
        "confirm_member_roster",
        "remove_stale_member",
    }
    if required - request.keys() or request.keys() - required - optional:
        raise SupervisorError(
            "Restore request is missing required fields or contains unknown fields."
        )
    if request["confirmed_data_dir"] != str(runtime.paths.data_dir):
        raise SupervisorError("Confirm the exact installed data directory before restoring.")
    archive = Path(request["archive_path"])
    identity = Path(request["identity_file"])
    if any(not p.is_absolute() or ".." in p.parts for p in (archive, identity)):
        raise SupervisorError("Protected archive and identity require absolute normalized paths.")
    archive_digest = _hash(archive)
    key = hashlib.sha256(
        json.dumps(
            {"archive_sha256": archive_digest, "selected": operation["target"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    storage = runtime.paths.supervisor / "restore-preparations"
    _directory(storage, runtime.gid)
    preparation = storage / key
    _directory(preparation, runtime.gid)
    state_path = preparation / "progress.json"
    if os.path.lexists(state_path):
        state = json.loads(_read_file(state_path, uid=0, max_bytes=MAX_RESTORE_PROGRESS_BYTES))
        if (
            not isinstance(state, dict)
            or state.keys()
            != {
                "version",
                "archive_sha256",
                "selected",
                "preparation_id",
                "detached_at",
                "plaintext_sha256",
                "recipient_fingerprint",
                "progress",
            }
            or type(state["version"]) is not int
            or state["version"] != 1
            or state["archive_sha256"] != archive_digest
            or state["selected"] != operation["target"]
        ):
            raise SupervisorError(
                "Saved restore preparation is invalid or names another archive/release."
            )
        if _hash(preparation / "archive.tar", uid=0) != state["plaintext_sha256"]:
            raise SupervisorError("Saved decrypted archive changed.")
    else:
        for orphan in (preparation / "archive.tar", preparation / "archive.stderr"):
            if os.path.lexists(orphan):
                info = orphan.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0
                    or info.st_nlink != 1
                    or info.st_mode & 0o022
                ):
                    raise SupervisorError("Unsealed restore payload has unsafe metadata.")
                os.replace(orphan, orphan.with_name(f"unsealed-{uuid.uuid4()}-{orphan.name}"))
                _sync(preparation)
        plain_digest, recipient = _decrypt(
            archive, identity, preparation / "archive.tar", runtime.gid, archive_digest
        )
        if _hash(archive) != archive_digest:
            raise SupervisorError("Encrypted archive changed during decryption.")
        state = {
            "version": 1,
            "archive_sha256": archive_digest,
            "selected": operation["target"],
            "preparation_id": str(uuid.uuid4()),
            "detached_at": datetime.now(UTC).isoformat(),
            "plaintext_sha256": plain_digest,
            "recipient_fingerprint": recipient,
            "progress": [],
        }
        write_root_json(state_path, state)
    workspace = runtime.paths.checkpoints_root / operation["operation_id"]
    resume = [
        "sudo",
        "rcp",
        "server",
        "restore",
        str(archive),
        "--identity-file",
        str(identity),
        "--confirm-data-dir",
        str(runtime.paths.data_dir),
    ]
    for name in optional:
        if request.get(name) is not None:
            resume.extend(["--" + name.replace("_", "-"), request[name]])
    for _step in range(MAX_RESTORE_PROGRESS_STEPS):
        app_request = {
            "version": 1,
            "data_dir": str(runtime.paths.data_dir),
            "output_dir": str(workspace / f"restore-prepared-{uuid.uuid4()}"),
            "plaintext_path": str(preparation / "archive.tar"),
            "plaintext_sha256": state["plaintext_sha256"],
            "recipient_fingerprint": state["recipient_fingerprint"],
            "source_commit": operation["target"]["commit"],
            "preparation_id": state["preparation_id"],
            "detached_at": state["detached_at"],
            "progress": state["progress"],
            "previous_roots": prepared_previous["roots"],
            "resume_argv": resume,
            **{
                k: v
                for k, v in request.items()
                if k in optional or k in {"confirmed_data_dir", "confirmed_by"}
            },
        }
        result = runtime.application(operation["target"], "restore-prepare", app_request)
        if result.get("status") in {"continue", "operator_action_needed"}:
            updated = result.get("progress")
            if (
                not isinstance(updated, list)
                or len(json.dumps(updated).encode()) > MAX_RESTORE_PROGRESS_BYTES
            ):
                raise SupervisorError("Application restore progress is invalid.")
            if result["status"] == "continue" and updated == state["progress"]:
                raise SupervisorError("Application restore preparation did not advance.")
            state["progress"] = updated
            write_root_json(state_path, state)
            if result["status"] == "continue":
                continue
            actions = list(result.get("actions", []))
            fields = {item["name"]: item["value"] for item in result.get("fields", [])}
            if (
                request.get("confirm_old_authority") is None
                or request.get("confirm_member_roster") is None
                or request.get("old_authority_disposition") is None
            ):
                base = resume[:]
                # Existing optional values are rebuilt exactly once below.
                for name in optional:
                    flag = "--" + name.replace("_", "-")
                    if flag in base:
                        index = base.index(flag)
                        del base[index : index + 2]
                if request.get("remove_stale_member") is not None:
                    base += ["--remove-stale-member", request["remove_stale_member"]]
                dispositions = (
                    [request["old_authority_disposition"]]
                    if request.get("old_authority_disposition")
                    else ["old-machine-destroyed", "old-machine-fenced-and-credentials-revoked"]
                )
                actions += [
                    {
                        "kind": "command",
                        "argv": [
                            *base,
                            "--old-authority-disposition",
                            disposition,
                            "--confirm-old-authority",
                            fields["old_authority_boundary"],
                            "--confirm-member-roster",
                            fields["member_roster_boundary"],
                        ],
                    }
                    for disposition in dispositions
                ]
            raise RestoreOperatorAction(
                result.get("diagnostic", "Protected restore requires operator review."),
                {
                    "actions": actions,
                    "fields": result.get("fields", []),
                    "resume_argv": resume,
                },
            )
        if result.get("status") != "prepared":
            raise SupervisorError("Application restore returned no verified candidate.")
        checked = runtime.application(
            operation["target"],
            "validate",
            {
                "version": 1,
                "proof_path": result["proof_path"],
                "proof_sha256": result["proof_sha256"],
                "output_dir": str(workspace / f"restore-validated-{uuid.uuid4()}"),
            },
        )
        candidate = runtime.filesystem(
            "checkpoint",
            {
                "directory": str(workspace / "restore-candidate"),
                "roots": result["roots"],
                "boundary_sha256": result["boundary_sha256"],
            },
        )
        return (
            candidate,
            {"path": checked["proof_path"], "sha256": checked["proof_sha256"]},
            result["extra_previous_roots"],
        )

    raise SupervisorError("Restore preparation exceeded its bounded progress steps.")
