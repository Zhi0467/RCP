"""Root-owned recovery identity for the simple server-backup path."""

from __future__ import annotations

import os
import stat
import subprocess
import uuid
from contextlib import suppress
from pathlib import Path

from rcp.limits import SERVER_BACKUP_CONFIGURATION_TIMEOUT_SECONDS
from rcp.server_ops._local_primitives import fsync_directory
from rcp.server_ops.config import validate_age_recipient
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout

BACKUP_IDENTITY_MODE = 0o600
BACKUP_IDENTITY_NAME = "backup-recovery.agekey"


class BackupIdentityRefused(ValueError):
    """The fixed server-managed recovery identity is missing or unsafe."""


def backup_identity_path(layout: ServerLayout = DEFAULT_SERVER_LAYOUT) -> Path:
    return layout.config_path.parent / BACKUP_IDENTITY_NAME


def resolve_backup_recipient(
    *,
    layout: ServerLayout,
    configured_recipient: str | None,
    requested_recipient: str | None,
    age_keygen_executable: str = "age-keygen",
    expected_owner: tuple[int, int] = (0, 0),
) -> str:
    """Resolve explicit compatibility input or one durable server-managed identity."""

    path = backup_identity_path(layout)
    if path.exists() or path.is_symlink():
        observed = read_backup_identity_recipient(
            path,
            age_keygen_executable=age_keygen_executable,
            expected_owner=expected_owner,
        )
        if configured_recipient is not None and configured_recipient != observed:
            raise BackupIdentityRefused(
                "The server-managed backup identity does not match the configured recipient. "
                "Restore the original root-owned identity before reconfiguring backups."
            )
        if requested_recipient is not None and requested_recipient != observed:
            raise BackupIdentityRefused(
                "The supplied recipient differs from the retained server-managed backup "
                "identity. Omit --recipient to reuse the existing identity."
            )
        return observed

    if configured_recipient is not None and requested_recipient is None:
        raise BackupIdentityRefused(
            "Backups already use an externally managed recipient, and no server-managed "
            "identity exists. Reconfigure with the same explicit --recipient."
        )
    if requested_recipient is not None:
        validated = validate_age_recipient(requested_recipient)
        if configured_recipient is not None and validated != configured_recipient:
            raise BackupIdentityRefused(
                "The supplied recipient differs from the configured external recipient. "
                "Rerun with the same --recipient; recipient rotation is not implicit."
            )
        return validated
    return create_backup_identity(
        path,
        age_keygen_executable=age_keygen_executable,
        expected_owner=expected_owner,
    )


def read_backup_identity_recipient(
    path: Path,
    *,
    age_keygen_executable: str = "age-keygen",
    expected_owner: tuple[int, int] = (0, 0),
) -> str:
    _require_identity_file(path, expected_owner=expected_owner)
    try:
        completed = subprocess.run(
            (age_keygen_executable, "-y", str(path)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=SERVER_BACKUP_CONFIGURATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupIdentityRefused(
            "RCP could not read the server-managed backup identity with age-keygen."
        ) from exc
    try:
        recipient = validate_age_recipient(completed.stdout.strip())
    except ValueError as exc:
        raise BackupIdentityRefused(
            "The server-managed backup identity is damaged or is not a native age identity."
        ) from exc
    if completed.returncode != 0:
        raise BackupIdentityRefused("The server-managed backup identity is damaged or unreadable.")
    _require_identity_file(path, expected_owner=expected_owner)
    return recipient


def create_backup_identity(
    path: Path,
    *,
    age_keygen_executable: str = "age-keygen",
    expected_owner: tuple[int, int] = (0, 0),
) -> str:
    """Create one non-overwriting identity and return only its public recipient."""

    _require_identity_parent(path.parent, expected_uid=expected_owner[0])
    if path.exists() or path.is_symlink():
        raise BackupIdentityRefused(
            "The server-managed backup identity appeared during configuration; rerun the "
            "same command so RCP can validate and reuse it."
        )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    descriptor = -1
    try:
        completed = subprocess.run(
            (age_keygen_executable, "--output", str(temporary)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=SERVER_BACKUP_CONFIGURATION_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise BackupIdentityRefused(
                "age-keygen could not create the server-managed backup identity."
            )
        descriptor = os.open(temporary, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BackupIdentityRefused("age-keygen did not create one regular identity file.")
        os.fchown(descriptor, *expected_owner)
        os.fchmod(descriptor, BACKUP_IDENTITY_MODE)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        recipient = read_backup_identity_recipient(
            temporary,
            age_keygen_executable=age_keygen_executable,
            expected_owner=expected_owner,
        )
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise BackupIdentityRefused(
                "The server-managed backup identity appeared during configuration; rerun the "
                "same command so RCP can validate and reuse it."
            ) from exc
        temporary.unlink()
        fsync_directory(path.parent)
        observed = read_backup_identity_recipient(
            path,
            age_keygen_executable=age_keygen_executable,
            expected_owner=expected_owner,
        )
        if observed != recipient:  # pragma: no cover - immutable root directory boundary
            raise BackupIdentityRefused(
                "The published server-managed backup identity changed during readback."
            )
        return observed
    except BackupIdentityRefused:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupIdentityRefused(
            "RCP could not safely create the root-owned backup identity."
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink()


def _require_identity_parent(path: Path, *, expected_uid: int) -> None:
    if path.is_symlink():
        raise BackupIdentityRefused(
            "The server configuration directory must not be a symbolic link."
        )
    try:
        info = path.stat()
    except OSError as exc:
        raise BackupIdentityRefused(
            "The server configuration directory is missing or unreadable."
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != expected_uid
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise BackupIdentityRefused(
            "The server configuration directory has unsafe ownership or permissions."
        )


def _require_identity_file(path: Path, *, expected_owner: tuple[int, int]) -> None:
    if path.is_symlink():
        raise BackupIdentityRefused(
            "The server-managed backup identity must not be a symbolic link."
        )
    try:
        info = path.stat()
    except OSError as exc:
        raise BackupIdentityRefused(
            "The server-managed backup identity is missing or unreadable."
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_uid, info.st_gid) != expected_owner
        or stat.S_IMODE(info.st_mode) != BACKUP_IDENTITY_MODE
    ):
        raise BackupIdentityRefused(
            "The server-managed backup identity must be one root-owned mode-0600 file."
        )


__all__ = [
    "BACKUP_IDENTITY_MODE",
    "BackupIdentityRefused",
    "backup_identity_path",
    "create_backup_identity",
    "read_backup_identity_recipient",
    "resolve_backup_recipient",
]
