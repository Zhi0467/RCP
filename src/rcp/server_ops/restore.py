"""Crash-safe construction of one offline team-server restore candidate."""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import stat
import tarfile
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rcp.limits import (
    BACKUP_COPY_BUFFER_BYTES,
    BACKUP_RECEIPT_MAX_BYTES,
    MEMBER_REMOVAL_PREVIEW_MAX_ITEMS,
)
from rcp.server_ops._local_primitives import (
    canonical_json_bytes,
)
from rcp.server_ops._local_primitives import (
    fsync_file_tree as _fsync_tree,
)
from rcp.server_ops._local_primitives import (
    normalized_absolute_non_root_path as _absolute,
)
from rcp.server_ops.backup_integrity import (
    canonical_backup_manifest_bytes,
    database_schema_sha256,
)
from rcp.server_ops.backup_models import BackupArchiveManifest
from rcp.storage import AppStore

RESTORE_DIRECTORY_MODE = 0o700

# Every schema era accepted by the immutable server-upgrade harness, plus the
# current schema. A later schema change must deliberately extend this set before
# that release can restore its own archives.
SUPPORTED_RESTORE_DATABASE_SCHEMAS = frozenset(
    {
        "91b15bf1f86acba1a9e29d3ad2d222a568fc3ea1781bb09ade823f2d990ca9f0",
        "fc86f6048a4696bdab5b06771271a3651e7972432ad6f5b5ceefc3be40794426",
        "fb854eddda342b3d83507c3c8dd344cc635306b6a82b022382ed5c0dbdbf6f52",
        "bd813aa0f7161aa398433459f622c4d4d156e4a14c201d1b1cb3d1088a57b70f",
        "67ec363b621d9d8e3d0855c719d511e32b6d82733735cbb996c13b284d57105c",
        "742cd73108cb6cc8e23122ad8a1c18444c28ce572b3c2e16d4673365cec2f8a4",
        "ad6edcdd78df9b32ad11f6ff14e2f51ee68034f27f2a46dfe3840fb0a64926c1",
        "5b06cd734c8fd7cbd016e425955751407e61fa21b864a0a81d6585ca219e314e",
        "7e6988b524c0b3fdda1873ab5da4bd03664dd1ac9de1447b42fb2d74858b23ef",
        "87150f2308f7db67da7a3ceb3879c475830c560096b836df92df6a6fd43798fb",
        "3abf5220f328382d9abebe6091fd8f73daa634fb1e4bf1ed8993fc2b58f6005b",
        "2e5ba0c68eccff397619edb4f3bf8574b0002d89879a8095acfa001474da23f4",
        "d7530fb1961b8c0d002bc39b92b354e7d3f34681845beeef15caa62b5713132a",
        "46e5a2762be47bff427ae3e240dd61e7c8048c0c24f78c14e5595d749765a4b7",
        "198c440b01783e09952742b56efb2b1e4987e405e88e7a59707ab5896f60af69",
        "0e76145bb3316c6b193792982f894b14c7c2f25ff85b2fa91f7e2d6cb6db8859",
        "a69f2c7990077b1ea150e6977f370bd5614e9711527662772c55acced0f98cf0",
        "192554f3171d2758042cafe337c0c76ca67496f72a6107b5fbd5aa6bebf54e63",
        "f79e61850320ad658571ea91fa543b8b36ac1c612446431d7848ff6f10c27aa8",
        "35a89ceb4cd6021dfd343f576d370ea5b3aae4237ffa22bd36821c532d1956ed",
        "c8d2b852aa73295487343b38e7b5ac94f1c1317d3fc171fb770586c7f4a537bb",
        "94243dd4e23f4dd8d59da0e45aa43f83e6499626e8f73a7b6f5a59ed6fe8870c",
        "b71de34f4629d22c4c84e961bfce8bf31519a2240f1893c636eb639d0edfaaa7",
        "3aab58e7f38d85951601b14caf4e1d155f8c3804f65c80371bc1cd24150bbf3a",
        "b926b4089c85f4e00626a656332ad895fe4ca24508efccd80c5194b2a9c75a7c",
    }
)

_SHA256 = frozenset("0123456789abcdef")
_FULL_COMMIT = frozenset("0123456789abcdef")
_RESTORE_ALIAS = re.compile(r"[a-z][a-z0-9-]{0,47}")
_OPENSSH_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


