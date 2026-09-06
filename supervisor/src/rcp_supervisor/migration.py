"""One-time guarded adoption of the shipped source installation.

The caller holds the normal root operation lock. Old application code is never
asked to implement the new maintenance protocol. It is stopped before the raw
checkpoint; only verified candidate code runs thereafter, behind closed admission.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.launch import validate_selected_receipt
from rcp_supervisor.limits import (
    CHECKPOINT_COPY_BYTES,
    HTTP_TIMEOUT_SECONDS,
    LEGACY_START_POLL_SECONDS,
    MAX_CHECKPOINT_BYTES,
    MAX_OPERATION_BYTES,
    MAX_SELECTED_RECEIPT_BYTES,
    PROBE_TIMEOUT_SECONDS,
    SERVICE_TIMEOUT_SECONDS,
    STARTUP_RECOVERY_TIMEOUT_SECONDS,
)
from rcp_supervisor.operations import _read
from rcp_supervisor.root_tools import root_executable
from rcp_supervisor.runtime import _read_file, _sync, write_root_json

_PHASES = {
    "prepared",
    "guarded",
    "stopped",
    "raw_checkpoint_ready",
    "checkpoint_ready",
    "probing",
    "candidate_chosen",
    "rollback_chosen",
    "previous_chosen",
    "committed",
    "rolled_back",
}
_FIELDS = {
    "version",
    "operation_id",
    "phase",
    "nonce",
    "previous",
    "target",
    "old",
    "integration",
    "raw_checkpoint",
    "checkpoint",
    "target_proof",
    "legacy_backup",
    "protected_backup",
}
_GUARD = "ExecStartPre=+/usr/local/bin/rcp-supervisor recover --startup"


def _integration_paths(runtime) -> dict[str, Path]:
    config = runtime.paths.config
    root = config.parents[2]
    if config != root / "etc/rcp/server.toml":
        raise SupervisorError("Source adoption requires the fixed installed config layout.")
    return {
        "config": config,
        "wrapper": root / "usr/local/bin/rcp",
        "unit": root / "etc/systemd/system/rcp.service",
    }


def _content(path: Path) -> dict:
    data = _read_file(path, uid=0, max_bytes=MAX_OPERATION_BYTES // 8)
    info = path.stat()
    return {"text": data.decode("utf-8"), "mode": stat.S_IMODE(info.st_mode), "gid": info.st_gid}


def _write_content(path: Path, item: dict) -> None:
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SupervisorError("Adoption integration parent is not protected by root.")
    if path.is_symlink():
        raise SupervisorError("Adoption never replaces symbolic-link launch files.")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.adoption-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(item["text"].encode())
            os.fchmod(output.fileno(), item["mode"])
            os.fchown(output.fileno(), 0, item["gid"])
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _journal(runtime) -> Path:
    return runtime.paths.supervisor / "adoption.json"


def _publish(runtime, record: dict, phase: str) -> None:
    record["phase"] = phase
    document = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    if len(document.encode()) > MAX_OPERATION_BYTES:
        raise SupervisorError("Adoption journal exceeds its size limit.")
    _write_content(_journal(runtime), {"text": document, "mode": 0o600, "gid": 0})
    write_root_json(
        runtime.paths.supervisor / "status.json",
        {
            "version": 1,
            "operation_id": record["operation_id"],
            "kind": "migration",
            "phase": "adoption_" + phase,
            "previous_build": None,
            "target_build": record["target"]["build"],
            "error": None,
            "active": phase not in {"committed", "rolled_back"},
        },
    )
    runtime.notify("adoption_" + phase)


def _validate(runtime, record: dict) -> dict:
    try:
        return _validate_document(runtime, record)
    except (TypeError, ValueError, KeyError) as exc:
        raise SupervisorError("Adoption journal is invalid.") from exc


def _validate_document(runtime, record: dict) -> dict:
    if (
        not isinstance(record, dict)
        or record.keys() != _FIELDS
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["phase"] not in _PHASES
    ):
        raise SupervisorError("Adoption journal is invalid.")
    if (
        str(uuid.UUID(record["operation_id"])) != record["operation_id"]
        or re.fullmatch(r"[0-9a-f]{64}", record["nonce"]) is None
    ):
        raise SupervisorError("Adoption journal identity is invalid.")
    validate_selected_receipt(record["target"], releases_root=runtime.paths.releases_root)
    previous = record["previous"]
    if (
        not isinstance(previous, dict)
        or previous.keys() != {"release_directory", "commit", "version_string"}
        or re.fullmatch(r"[0-9a-f]{40}", previous["commit"]) is None
        or previous["release_directory"] != str(runtime.paths.releases_root / previous["commit"])
    ):
        raise SupervisorError("Adoption legacy release identity is invalid.")
    for container in (record["old"], record["integration"]):
        if not isinstance(container, dict) or container.keys() != {"config", "unit", "wrapper"}:
            raise SupervisorError("Adoption integration inventory is incomplete.")
        for item in container.values():
            if (
                not isinstance(item, dict)
                or item.keys() != {"text", "mode", "gid"}
                or not isinstance(item["text"], str)
                or len(item["text"].encode()) > MAX_OPERATION_BYTES // 8
                or type(item["mode"]) is not int
                or item["mode"] not in {0o640, 0o644, 0o755}
                or type(item["gid"]) is not int
                or item["gid"] < 0
            ):
                raise SupervisorError("Adoption integration entry is invalid.")
    workspace = runtime.paths.checkpoints_root / record["operation_id"]
    for key, name in (("raw_checkpoint", "raw-checkpoint"), ("checkpoint", "checkpoint")):
        value = record[key]
        if value is not None and (
            not isinstance(value, dict)
            or value.keys() != {"directory", "sha256", "boundary_sha256"}
            or value["directory"] != str(workspace / name)
            or any(
                not isinstance(value[k], str) or re.fullmatch(r"[0-9a-f]{64}", value[k]) is None
                for k in ("sha256", "boundary_sha256")
            )
        ):
            raise SupervisorError("Adoption checkpoint reference is invalid.")
    if record["legacy_backup"] not in {"protected", "unavailable"}:
        raise SupervisorError("Legacy backup status is invalid.")
    if record["protected_backup"] is not None:
        backup = record["protected_backup"]
        if (
            not isinstance(backup, dict)
            or backup.keys() != {"receipt_path", "receipt_sha256", "archive_path", "archive_sha256"}
            or any(
                not isinstance(backup[key], str)
                or re.fullmatch(r"[0-9a-f]{64}", backup[key]) is None
                for key in ("receipt_sha256", "archive_sha256")
            )
        ):
            raise SupervisorError("Adoption protected backup receipt is invalid.")
    if record["phase"] in {"probing", "candidate_chosen", "committed"} and (
        record["checkpoint"] is None or record["protected_backup"] is None
    ):
        raise SupervisorError("Adoption changed application state without a sealed checkpoint.")
    return record


def _artifact_hash(path: Path, *, uid: int) -> str:
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            raise SupervisorError("Protected adoption artifacts cannot traverse symbolic links.")
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != uid
        or before.st_nlink != 1
        or before.st_mode & 0o077
        or before.st_size > MAX_CHECKPOINT_BYTES
    ):
        raise SupervisorError("Protected adoption artifact is not one bounded private file.")
    digest = hashlib.sha256()
    total = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if os.fstat(source.fileno()) != before:
            raise SupervisorError("Protected adoption artifact changed while opening.")
        while chunk := source.read(CHECKPOINT_COPY_BYTES):
            total += len(chunk)
            if total > MAX_CHECKPOINT_BYTES:
                raise SupervisorError("Protected adoption artifact exceeds its bound.")
            digest.update(chunk)
        after = os.fstat(source.fileno())
        if any(
            getattr(before, name) != getattr(after, name)
            for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise SupervisorError("Protected adoption artifact changed during verification.")
    return digest.hexdigest()


def _verify_protected_backup(runtime, protected: dict, output: Path) -> dict:
    if (
        not isinstance(protected, dict)
        or protected.get("version") != 1
        or protected.get("status") != "protected"
        or type(protected.get("uncaptured_projects")) is not int
        or protected["uncaptured_projects"] != 0
    ):
        raise SupervisorError(
            "A complete current protected backup is required before source adoption."
        )
    result = {}
    for label in ("receipt", "archive"):
        path_value = protected.get(label + "_path")
        expected = protected.get(label + "_sha256")
        if (
            not isinstance(path_value, str)
            or not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise SupervisorError("Protected adoption receipt is invalid.")
        path = Path(path_value)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not path.is_relative_to(output)
            or path == output
            or _artifact_hash(path, uid=runtime.uid) != expected
        ):
            raise SupervisorError(
                "Protected adoption artifact differs from its complete capture receipt."
            )
        result[label + "_path"] = path_value
        result[label + "_sha256"] = expected
    return result


def _reload(runtime) -> None:
    if not runtime.startup:
        subprocess.run(
            [root_executable("systemctl"), "daemon-reload"],
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=SERVICE_TIMEOUT_SECONDS,
        )


def _pointer(runtime, target: str, *, allowed: set[str]) -> None:
    current = runtime.paths.current
    info = current.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or os.readlink(current) not in allowed:
        raise SupervisorError("Adoption current pointer differs from its sealed identities.")
    temporary = current.with_name(".current-adoption-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, current)
        _sync(current.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _legacy_start(runtime, previous: dict) -> None:
    if runtime.startup:
        return
    runtime._systemctl("start")
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            metadata = runtime.metadata()
            if metadata.get("running_commit") == previous["commit"] and _legacy_health(
                runtime, metadata
            ):
                return
        except (OSError, SupervisorError):
            pass
        time.sleep(LEGACY_START_POLL_SECONDS)
    raise SupervisorError("Restored source service did not publish its original release identity.")


def _legacy_health(runtime, metadata: dict) -> bool:
    result = subprocess.run(
        [
            root_executable("systemctl"),
            "show",
            "--property=MainPID",
            "--value",
            runtime.paths.service_unit,
        ],
        capture_output=True,
        check=True,
        timeout=SERVICE_TIMEOUT_SECONDS,
    )
    if (
        result.stdout.strip() != str(metadata.get("pid")).encode()
        or type(metadata.get("pid")) is not int
        or metadata["pid"] <= 0
    ):
        return False
    connection = http.client.HTTPConnection(
        "127.0.0.1", runtime.paths.port, timeout=HTTP_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        content = response.read(MAX_SELECTED_RECEIPT_BYTES + 1)
        if response.status != 200 or len(content) > MAX_SELECTED_RECEIPT_BYTES:
            return False
        health = json.loads(content)
        return (
            isinstance(health, dict)
            and health.get("status") == "ok"
            and health.get("space_kind") == "team"
            and all(
                health.get(key) == metadata.get(key)
                for key in ("pid", "instance_id", "data_dir_id", "running_commit")
            )
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _retire_source_keys(runtime) -> None:
    runtime.filesystem(
        "retire-source-keys", {"credentials": str(runtime.paths.data_dir.parent / "credentials")}
    )


def prepare_deployment_lock(directory: Path) -> None:
    """Create the shared backup fence as rcp without replacing an existing inode."""
    if os.geteuid() == 0 or not directory.is_absolute() or ".." in directory.parts:
        raise SupervisorError("The deployment lock must be prepared by its service account.")
    for path in (*reversed(directory.parents), directory):
        if path.is_symlink():
            raise SupervisorError("Deployment lock ancestry cannot contain symbolic links.")
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SupervisorError("The deployment lock requires the private service directory.")
    descriptor = os.open(
        directory / ".backup-run.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid())
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SupervisorError("The deployment lock has unsafe metadata.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync(directory)


def retire_source_keys(credentials: Path) -> None:
    """As the service account, retire only the two obsolete installation keys."""
    if os.geteuid() == 0:
        raise SupervisorError("Retire application source keys as the service account, not root.")
    if not credentials.is_absolute() or ".." in credentials.parts:
        raise SupervisorError("Source-key retirement requires a normalized directory.")
    if not credentials.exists():
        return
    for parent in (*reversed(credentials.parents), credentials):
        if parent.is_symlink():
            raise SupervisorError("Source-key retirement refuses symbolic-link ancestry.")
    owner = credentials.stat()
    if not stat.S_ISDIR(owner.st_mode) or owner.st_uid != os.geteuid() or owner.st_mode & 0o077:
        raise SupervisorError(
            "Source-key retirement requires the private rcp credentials directory."
        )
    for name, mode in (("source_ed25519", 0o600), ("source_ed25519.pub", 0o644)):
        path = credentials / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode
        ):
            raise SupervisorError(
                "An obsolete source-key file has unsafe metadata; it was retained."
            )
        path.unlink()
    # Also seal an idempotent retry after a process died between unlink and
    # directory fsync; absent names alone are not a durability receipt.
    _sync(credentials)


def _finish_candidate(runtime, record: dict) -> None:
    paths = _integration_paths(runtime)
    for key in ("config", "wrapper", "unit"):
        _write_content(paths[key], record["integration"][key])
    _pointer(
        runtime,
        record["target"]["release_directory"],
        allowed={record["previous"]["release_directory"], record["target"]["release_directory"]},
    )
    runtime.select(record["target"])
    _reload(runtime)
    _publish(runtime, record, "committed")
    _retire_source_keys(runtime)
    if not runtime.startup:
        runtime.start_service()


def recover(runtime, *, for_install: bool = False) -> dict | None:
    """Recover while excluding backup writers; None defers to ordinary recovery."""
    with runtime.deployment_lock():
        return _recover(runtime, for_install=for_install)


def _recover(runtime, *, for_install: bool = False) -> dict | None:
    path = _journal(runtime)
    if not path.exists() and not path.is_symlink():
        return None
    record = _validate(runtime, _read(path))
    if record["phase"] == "committed":
        _retire_source_keys(runtime)
        return None
    if record["phase"] == "rolled_back":
        if for_install:
            # A completed rollback may be archived and retried by adopt.
            return None
        _legacy_start(runtime, record["previous"])
        return record
    if record["phase"] == "candidate_chosen":
        _finish_candidate(runtime, record)
        return record
    if not runtime.startup:
        runtime.stop_service()
    if record["phase"] != "previous_chosen":
        _publish(runtime, record, "rollback_chosen")
        checkpoint = record["checkpoint"] or record["raw_checkpoint"]
        if checkpoint is not None:
            runtime.restore_roots(checkpoint)
        paths = _integration_paths(runtime)
        for key in ("config", "wrapper"):
            _write_content(paths[key], record["old"][key])
        _pointer(
            runtime,
            record["previous"]["release_directory"],
            allowed={
                record["previous"]["release_directory"],
                record["target"]["release_directory"],
            },
        )
        # No path before candidate_chosen publishes selected authority.
        if runtime.paths.selected.exists() or runtime.paths.selected.is_symlink():
            raise SupervisorError("Unexpected selected authority prevents legacy rollback.")
        _publish(runtime, record, "previous_chosen")
    # Keep the recovery guard until all original bytes and launch authority are
    # restored and the durable no-more-restoration decision has been published.
    _write_content(_integration_paths(runtime)["unit"], record["old"]["unit"])
    _reload(runtime)
    _publish(runtime, record, "rolled_back")
    _legacy_start(runtime, record["previous"])
    return record


def _guarded_unit(text: str) -> str:
    # Retain original bytes in the journal, but replace any earlier service
    # startup timeout while the temporary guard owns reboot admission.
    before, service = text.split("[Service]", 1)
    parts = re.split(r"(?m)(?=^\[)", service, maxsplit=1)
    service = re.sub(r"(?m)^[ \t]*TimeoutStartSec[ \t]*=.*\n?", "", parts[0])
    return (
        before
        + "[Service]\n"
        + _GUARD
        + f"\nTimeoutStartSec={STARTUP_RECOVERY_TIMEOUT_SECONDS}"
        + service
        + (parts[1] if len(parts) == 2 else "")
    )


def adopt(runtime, target: dict) -> dict:
    """Adopt one explicit verified candidate, retaining every failed attempt."""
    if runtime.startup:
        raise SupervisorError("Startup may recover adoption but cannot begin one.")
    validate_selected_receipt(target, releases_root=runtime.paths.releases_root)
    journal = _journal(runtime)
    if journal.exists() or journal.is_symlink():
        existing = _validate(runtime, _read(journal))
        if existing["phase"] not in {"rolled_back", "committed"}:
            recover(runtime)
            return _validate(runtime, _read(journal))
        if existing["phase"] == "committed":
            raise SupervisorError("Source adoption already completed; use server update.")
        os.replace(journal, journal.with_name("adoption-" + existing["operation_id"] + ".json"))
        _sync(journal.parent)
    if runtime.paths.selected.exists() or runtime.paths.selected.is_symlink():
        raise SupervisorError("Source adoption refuses an already selected artifact release.")
    previous_path = Path(os.readlink(runtime.paths.current))
    if (
        previous_path.parent != runtime.paths.releases_root
        or re.fullmatch(r"[0-9a-f]{40}", previous_path.name) is None
    ):
        raise SupervisorError("Legacy current must name the exact full-commit source release.")
    metadata = runtime.metadata()
    if metadata.get("running_commit") != previous_path.name or not isinstance(
        metadata.get("app_version"), str
    ):
        raise SupervisorError("Legacy process identity differs from its source release pointer.")
    previous = {
        "release_directory": str(previous_path),
        "commit": previous_path.name,
        "version_string": metadata["app_version"],
    }
    legacy_backup = "protected"
    try:
        runtime.protected_backup(previous)
    except SupervisorError:
        legacy_backup = "unavailable"
        runtime.notify(
            "Legacy backup is incomplete or unavailable; adoption requires a stopped byte checkpoint and a complete current encrypted backup before activation."
        )
    with runtime.deployment_lock():
        return _adopt_guarded(runtime, target, previous, legacy_backup)


def _adopt_guarded(runtime, target: dict, previous: dict, legacy_backup: str) -> dict:
    paths = _integration_paths(runtime)
    old = {key: _content(path) for key, path in paths.items()}
    packet = json.loads(
        _read_file(
            runtime.paths.supervisor / "integration.json", uid=0, max_bytes=MAX_OPERATION_BYTES
        )
    )
    if (
        not isinstance(packet, dict)
        or packet.keys() != {"version", "files"}
        or packet["version"] != 1
    ):
        raise SupervisorError("Run the paired-wheel bootstrap to stage supervisor integration.")
    operation_id = str(uuid.uuid4())
    record = {
        "version": 1,
        "operation_id": operation_id,
        "phase": "prepared",
        "nonce": uuid.uuid4().hex + uuid.uuid4().hex,
        "previous": previous,
        "target": target,
        "old": old,
        "integration": packet["files"],
        "raw_checkpoint": None,
        "checkpoint": None,
        "target_proof": None,
        "legacy_backup": legacy_backup,
        "protected_backup": None,
    }
    _validate(runtime, record)
    candidate_config = tomllib.loads(record["integration"]["config"]["text"])
    if candidate_config.get("schema_version") != 3 or candidate_config.get(
        "installation_id"
    ) != runtime.config.get("installation_id"):
        raise SupervisorError("Staged adoption configuration does not preserve this installation.")
    if _GUARD in old["unit"]["text"] or old["unit"]["text"].count("[Service]") != 1:
        raise SupervisorError("Legacy unit does not match the one-time guard insertion contract.")
    _publish(runtime, record, "prepared")
    try:
        guarded = dict(old["unit"], text=_guarded_unit(old["unit"]["text"]))
        _write_content(paths["unit"], guarded)
        _reload(runtime)
        _publish(runtime, record, "guarded")
        runtime.stop_service()
        _publish(runtime, record, "stopped")
        workspace = runtime.paths.checkpoints_root / operation_id
        runtime.filesystem("workspace", {"directory": str(workspace)})
        record["raw_checkpoint"] = runtime.filesystem(
            "snapshot",
            {
                "directory": str(workspace / "raw-checkpoint"),
                "live": str(runtime.paths.data_dir),
                "boundary_sha256": record["nonce"],
            },
        )
        _publish(runtime, record, "raw_checkpoint_ready")
        prepared = runtime.application(
            target,
            "offline-prepare",
            {
                "version": 1,
                "data_dir": str(runtime.paths.data_dir),
                "output_dir": str(workspace / "prepared"),
                "source_commit": previous["commit"],
            },
        )
        record["checkpoint"] = runtime.filesystem(
            "checkpoint",
            {
                "directory": str(workspace / "checkpoint"),
                "roots": prepared["roots"],
                "boundary_sha256": prepared["boundary_sha256"],
            },
        )
        checked = runtime.application(
            target,
            "validate",
            {
                "version": 1,
                "proof_path": prepared["proof_path"],
                "proof_sha256": prepared["proof_sha256"],
                "output_dir": str(workspace / "validated"),
            },
        )
        record["target_proof"] = {"path": checked["proof_path"], "sha256": checked["proof_sha256"]}
        backup = runtime.config.get("backup")
        if (
            not isinstance(backup, dict)
            or not isinstance(backup.get("destination"), str)
            or not isinstance(backup.get("age_recipient"), str)
        ):
            raise SupervisorError(
                "Source adoption requires configured protected backup storage and recipient."
            )
        backup_output = Path(backup["destination"]) / ("adoption-" + operation_id)
        protected = runtime.application(
            target,
            "offline-protect",
            {
                "version": 1,
                "proof_path": prepared["proof_path"],
                "proof_sha256": prepared["proof_sha256"],
                "output_dir": str(backup_output),
                "recipient": backup["age_recipient"],
                "installation_id": runtime.config["installation_id"],
            },
        )
        record["protected_backup"] = _verify_protected_backup(runtime, protected, backup_output)
        _publish(runtime, record, "checkpoint_ready")
        # The new app reads schema 3 only after rollback roots are durable.
        _write_content(paths["config"], record["integration"]["config"])
        runtime.config = candidate_config
        _pointer(runtime, target["release_directory"], allowed={previous["release_directory"]})
        _publish(runtime, record, "probing")
        runtime.probe(target, record, record["target_proof"])
        _publish(runtime, record, "candidate_chosen")
        _finish_candidate(runtime, record)
        return record
    except Exception:
        # Recovery selects its branch from the durable journal, including errors
        # after candidate_chosen. It never runs old code over candidate data.
        _recover(runtime)
        raise


def startup_allowed(runtime) -> bool:
    """Read-only busy-lock guard after a durable adoption admission decision."""
    journal = _journal(runtime)
    if not journal.exists() and not journal.is_symlink():
        return False
    record = _validate(runtime, _read(journal))
    phase = record["phase"]
    if phase == "committed":
        # Adoption no longer owns startup after its durable selection. A later
        # ordinary update or restore may legitimately choose another release.
        return False
    if phase not in {"previous_chosen", "rolled_back"}:
        raise SupervisorError("Source adoption has not chosen safe startup authority.")
    current = runtime.paths.current
    info = current.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
        raise SupervisorError("Adoption startup pointer is unsafe.")
    if phase in {"previous_chosen", "rolled_back"}:
        # previous_chosen is published only after the complete byte restoration
        # returns verified, before any source process is allowed to start.
        if (
            os.readlink(current) != record["previous"]["release_directory"]
            or runtime.paths.selected.exists()
            or runtime.paths.selected.is_symlink()
        ):
            raise SupervisorError("Restored source startup authority disagrees.")
        for key in ("config", "wrapper"):
            if _content(_integration_paths(runtime)[key]) != record["old"][key]:
                raise SupervisorError(
                    "Restored source launch files disagree with their sealed bytes."
                )
    return True
