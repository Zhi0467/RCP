"""Replacement checkout proofs, separate from immutable provisioning reviews.

The archive embeds the resolved recovery descriptor. These local receipts are
rebuilt from it during restore, so their raw files need not enter the archive.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from rcp.limits import BACKUP_RECEIPT_MAX_BYTES
from rcp.server_ops.backup_models import BackupCheckoutRecoveryDescriptor


class ReplacementCheckoutProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provisioned: BackupCheckoutRecoveryDescriptor
    replacement: BackupCheckoutRecoveryDescriptor

    @model_validator(mode="after")
    def validate_replacement(self) -> ReplacementCheckoutProof:
        excluded = {"git_commit", "deploy_key_label", "public_key_fingerprint"}
        if (
            self.provisioned.project_id != self.replacement.project_id
            or self.provisioned.home_space_id != self.replacement.home_space_id
            or self.provisioned.machines != self.replacement.machines
            or [r.model_dump(exclude=excluded) for r in self.provisioned.repositories]
            != [r.model_dump(exclude=excluded) for r in self.replacement.repositories]
        ):
            raise ValueError("replacement proof cannot change repository identity or authority")
        return self


def _proof_path(data_dir: Path, project_id: str, *, create: bool = False) -> Path:
    root = data_dir / "checkout-recovery"
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("replacement checkout proof directory is unsafe")
    return root / f"{project_id}.json"


def apply_replacement_proofs(
    data_dir: Path, recovery: BackupCheckoutRecoveryDescriptor
) -> BackupCheckoutRecoveryDescriptor:
    from rcp.server_ops.backup import _read_private_file

    try:
        path = _proof_path(data_dir, recovery.project_id)
        path.lstat()
    except FileNotFoundError:
        return recovery
    proof = ReplacementCheckoutProof.model_validate_json(
        _read_private_file(path, expected_uid=os.geteuid(), maximum=BACKUP_RECEIPT_MAX_BYTES)
    )
    if any(
        descriptor.project_id != recovery.project_id
        or descriptor.home_space_id != recovery.home_space_id
        for descriptor in (proof.provisioned, proof.replacement)
    ):
        raise ValueError("replacement checkout proof belongs to another project")
    original = {r.alias: r for r in proof.provisioned.repositories}
    replacements = {r.alias: r for r in proof.replacement.repositories}
    old_machines = {m.alias: m for m in proof.provisioned.machines}
    machines = {m.alias: m for m in recovery.machines}
    repositories = tuple(
        replacements[r.alias]
        if original.get(r.alias) == r
        and old_machines.get(r.machine_alias) == machines[r.machine_alias]
        else r
        for r in recovery.repositories
    )
    return BackupCheckoutRecoveryDescriptor.model_validate(
        {**recovery.model_dump(), "repositories": repositories}
    )


def record_replacement_proofs(
    data_dir: Path,
    provisioned: BackupCheckoutRecoveryDescriptor,
    replacement: BackupCheckoutRecoveryDescriptor,
) -> None:
    from rcp.server_ops.backup import _atomic_replace_private, _model_bytes

    proof = ReplacementCheckoutProof(provisioned=provisioned, replacement=replacement)
    _atomic_replace_private(
        _proof_path(data_dir, provisioned.project_id, create=True), _model_bytes(proof)
    )