class RestoreRefused(RuntimeError):
    """A restore stopped without making ambiguous target state serveable."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _digest(value: str, *, label: str) -> str:
    if len(value) != 64 or any(character not in _SHA256 for character in value):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


class RestoreRepositoryRecovery(_StrictModel):
    project_id: str
    repository_alias: str
    machine_alias: str
    state: Literal["key_started", "key_ready", "checkout_ready"]
    deploy_key_label: str | None = None
    deploy_public_key: str | None = None
    public_key_fingerprint: str | None = None
    probed_commit: str | None = None
    central_root: str | None = None
    repository_path: str | None = None
    checkout_disposition: Literal["request_created", "reused_existing"] | None = None
    checkout_commit: str | None = None

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as exc:
            raise ValueError("restore repository project id must be a canonical UUID4") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("restore repository project id must be a canonical UUID4")
        return value

    @field_validator("repository_alias", "machine_alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        if _RESTORE_ALIAS.fullmatch(value) is None:
            raise ValueError("restore repository alias is invalid")
        return value

    @field_validator("deploy_key_label", "deploy_public_key")
    @classmethod
    def validate_key_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        if (
            not value
            or len(value) > 16 * 1024
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"restore {info.field_name.replace('_', ' ')} is invalid")
        return value

    @field_validator("public_key_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str | None) -> str | None:
        if value is not None and _OPENSSH_FINGERPRINT.fullmatch(value) is None:
            raise ValueError("restore public-key fingerprint is invalid")
        return value

    @field_validator("probed_commit", "checkout_commit")
    @classmethod
    def validate_commit(cls, value: str | None, info) -> str | None:
        if value is not None and (
            len(value) != 40 or any(character not in _FULL_COMMIT for character in value)
        ):
            raise ValueError(f"restore {info.field_name.replace('_', ' ')} is invalid")
        return value

    @field_validator("central_root", "repository_path")
    @classmethod
    def validate_optional_path(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else _absolute(value, label=f"restore {info.field_name.replace('_', ' ')}")
        )

    @model_validator(mode="after")
    def validate_progress(self) -> RestoreRepositoryRecovery:
        key_fields = (
            self.deploy_key_label,
            self.deploy_public_key,
            self.public_key_fingerprint,
        )
        checkout_fields = (
            self.central_root,
            self.repository_path,
            self.checkout_disposition,
            self.checkout_commit,
        )
        if self.state == "key_started" and any((*key_fields, self.probed_commit, *checkout_fields)):
            raise ValueError("a restore key-start receipt cannot claim later effects")
        if self.state in {"key_ready", "checkout_ready"} and not all(key_fields):
            raise ValueError("a restore key-ready receipt requires its complete public proof")
        if self.state == "key_ready" and any(checkout_fields):
            raise ValueError("a restore key-ready receipt cannot claim a checkout")
        if self.state == "checkout_ready" and (
            self.probed_commit is None or not all(checkout_fields)
        ):
            raise ValueError("a restore checkout receipt requires every checkout proof")
        if self.checkout_commit is not None and self.checkout_commit != self.probed_commit:
            raise ValueError("restore checkout and write-probe commits differ")
        return self


RestoreOldAuthorityDisposition = Literal[
    "old-machine-destroyed", "old-machine-fenced-and-credentials-revoked"
]


class RestoreMemberRosterEntry(_StrictModel):
    member_id: str
    display_name: str | None = Field(default=None, max_length=240)
    active_token_ids: tuple[str, ...] = Field(max_length=MEMBER_REMOVAL_PREVIEW_MAX_ITEMS)

    @field_validator("member_id")
    @classmethod
    def validate_member_id(cls, value: str) -> str:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("restore member id must be a canonical UUID4")
        return value

    @field_validator("active_token_ids")
    @classmethod
    def validate_token_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("restore member authority requires sorted active token ids")
        for token_id in value:
            parsed = uuid.UUID(token_id)
            if parsed.version != 4 or str(parsed) != token_id:
                raise ValueError("restore token id must be a canonical UUID4")
        return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def restore_old_authority_boundary(manifest: BackupArchiveManifest) -> str:
    repositories = []
    routes = []
    for project in sorted(manifest.projects, key=lambda item: item.project_id):
        if project.status != "captured" or project.recovery is None:
            continue
        repositories.extend(
            {
                "project_id": project.project_id,
                "alias": repository.alias,
                "deploy_key_label": repository.deploy_key_label,
                "public_key_fingerprint": repository.public_key_fingerprint,
            }
            for repository in sorted(project.recovery.repositories, key=lambda item: item.alias)
        )
        routes.extend(
            {
                "project_id": project.project_id,
                "alias": machine.alias,
                "host": machine.host,
                "os_account": machine.os_account,
            }
            for machine in sorted(project.recovery.machines, key=lambda item: item.alias)
        )
    return _canonical_sha256(
        {
            "captured_at": manifest.captured_at.isoformat(),
            "installation_id": manifest.installation_id,
            "source_deploy_key_label": manifest.source_deploy_key_label,
            "source_public_key_fingerprint": manifest.source_public_key_fingerprint,
            "repositories": repositories,
            "remote_routes": routes,
        }
    )


def restore_member_roster_boundary(
    captured_at: datetime,
    members: tuple[RestoreMemberRosterEntry, ...],
) -> str:
    return _canonical_sha256(
        {
            "archive_captured_at": captured_at.isoformat(),
            "members": [item.model_dump(mode="json") for item in members],
        }
    )


def detach_restore_database(
    database: Path,
    *,
    confirmed_by: str,
    detached_at: datetime,
) -> None:
    store = AppStore(database)
    store.detach_restored_lifecycle(
        diagnostic=(
            "This operation was captured by a replacement-server archive and cannot resume on "
            "the restored machine."
        ),
        confirmed_by=confirmed_by,
        detached_at=detached_at.isoformat(),
    )
    _verify_sqlite_integrity(database)


class _RestoreArchiveStream(io.BufferedReader):
    def read(self, size: int = -1) -> bytes:
        # tarfile otherwise allocates one unbounded PAX/long-name header before
        # the application can validate its declared regular-file inventory.
        if size < 0 or size > BACKUP_RECEIPT_MAX_BYTES:
            raise RestoreRefused("Restore archive metadata exceeds its read bound.")
        return super().read(size)


def _extract_verified_archive(
    plaintext: Path,
    payload_root: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[BackupArchiveManifest, str]:
    payload_root.mkdir(mode=RESTORE_DIRECTORY_MODE)
    os.chown(payload_root, expected_uid, expected_gid)
    try:
        with (
            _RestoreArchiveStream(io.FileIO(plaintext, "rb")) as stream,
            tarfile.open(fileobj=stream, mode="r:") as archive,
        ):
            manifest_member = archive.next()
            if manifest_member is None or manifest_member.name != "manifest.json":
                raise RestoreRefused("The restore archive does not begin with its manifest.")
            _require_regular_archive_member(manifest_member)
            if manifest_member.size > BACKUP_RECEIPT_MAX_BYTES:
                raise RestoreRefused("The restore archive manifest exceeds its size bound.")
            manifest_stream = archive.extractfile(manifest_member)
            if manifest_stream is None:
                raise RestoreRefused("The restore archive manifest cannot be read.")
            manifest_bytes = manifest_stream.read(BACKUP_RECEIPT_MAX_BYTES + 1)
            try:
                manifest = BackupArchiveManifest.model_validate_json(manifest_bytes)
            except ValueError as exc:
                raise RestoreRefused(
                    "The restore archive manifest is invalid or unsupported."
                ) from exc
            canonical_manifest = canonical_backup_manifest_bytes(manifest)
            if manifest_bytes != canonical_manifest:
                raise RestoreRefused("The restore archive manifest is not canonical.")
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            entries = {
                entry.archive_path: entry
                for entry in (
                    manifest.sqlite_snapshot,
                    *(item for project in manifest.projects for item in project.files),
                    *(item for capture in manifest.imported_sources for item in capture.files),
                )
            }
            seen: set[str] = set()
            while (member := archive.next()) is not None:
                _require_regular_archive_member(member)
                entry = entries.get(member.name)
                if entry is None or member.name in seen or member.size != entry.size_bytes:
                    raise RestoreRefused(
                        "The restore archive contains an undeclared, repeated, or resized member."
                    )
                destination = payload_root.joinpath(*PurePosixPath(member.name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=RESTORE_DIRECTORY_MODE)
                _chown_tree_parents(destination.parent, payload_root, expected_uid, expected_gid)
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o400,
                )
                digest = hashlib.sha256()
                size = 0
                source = archive.extractfile(member)
                if source is None:
                    os.close(descriptor)
                    raise RestoreRefused("A declared restore archive member cannot be read.")
                try:
                    while True:
                        chunk = source.read(BACKUP_COPY_BUFFER_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("short restore member write")
                            view = view[written:]
                    os.fchown(descriptor, expected_uid, expected_gid)
                    os.fchmod(descriptor, 0o400)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if size != entry.size_bytes or digest.hexdigest() != entry.sha256:
                    raise RestoreRefused(
                        "A restore archive member does not match its declared hash."
                    )
                seen.add(member.name)
            if seen != set(entries):
                raise RestoreRefused("The restore archive omits declared files.")
    except RestoreRefused:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise RestoreRefused("The decrypted restore archive is unsafe or unreadable.") from exc
    _fsync_tree(payload_root)
    return manifest, manifest_sha256


def _require_regular_archive_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if (
        not member.isreg()
        or path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or member.mode != 0o400
        or member.uid != 0
        or member.gid != 0
        or member.mtime != 0
    ):
        raise RestoreRefused("The restore archive contains an unsafe or unsupported member.")


def _database_schema_sha256(path: Path) -> str:
    uri = f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        return database_schema_sha256(connection)


def _verify_sqlite_integrity(path: Path) -> None:
    uri = f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RestoreRefused("The restored SQLite candidate failed integrity_check.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RestoreRefused("The restored SQLite candidate failed foreign_key_check.")


def _hash_regular_file(path: Path, *, expected_uid: int | None = None) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (
            expected_uid is not None and before.st_uid != expected_uid
        ):
            raise RestoreRefused("A restore input is not one owned regular file.")
        while True:
            chunk = os.read(descriptor, BACKUP_COPY_BUFFER_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RestoreRefused("A restore input changed while it was being verified.")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _chown_tree_parents(path: Path, root: Path, uid: int, gid: int) -> None:
    current = path
    while current != root:
        os.chown(current, uid, gid)
        os.chmod(current, RESTORE_DIRECTORY_MODE)
        current = current.parent


class RestorePrepareRequest(_StrictModel):
    version: Literal[1]
    data_dir: str
    output_dir: str
    plaintext_path: str
    plaintext_sha256: str
    recipient_fingerprint: str
    source_commit: str
    preparation_id: str
    detached_at: datetime
    confirmed_data_dir: str
    confirmed_by: str = Field(min_length=1, max_length=400)
    old_authority_disposition: RestoreOldAuthorityDisposition | None = None
    confirm_old_authority: str | None = None
    confirm_member_roster: str | None = None
    remove_stale_member: str | None = None
    progress: tuple[RestoreRepositoryRecovery, ...] = ()
    resume_argv: tuple[str, ...] = ()
    previous_roots: tuple[dict[str, str], ...] = ()

    @field_validator("data_dir", "output_dir", "plaintext_path", "confirmed_data_dir")
    @classmethod
    def path(cls, value: str) -> str:
        return _absolute(value, label="restore preparation path")

    @field_validator("plaintext_sha256", "recipient_fingerprint")
    @classmethod
    def digest(cls, value: str) -> str:
        return _digest(value, label="restore preparation digest")

    @field_validator("source_commit")
    @classmethod
    def commit(cls, value: str) -> str:
        if len(value) != 40 or any(c not in _FULL_COMMIT for c in value):
            raise ValueError("restore requires a full selected commit")
        return value

    @field_validator("preparation_id")
    @classmethod
    def identity(cls, value: str) -> str:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("restore preparation identity must be canonical UUID4")
        return value

    @field_validator("progress")
    @classmethod
    def unique_progress(cls, value: tuple[RestoreRepositoryRecovery, ...]):
        keys = [(item.project_id, item.repository_alias) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("restore progress repeats a repository identity")
        return value

    @field_validator("previous_roots")
    @classmethod
    def previous_root_paths(cls, value: tuple[dict[str, str], ...]):
        live = []
        for root in value:
            if root.keys() != {"live", "payload"}:
                raise ValueError("restore previous root fields are invalid")
            for path in root.values():
                _absolute(path, label="previous root")
            live.append(root["live"])
        if len(live) != len(set(live)):
            raise ValueError("restore previous roots repeat a live path")
        return value

    @field_validator("detached_at")
    @classmethod
    def timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("restore detachment time requires a timezone")
        return value


def _operator_reply(
    request, manifest, members, *, message, progress=None, actions=None, extra_fields=()
):
    import json

    from rcp.server_ops.models import (
        SERVER_CLI_MAX_FIELD_CHARS,
        SERVER_CLI_MAX_FIELDS,
        NonsecretField,
    )

    values = {
        "old_authority_boundary": restore_old_authority_boundary(manifest),
        "member_roster_boundary": restore_member_roster_boundary(manifest.captured_at, members),
        "archive_captured_at": manifest.captured_at.isoformat(),
    }
    fields = [NonsecretField(name=name, value=value).model_dump() for name, value in values.items()]
    inventories = {
        "source_authority": {
            "deploy_key_label": manifest.source_deploy_key_label,
            "public_key_fingerprint": manifest.source_public_key_fingerprint,
        },
        "repositories": [
            r.model_dump(mode="json")
            for p in manifest.projects
            if p.recovery
            for r in p.recovery.repositories
        ],
        "machines": [
            m.model_dump(mode="json")
            for p in manifest.projects
            if p.recovery
            for m in p.recovery.machines
        ],
        "members": [m.model_dump(mode="json") for m in members],
    }
    for name, inventory in inventories.items():
        text = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
        for index, start in enumerate(range(0, len(text), SERVER_CLI_MAX_FIELD_CHARS), 1):
            fields.append(
                NonsecretField(
                    name=f"{name}_{index:02}",
                    value=text[start : start + SERVER_CLI_MAX_FIELD_CHARS],
                ).model_dump()
            )
    fields.extend(field.model_dump() for field in extra_fields)
    if len(fields) > SERVER_CLI_MAX_FIELDS:
        raise RestoreRefused("Archived authority exceeds the bounded operator review surface.")
    return {
        "version": 1,
        "status": "operator_action_needed",
        "diagnostic": message,
        "progress": [
            item.model_dump(mode="json")
            for item in (progress if progress is not None else request.progress)
        ],
        "actions": actions
        or [
            {
                "kind": "external",
                "instruction": "Either permanently destroy the archived server, or fence it and revoke every archived source/repository deploy key, SSH grant, and provider login. Review retained members and remove known stale members before confirming both exact boundaries.",
            }
        ],
        "fields": fields,
    }


def _recovery_machine(archived):
    from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
    from rcp.storage import ProjectProvisioningMachineIntent

    if archived.location == "local":
        if (
            archived.os_account != DEFAULT_SERVER_LAYOUT.service_account
            or archived.resolved_central_root != str(DEFAULT_SERVER_LAYOUT.projects_root)
        ):
            raise RestoreRefused("Archived local checkout target differs from this installation.")
        return ProjectProvisioningMachineIntent.model_construct(
            alias=archived.alias,
            location="local",
            host="",
            os_account=archived.os_account,
            central_root=archived.resolved_central_root,
        )
    return ProjectProvisioningMachineIntent(
        alias=archived.alias,
        location="ssh",
        host=archived.host,
        os_account=archived.os_account,
        central_root=archived.resolved_central_root,
    )


def _recover_repositories(request, manifest, previous_store, members):
    from rcp.projects import inspect_backup_project_registration
    from rcp.server_ops.backup_checkout import verify_checkout_identities
    from rcp.server_ops.git_credentials import (
        GitCredentialManager,
        restore_deploy_key_operator_step,
    )
    from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT
    from rcp.server_ops.project_checkout import ProjectCheckoutManager

    credentials = GitCredentialManager(DEFAULT_SERVER_LAYOUT)
    checkouts = ProjectCheckoutManager(DEFAULT_SERVER_LAYOUT)
    progress = list(request.progress)
    for capture in manifest.projects:
        if capture.status != "captured":
            continue
        recovery = capture.recovery
        assert recovery is not None
        existing = None if previous_store is None else previous_store.project(capture.project_id)
        if existing is not None:
            registration = inspect_backup_project_registration(
                existing,
                data_dir=Path(request.data_dir),
                provisioning_requests=previous_store.completed_project_provisioning_requests(
                    capture.project_id
                ),
            )
            if registration.recovery != recovery:
                raise RestoreRefused(
                    "Existing project checkout authority differs from the protected archive."
                )
            verify_checkout_identities(registration.recovery)
            # The current typed provisioning owner already proves these exact
            # checkouts, host/account routes, and credentials. No Git mutation.
            continue
        machines = {m.alias: m for m in recovery.machines}
        for repository in recovery.repositories:
            machine = _recovery_machine(machines[repository.machine_alias])
            saved = next(
                (
                    p
                    for p in progress
                    if p.project_id == capture.project_id and p.repository_alias == repository.alias
                ),
                None,
            )
            if saved is None:
                credentials.preflight_recovery_key(
                    machine,
                    space_id=manifest.space_id,
                    project_id=capture.project_id,
                    repository_alias=repository.alias,
                )
                progress.append(
                    RestoreRepositoryRecovery(
                        project_id=capture.project_id,
                        repository_alias=repository.alias,
                        machine_alias=repository.machine_alias,
                        state="key_started",
                    )
                )
                return {
                    "version": 1,
                    "status": "continue",
                    "progress": [p.model_dump(mode="json") for p in progress],
                }
            material = credentials.prepare_recovery_key(
                machine,
                repository.repository,
                space_id=manifest.space_id,
                project_id=capture.project_id,
                repository_alias=repository.alias,
            )
            if material.public_key_fingerprint == repository.public_key_fingerprint:
                raise RestoreRefused("Fresh checkout reused archived deploy-key authority.")
            if saved.state == "key_started":
                progress[progress.index(saved)] = saved.model_copy(
                    update={
                        "state": "key_ready",
                        "deploy_key_label": material.label,
                        "deploy_public_key": material.public_key,
                        "public_key_fingerprint": material.public_key_fingerprint,
                    }
                )
                return {
                    "version": 1,
                    "status": "continue",
                    "progress": [p.model_dump(mode="json") for p in progress],
                }
            if (saved.deploy_key_label, saved.deploy_public_key, saved.public_key_fingerprint) != (
                material.label,
                material.public_key,
                material.public_key_fingerprint,
            ):
                raise RestoreRefused("Fresh deploy-key identity changed after its saved proof.")
            if saved.state == "checkout_ready":
                from types import SimpleNamespace

                probe = SimpleNamespace(ready=True, commit=saved.probed_commit)
            else:
                probe = credentials.probe_write(
                    machine, material, request_id=request.preparation_id
                )
            if not probe.ready:
                if probe.status in {"cleanup_failed", "temporary_ref_conflict"}:
                    temporary_ref = probe.temporary_ref or "the request-scoped restore probe ref"
                    return _operator_reply(
                        request,
                        manifest,
                        members,
                        progress=progress,
                        message=probe.diagnostic,
                        actions=[
                            {
                                "kind": "external",
                                "instruction": f"Inspect {temporary_ref!r} in {repository.repository.identity}. Remove it only after proving it belongs to this restore write probe; otherwise preserve it and investigate the ownership conflict.",
                            }
                        ],
                    )
                if probe.status not in {"github_host_trust_needed", "github_grant_needed"}:
                    raise RestoreRefused(probe.diagnostic)
                step = restore_deploy_key_operator_step(
                    credentials, machine, material, number=1, resume_argv=request.resume_argv
                )
                return _operator_reply(
                    request,
                    manifest,
                    members,
                    progress=progress,
                    message=probe.diagnostic,
                    actions=[a.model_dump(mode="json") for a in step.actions],
                    extra_fields=step.fields,
                )
            archived = (
                {
                    e.source_relative_path: (e.sha256, e.size_bytes)
                    for e in capture.files
                    if e.source_relative_path.startswith(".research/")
                }
                if repository.alias == recovery.configuration.state_repository
                else {}
            )
            result = checkouts.prepare_recovery(
                machine,
                material,
                project_id=capture.project_id,
                repository_alias=repository.alias,
                state_repository=repository.alias == recovery.configuration.state_repository,
                expected_head=probe.commit,
                retained_provisioning_commit=repository.git_commit,
                archived_research=archived,
            )
            if (
                result.repository_path != repository.resolved_path
                or result.central_root != machines[repository.machine_alias].resolved_central_root
                or result.commit != probe.commit
            ):
                raise RestoreRefused("Reconstructed checkout differs from the archived target.")
            progress[progress.index(saved)] = saved.model_copy(
                update={
                    "state": "checkout_ready",
                    "probed_commit": probe.commit,
                    "central_root": result.central_root,
                    "repository_path": result.repository_path,
                    "checkout_disposition": result.checkout_disposition,
                    "checkout_commit": result.commit,
                }
            )
            if saved.state != "checkout_ready":
                return {
                    "version": 1,
                    "status": "continue",
                    "progress": [p.model_dump(mode="json") for p in progress],
                }
    return None


def _restore_project_bytes(owners, capture, payload, operation_projects, *, existing_remote):
    entries = capture.files
    if existing_remote:
        owners.workspace.refresh()
        _verify_project_publication_bytes(owners.workspace, entries)
        return owners.history.materialize(write_outputs=False)
    canonical = {
        e.source_relative_path.removeprefix(".research/"): (
            payload / e.archive_path,
            e.sha256,
            e.size_bytes,
        )
        for e in entries
        if e.group == "canonical"
    }
    materialization = owners.history.restore_canonical_history(
        canonical, expected_main_head=capture.main_head, expected_branch_heads=capture.branch_heads
    )
    for entry in entries:
        source = payload / entry.archive_path
        arguments = {"expected_sha256": entry.sha256, "expected_size": entry.size_bytes}
        if entry.group == "chat":
            owners.service.restore_canonical_chat(
                source, operation_projects=operation_projects, **arguments
            )
        elif entry.group == "paper_introduction":
            owners.paper.restore_canonical(source, **arguments)
        elif entry.group == "fact":
            owners.workspace.restore_exact_file(
                entry.source_relative_path.removeprefix(".research/"), source, **arguments
            )
        elif entry.group == "kept_artifact":
            owners.workspace.restore_kept_artifact(
                PurePosixPath(entry.source_relative_path).name, source, **arguments
            )
        elif entry.group == "legacy_kept_result_view":
            owners.workspace.restore_kept_result_view(
                PurePosixPath(entry.source_relative_path).name, source, **arguments
            )
    if owners.workspace.remote:
        owners.workspace.refresh()
    _verify_project_publication_bytes(owners.workspace, entries)
    return materialization


def _verify_project_publication_bytes(workspace, entries) -> None:
    for entry in entries:
        relative = PurePosixPath(entry.source_relative_path)
        if entry.group in {"kept_artifact", "legacy_kept_result_view"}:
            if entry.group == "kept_artifact":
                data = workspace.read_kept_artifact(
                    relative.name,
                    max_bytes=max(1, entry.size_bytes),
                )
            else:
                data = workspace.read_kept_result_view(
                    relative.name,
                    max_bytes=max(1, entry.size_bytes),
                )
            observed = (hashlib.sha256(data).hexdigest(), len(data))
        else:
            if relative.parts[0] != ".research":
                raise RestoreRefused("A canonical project byte has an invalid restore path.")
            destination = workspace.root.joinpath(*relative.parts[1:])
            observed = _hash_regular_file(destination)
        if observed != (entry.sha256, entry.size_bytes):
            raise RestoreRefused(
                "A restored project file changed before final publication readback."
            )


def _preserve_unrestored_files(source: Path, destination: Path) -> None:
    """Keep repository files which the archive does not claim to replace."""
    from rcp.server_ops.application_snapshot import _copy_declared_file

    for root, directories, files in os.walk(source, followlinks=False):
        relative = Path(root).relative_to(source)
        target = destination / relative
        target.mkdir(mode=0o700, exist_ok=True)
        for name in directories:
            child = Path(root) / name
            if not stat.S_ISDIR(child.lstat().st_mode):
                raise RestoreRefused("Retained repository output has an unsafe directory.")
        for name in files:
            old = Path(root) / name
            restored = target / name
            if os.path.lexists(restored):
                continue
            digest, size = _hash_regular_file(old, expected_uid=os.geteuid())
            _copy_declared_file(
                old,
                restored,
                relative_path=name,
                expected_sha256=digest,
                expected_size=size,
                restore_mode=stat.S_IMODE(old.stat().st_mode),
            )


def prepare_restore(request: RestorePrepareRequest) -> dict[str, object]:
    import shutil
    from datetime import UTC

    from rcp.__main__ import instance_lock
    from rcp.projects import (
        complete_restored_project_publication,
        rebind_restored_project_registration,
        restored_project_owners,
    )
    from rcp.server_ops.application_snapshot import _set_private_directory_modes, _snapshot_tree
    from rcp.server_ops.backup_capture import (
        BackupSnapshotProjectInventory,
        BackupSQLiteCaptureReceipt,
        write_immutable_backup_receipt,
    )
    from rcp.server_ops.backup_models import BackupFileEntry, inspect_app_data_capture_plan
    from rcp.server_ops.backup_project_files import BackupProjectFileCaptureReceipt
    from rcp.server_ops.deployment import (
        ApplicationProof,
        _check_copy,
        _new_output,
        _private_ancestors,
        _publish_proof,
    )
    from rcp.sources.imported import ImportedProviderSourceInventory, ImportedProviderSourceStore

    data, output, plaintext = (
        Path(request.data_dir),
        Path(request.output_dir),
        Path(request.plaintext_path),
    )
    if request.confirmed_data_dir != str(data):
        raise RestoreRefused("Confirm the exact restore data directory before preparation.")
    _private_ancestors(data)
    _private_ancestors(plaintext.parent)
    source_info = plaintext.lstat()
    if (
        source_info.st_uid not in {0, os.geteuid()}
        or source_info.st_mode & 0o022
        or source_info.st_nlink != 1
    ):
        raise RestoreRefused("Decrypted archive has unsafe ownership or metadata.")
    if _hash_regular_file(plaintext)[0] != request.plaintext_sha256:
        raise RestoreRefused("Decrypted protected archive digest changed.")
    if output == data or output.is_relative_to(data) or data.is_relative_to(output):
        raise RestoreRefused("Restore preparation overlaps live application state.")
    _new_output(output)
    payload = output / "archive"
    manifest, _ = _extract_verified_archive(
        plaintext, payload, expected_uid=os.geteuid(), expected_gid=os.getegid()
    )
    if manifest.encryption_recipient_fingerprint != request.recipient_fingerprint:
        raise RestoreRefused("Archive encryption recipient differs from the recovery identity.")
    repository_identities = {
        (project.project_id, repository.alias): repository.machine_alias
        for project in manifest.projects
        if project.recovery is not None
        for repository in project.recovery.repositories
    }
    if any(
        repository_identities.get((item.project_id, item.repository_alias)) != item.machine_alias
        for item in request.progress
    ):
        raise RestoreRefused(
            "Saved restore progress does not match the archived repository inventory."
        )
    archived_database = payload / manifest.sqlite_snapshot.archive_path
    _verify_sqlite_integrity(archived_database)
    if (
        _database_schema_sha256(archived_database) != manifest.database_schema_sha256
        or manifest.database_schema_sha256 not in SUPPORTED_RESTORE_DATABASE_SCHEMAS
    ):
        raise RestoreRefused("Protected archive SQLite schema is unsupported or changed.")
    app = output / "app-data"
    app.mkdir(mode=0o700)
    database = app / "rcp.sqlite3"
    shutil.copyfile(archived_database, database)
    database.chmod(0o600)
    detach_restore_database(
        database, confirmed_by=request.confirmed_by, detached_at=request.detached_at
    )
    store = AppStore(database)
    if (
        store.space_kind != "team"
        or store.space_id != manifest.space_id
        or store.space_name != manifest.space_name
    ):
        raise RestoreRefused("Archived SQLite identity differs from its protected manifest.")
    if request.remove_stale_member:
        preview = store.member_removal_preview(request.remove_stale_member)
        store.begin_member_removal(
            request.remove_stale_member, expected_boundary_sha256=preview.boundary_sha256
        )
        store.complete_member_removal(request.remove_stale_member)
    members = tuple(
        RestoreMemberRosterEntry(
            member_id=m.member_id, display_name=m.display_name, active_token_ids=m.active_token_ids
        )
        for m in store.active_team_member_authority()
    )
    if not members:
        raise RestoreRefused("Restored team must retain an active authenticating member.")
    authority = restore_old_authority_boundary(manifest)
    roster = restore_member_roster_boundary(manifest.captured_at, members)
    if request.confirm_old_authority is not None and request.confirm_old_authority != authority:
        raise RestoreRefused("Archived authority changed after confirmation.")
    if request.confirm_member_roster is not None and request.confirm_member_roster != roster:
        raise RestoreRefused("Retained member authority changed after confirmation.")
    if (
        request.old_authority_disposition is None
        or request.confirm_old_authority is None
        or request.confirm_member_roster is None
    ):
        return _operator_reply(
            request,
            manifest,
            members,
            message="Review the archived authority and retained member/token inventory, then confirm both exact boundaries.",
        )
    with instance_lock(data, timeout=0):
        from rcp.server_ops.deployment import InspectRequest, inspect

        previous = (
            AppStore.open_read_only_snapshot(data / "rcp.sqlite3")
            if inspect(InspectRequest(version=1, data_dir=str(data)))["status"]
            == "initialized_team"
            else None
        )
        progress_reply = _recover_repositories(request, manifest, previous, members)
        if progress_reply is not None:
            return progress_reply
        for capture in manifest.imported_sources:
            if capture.present:
                expected = ImportedProviderSourceInventory.model_validate(
                    capture.inventory.model_dump()
                )
                ImportedProviderSourceStore(app, capture.project_id).publish_snapshot(
                    payload / "project-sources" / capture.project_id / "provider-history", expected
                )
        operation_projects = {
            task.operation_id: task.project_id
            for p in manifest.projects
            for task in store.all_project_agent_tasks(p.project_id)
        }
        roots = [{"live": str(data), "payload": str(app)}]
        extra_previous = []
        known = {r["live"] for r in request.previous_roots}
        for capture in manifest.projects:
            if capture.status != "captured":
                store.mark_uncaptured_project_unavailable_for_restore(
                    capture.project_id,
                    diagnostic=capture.unavailable_reason or "project capture failed",
                )
                continue
            recovery = capture.recovery
            assert recovery is not None
            repository_paths = {r.alias: r.resolved_path for r in recovery.repositories}
            record = rebind_restored_project_registration(
                store,
                capture,
                repository_paths=repository_paths,
                data_dir=app,
                uid=os.geteuid(),
                gid=os.getegid(),
            )
            if record.state_remote:
                # Bootstrap bytes are staged under app-data, but stored locator
                # must name their eventual selected live location.
                new_locator = str(data / Path(record.locator).relative_to(app))
                workspace_root = None
            else:
                workspace_root = output / "projects" / capture.project_id / ".research"
                workspace_root.parent.mkdir(parents=True, mode=0o700)
                workspace_root.mkdir(mode=0o700)
            archived_manifest = next(
                e
                for e in capture.files
                if e.group == "canonical" and e.source_relative_path == ".research/manifest.toml"
            )
            owners = restored_project_owners(
                store,
                capture,
                archived_manifest=payload / archived_manifest.archive_path,
                data_dir=app,
                local_home=Path.home(),
                local_workspace_root=workspace_root,
            )
            materialization = _restore_project_bytes(
                owners,
                capture,
                payload,
                operation_projects,
                existing_remote=record.state_remote
                and previous is not None
                and previous.project(capture.project_id) is not None,
            )
            complete_restored_project_publication(store, capture, owners, materialization)
            if record.state_remote:
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "UPDATE projects SET locator=? WHERE project_id=?",
                        (new_locator, record.project_id),
                    )
            if workspace_root is not None:
                for name in (".research", "artifacts", "views"):
                    candidate_root = workspace_root.parent / name
                    live = Path(record.state_location).parent / name
                    if name != ".research" and not candidate_root.exists() and not live.exists():
                        continue
                    candidate_root.mkdir(mode=0o700, exist_ok=True)
                    _private_ancestors(live.parent)
                    if not os.path.lexists(live):
                        live.mkdir(mode=0o700)
                    _private_ancestors(live)
                    if str(live) not in known:
                        previous_payload = output / "previous" / capture.project_id / name
                        previous_payload.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        _snapshot_tree(live, previous_payload, relative_prefix=PurePosixPath(name))
                        extra_previous.append({"live": str(live), "payload": str(previous_payload)})
                        known.add(str(live))
                    if name in {"artifacts", "views"}:
                        prior = next(
                            r
                            for r in (*request.previous_roots, *extra_previous)
                            if r["live"] == str(live)
                        )
                        _preserve_unrestored_files(Path(prior["payload"]), candidate_root)
                    roots.append({"live": str(live), "payload": str(candidate_root)})
        for old in request.previous_roots:
            if old["live"] not in {r["live"] for r in roots}:
                empty = output / "empty" / str(len(roots))
                empty.mkdir(mode=0o700, parents=True)
                roots.append({"live": old["live"], "payload": str(empty)})
        # A candidate capture keeps original archive paths while replacing only
        # its SQLite entry with the detached, rebound, migrated candidate.
        capture_id = str(uuid.uuid4())
        capture_root = output / f"backup-{capture_id}"
        _snapshot_tree(payload, capture_root, relative_prefix=PurePosixPath("capture"))
        snapshot = capture_root / "rcp.sqlite3"
        store.online_snapshot(snapshot)
        digest, size = _hash_regular_file(snapshot)
        inventories = tuple(
            BackupSnapshotProjectInventory(
                project_id=p.project_id,
                home_space_id=p.home_space_id,
                locator=store.project(p.project_id).locator,
                status="capturable" if p.status == "captured" else "uncaptured",
                recovery=p.recovery if p.status == "captured" else None,
                task_operation_ids=tuple(
                    sorted(t.operation_id for t in store.all_project_agent_tasks(p.project_id))
                )
                if p.status == "captured"
                else (),
                unavailable_reason=p.unavailable_reason if p.status != "captured" else None,
                unavailable_at=p.unavailable_at if p.status != "captured" else None,
            )
            for p in manifest.projects
        )
        plan = inspect_app_data_capture_plan(app).model_copy(
            update={"data_dir": str(data), "database_path": str(data / "rcp.sqlite3")}
        )
        sqlite_receipt = BackupSQLiteCaptureReceipt(
            capture_id=capture_id,
            captured_at=request.detached_at,
            rcp_source_commit=request.source_commit,
            space_id=store.space_id,
            space_name=store.space_name,
            snapshot_path=str(snapshot),
            database_schema_sha256=_database_schema_sha256(snapshot),
            sqlite_snapshot=BackupFileEntry(
                archive_path="database/rcp.sqlite3",
                source_relative_path="rcp.sqlite3",
                group="sqlite_snapshot",
                sha256=digest,
                size_bytes=size,
            ),
            app_data_plan=plan,
            projects=inventories,
            status="partial"
            if any(p.status == "uncaptured" for p in manifest.projects)
            else "complete",
        )
        sqlite_digest = write_immutable_backup_receipt(
            capture_root / "sqlite-capture.json", sqlite_receipt
        )
        projects = BackupProjectFileCaptureReceipt(
            capture_id=capture_id,
            captured_at=request.detached_at,
            completed_at=datetime.now(UTC),
            rcp_source_commit=request.source_commit,
            space_id=store.space_id,
            sqlite_receipt_sha256=sqlite_digest,
            sqlite_snapshot_sha256=digest,
            sqlite_capture_status=sqlite_receipt.status,
            projects=manifest.projects,
            imported_sources=manifest.imported_sources,
            status=sqlite_receipt.status,
        )
        project_digest = write_immutable_backup_receipt(
            capture_root / "project-files.json", projects
        )
        boundary = _canonical_sha256(
            {"sqlite": sqlite_digest, "projects": project_digest, "roots": roots}
        )
        baseline = _check_copy(
            output / "baseline",
            sqlite_receipt,
            sqlite_digest,
            projects,
            project_digest,
            capture_root,
        )
        proof = ApplicationProof(
            boundary_sha256=boundary,
            capture_root=str(capture_root),
            sqlite_receipt=sqlite_receipt,
            sqlite_receipt_sha256=sqlite_digest,
            project_receipt=projects,
            project_receipt_sha256=project_digest,
            read_model=baseline,
        )
        proof_path = output / "proof.json"
        proof_digest = _publish_proof(proof_path, proof)
        _set_private_directory_modes(output)
        _fsync_tree(output)
        return {
            "version": 1,
            "status": "prepared",
            "boundary_sha256": boundary,
            "roots": roots,
            "extra_previous_roots": extra_previous,
            "proof_path": str(proof_path),
            "proof_sha256": proof_digest,
        }
