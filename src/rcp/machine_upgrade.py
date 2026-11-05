"""The one reader of machines named by per-project alias, from before a machine was its card.

Older manifests held `[[machines]]` tables and named each by an alias the
project chose; records copied that alias or the machine's host. This module
reads those shapes once and maps them through one rule:

    A legacy reference maps to the card whose `host` equals the reference's
    host, or to "". An alias gets its host only from its own legacy manifest;
    a host-keyed row uses its host. An empty host is the backend card.

No other module names the legacy key. The upgrade never creates a card from
a row; cards come from manifests through `ensure_space_machines`. The backend
card has the fixed id `BACKEND_MACHINE_ID`, so a reference to it needs no card.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import tomlkit
from pydantic import BaseModel, Field, field_validator, model_validator

from rcp.config import (
    LEGACY_COMPUTE_CONNECTIONS_KEY,
    MachineComputeConfig,
    Manifest,
    PaperCoachConfig,
    is_legacy_manifest_document,
    load_manifest,
    manifest_from_text,
    resolve_manifest_path,
    validate_machine_access_hint,
    validate_machine_provider_paths,
)
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.providers import ProviderId
from rcp.server_ops.backup_models import (
    BackupArchiveManifest,
    BackupCheckoutRecoveryDescriptor,
    BackupManifestAgentProfile,
    BackupManifestConfiguration,
    BackupProjectCapture,
    BackupRecoveryRepository,
)
from rcp.ssh_validation import validate_ssh_destination
from rcp.storage.models import (
    ProjectProvisioningMachineRecord,
    ProjectProvisioningProviderCheckRecord,
    ProjectProvisioningRepositoryRecord,
    ProjectProvisioningRequestRecord,
    SpaceMachineRecord,
)

_OS_ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")
_AGENT_PROFILE_TABLES = (
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
    "orchestrator",
)


def machine_id_for_host(host: str, cards: Iterable[SpaceMachineRecord]) -> str:
    """The rule: the card whose host equals `host`, or "" for a machine the space lacks."""

    if not host:
        return BACKEND_MACHINE_ID
    return next((card.machine_id for card in cards if card.host == host), "")


class LegacyMachine(BaseModel):
    """One `[[machines]]` table, with the settings older builds kept there."""

    alias: str
    host: str = ""
    os_account: str = ""
    provider_paths: dict[ProviderId, str] = Field(default_factory=dict)
    compute: MachineComputeConfig | None = None
    access_hint: str = Field(default="", max_length=512)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = value.strip()
        return validate_ssh_destination(value) if value else value

    @field_validator("os_account")
    @classmethod
    def validate_os_account(cls, value: str) -> str:
        if value and _OS_ACCOUNT.fullmatch(value) is None:
            raise ValueError("machine operating-system account is invalid")
        return value

    @field_validator("access_hint")
    @classmethod
    def validate_access_hint(cls, value: str) -> str:
        return validate_machine_access_hint(value)

    @model_validator(mode="after")
    def validate_provider_paths(self) -> LegacyMachine:
        self.provider_paths = validate_machine_provider_paths(self.provider_paths, host=self.host)
        return self


@dataclass(frozen=True)
class LegacyManifest:
    """A manifest that still names machines by alias, read but not yet rewritten."""

    path: Path
    machines: tuple[LegacyMachine, ...]

    def machine_ids(self, cards: Iterable[SpaceMachineRecord]) -> dict[str, str]:
        """Each alias's card id under the rule; "" for a machine the space lacks."""

        cards = list(cards)
        return {
            machine.alias: machine_id_for_host(machine.host, cards) for machine in self.machines
        }


def read_legacy_manifest(path: Path) -> LegacyManifest | None:
    """Read `path` when it still holds `[[machines]]` tables; None when it is current."""

    document = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    if not is_legacy_manifest_document(document):
        return None
    machines = tuple(LegacyMachine.model_validate(item) for item in document["machines"])
    aliases = [machine.alias for machine in machines]
    if len(aliases) != len(set(aliases)):
        raise ValueError("machine aliases must be unique")
    return LegacyManifest(path=path, machines=machines)


def upgraded_manifest_text(legacy: LegacyManifest, machine_ids: Mapping[str, str]) -> str:
    """The manifest naming cards by id, for the caller to publish in place of the legacy one.

    Repositories keep their alias and path. Agent profiles drop `run_on`; a
    retired `[execution]` and `[paper.coach]` become the profiles they stood for.
    """

    document = tomlkit.parse(legacy.path.read_text(encoding="utf-8"))
    for repository in document.get("repositories", []):
        repository["machine"] = machine_ids.get(str(repository.get("machine", "")), "")
    agent = document.get("agent")
    if agent is None:
        agent = tomlkit.table()
        document.add("agent", agent)
    paper = document.get("paper")
    coach = paper.get("coach") if paper is not None else None
    if coach is not None and "paper_coach" not in agent:
        legacy_coach = PaperCoachConfig.model_validate(coach.unwrap())
        table = tomlkit.table()
        table.add("provider", legacy_coach.default_provider)
        table.add("model", legacy_coach.default_model)
        table.add("reasoning", legacy_coach.default_reasoning)
        agent["paper_coach"] = table
    for surface in _AGENT_PROFILE_TABLES:
        profile = agent.get(surface)
        if profile is not None:
            profile.pop("run_on", None)
    document.pop("execution", None)
    if paper is not None:
        paper.pop("coach", None)
        if not paper:
            document.pop("paper")
    document.pop(LEGACY_COMPUTE_CONNECTIONS_KEY, None)
    del document["machines"]
    used = [machine_ids.get(machine.alias, "") for machine in legacy.machines]
    document.add("machines", list(dict.fromkeys(item for item in used if item)))
    content = tomlkit.dumps(document)
    Manifest.model_validate(tomlkit.parse(content).unwrap())
    return content


def cards_by_id(cards: Iterable[SpaceMachineRecord]) -> dict[str, SpaceMachineRecord]:
    """Each card by its id, the backend's own card by `BACKEND_MACHINE_ID` in either schema.

    A snapshot from before migration 77 may hold the card with no host under
    another id; migration 77 re-keys it, so a reader of the snapshot does too.
    """

    return {BACKEND_MACHINE_ID if not card.host else card.machine_id: card for card in cards}


def snapshot_cards(connection: sqlite3.Connection) -> dict[str, SpaceMachineRecord]:
    """Each card in a snapshot of any release by id, reading only the identity columns.

    An update captures the outgoing release's database before migrating it, so
    later card columns may be absent.
    """

    return cards_by_id(
        SpaceMachineRecord(
            machine_id=str(machine_id),
            name=str(name),
            host=str(host),
            os_account=str(account),
            created_at=str(created),
            updated_at=str(updated),
        )
        for machine_id, name, host, account, created, updated in connection.execute(
            "SELECT machine_id, name, host, os_account, created_at, updated_at FROM space_machines"
        )
    )


def load_upgraded_manifest(
    value: str | Path, cards: Iterable[SpaceMachineRecord], *, project_root: Path | None = None
) -> Manifest:
    """The manifest at `value`, a legacy one upgraded in memory and left on disk as written.

    An update prepares from the outgoing release's live projects, whose manifests
    the candidate may read but not rewrite before it serves.
    """

    path = resolve_manifest_path(value)
    legacy = read_legacy_manifest(path)
    if legacy is None:
        return load_manifest(path, project_root=project_root)
    content = upgraded_manifest_text(legacy, legacy.machine_ids(cards))
    return manifest_from_text(content, path, project_root=project_root)


def machine_id_for_alias(
    value: str, machine_ids: Mapping[str, str], card_ids: Collection[str]
) -> str:
    """An alias-keyed record value's card id: its alias's card, an id already written, or ""."""

    if value in machine_ids:
        return machine_ids[value]
    return value if value in card_ids else ""


def upgraded_worktree_binding(
    payload: Mapping[str, Any], machine_ids: Mapping[str, str], card_ids: Collection[str]
) -> dict[str, Any]:
    """A stored worktree binding naming its card; the alias and host copies are dropped."""

    upgraded = {
        key: value for key, value in payload.items() if key not in {"machine", "execution_host"}
    }
    if "machine" in payload:
        upgraded["machine_id"] = machine_id_for_alias(
            str(payload["machine"]), machine_ids, card_ids
        )
    return upgraded


def upgraded_hidden_read_scope(
    payload: Mapping[str, Any], machine_ids: Mapping[str, str], card_ids: Collection[str]
) -> dict[str, Any]:
    """A stored hidden-read scope naming its card; the alias and host copies are dropped."""

    upgraded = {
        key: value
        for key, value in payload.items()
        if key not in {"execution_machine", "execution_host"}
    }
    if "execution_machine" in payload:
        upgraded["machine_id"] = machine_id_for_alias(
            str(payload["execution_machine"]), machine_ids, card_ids
        )
    return upgraded


def upgraded_terminal_record(
    payload: dict[str, Any], cards: Iterable[SpaceMachineRecord]
) -> dict[str, Any]:
    """A terminal record an older build wrote, naming its card; one the space lacks is unchanged.

    That build recorded the shell's host and its declared alias and account. The
    host resolves through the rule, and an unresolved record stays as written,
    so it still blocks its repository.
    """

    if "machine_id" in payload or "execution_host" not in payload:
        return payload
    machine_id = machine_id_for_host(str(payload["execution_host"]), cards)
    if not machine_id:
        return payload
    legacy = {"execution_host", "declared_machine", "declared_account"}
    return {
        **{key: value for key, value in payload.items() if key not in legacy},
        "machine_id": machine_id,
    }


class _LegacyProvisioningMachine(ProjectProvisioningMachineRecord):
    machine_id: str = Field(default="", exclude=True)
    alias: str
    location: Literal["local", "ssh"]
    host: str = ""
    os_account: str


class _LegacyProvisioningRepository(ProjectProvisioningRepositoryRecord):
    machine_id: str = Field(default="", exclude=True)
    machine_alias: str


class _LegacyProvisioningProviderCheck(ProjectProvisioningProviderCheckRecord):
    machine_id: str = Field(default="", exclude=True)
    machine_alias: str


class _LegacyProvisioningRequest(ProjectProvisioningRequestRecord):
    machines: list[_LegacyProvisioningMachine] = Field(min_length=1, max_length=32)  # pyright: ignore[reportIncompatibleVariableOverride]
    repositories: list[_LegacyProvisioningRepository] = Field(min_length=1, max_length=64)  # pyright: ignore[reportIncompatibleVariableOverride]
    provider_checks: list[_LegacyProvisioningProviderCheck] = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        default_factory=list, max_length=32
    )

    @model_validator(mode="after")
    def validate_request(self) -> _LegacyProvisioningRequest:
        """A frozen receipt was validated when written; its digest is its proof now."""

        return self


def is_legacy_provisioning_payload(payload: Mapping[str, Any]) -> bool:
    """Whether an assembled provisioning request still names machines by alias."""

    return any("alias" in machine for machine in payload.get("machines", []))


def read_legacy_provisioning_request(
    payload: Mapping[str, Any], cards: Iterable[SpaceMachineRecord]
) -> ProjectProvisioningRequestRecord:
    """Verify a legacy request's stored review digest, then name its machines by card.

    The stored bytes stay as they are; the mapped record exists only in memory.
    A machine the space no longer has maps to "" and is shown as removed.
    """

    from rcp.storage.provisioning import _verify_project_provisioning_review_digest

    legacy = _LegacyProvisioningRequest.model_validate_json(json.dumps(dict(payload)))
    _verify_project_provisioning_review_digest(legacy)
    cards = list(cards)
    machine_ids = {
        machine.alias: machine_id_for_host(machine.host, cards) for machine in legacy.machines
    }
    machines: dict[str, dict[str, Any]] = {}
    for machine in legacy.machines:
        machines.setdefault(
            machine_ids[machine.alias],
            {
                "machine_id": machine_ids[machine.alias],
                "central_root": machine.central_root,
                "resolved_central_root": machine.resolved_central_root,
            },
        )
    upgraded = legacy.model_dump(mode="json")
    upgraded["machines"] = list(machines.values())
    for key in ("repositories", "provider_checks"):
        for item in upgraded[key]:
            item["machine_id"] = machine_ids.get(item.pop("machine_alias"), "")
    return ProjectProvisioningRequestRecord.model_validate_json(json.dumps(upgraded))


class _LegacyBackupStrictModel(BaseModel):
    model_config = BackupArchiveManifest.model_config


class _LegacyBackupMachineCompute(_LegacyBackupStrictModel):
    job_manager: Literal["slurm"] | None = None
    jobs_root: str = ""


class _LegacyBackupMachine(_LegacyBackupStrictModel):
    alias: str
    host: str
    os_account: str
    provider_paths: dict[ProviderId, str]
    access_hint: str = Field(default="", exclude_if=lambda value: not value)
    compute: _LegacyBackupMachineCompute | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class _LegacyBackupComputeConnection(_LegacyBackupStrictModel):
    id: str
    name: str
    kind: Literal["local", "ssh"]
    ssh_target: str = Field(max_length=255)
    access_hint: str


class _LegacyBackupAgentProfile(BackupManifestAgentProfile):
    run_on: str


class _LegacyBackupConfiguration(BackupManifestConfiguration):
    machines: tuple[_LegacyBackupMachine, ...]  # pyright: ignore[reportIncompatibleVariableOverride]
    agent_profiles: tuple[_LegacyBackupAgentProfile, ...]  # pyright: ignore[reportIncompatibleVariableOverride]
    compute_connections: tuple[_LegacyBackupComputeConnection, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def validate_references(self) -> _LegacyBackupConfiguration:
        """An archived configuration was validated when captured; its digest is its proof now."""

        return self


class _LegacyBackupRecoveryMachine(_LegacyBackupStrictModel):
    alias: str
    location: Literal["local", "ssh"]
    host: str
    os_account: str
    resolved_central_root: str | None


class _LegacyBackupRecoveryRepository(BackupRecoveryRepository):
    machine_id: str = Field(default="", exclude=True)
    machine_alias: str


class _LegacyBackupRecoveryDescriptor(BackupCheckoutRecoveryDescriptor):
    configuration: _LegacyBackupConfiguration  # pyright: ignore[reportIncompatibleVariableOverride]
    machines: tuple[_LegacyBackupRecoveryMachine, ...]  # pyright: ignore[reportIncompatibleVariableOverride]
    repositories: tuple[_LegacyBackupRecoveryRepository, ...]  # pyright: ignore[reportIncompatibleVariableOverride]

    @model_validator(mode="after")
    def validate_descriptor(self) -> _LegacyBackupRecoveryDescriptor:
        if self.configuration_sha256 != self.configuration.sha256:
            raise ValueError("backup manifest configuration digest does not match its payload")
        return self


class _LegacyBackupProjectCapture(BackupProjectCapture):
    recovery: _LegacyBackupRecoveryDescriptor | None = None  # pyright: ignore[reportIncompatibleVariableOverride]


class LegacyBackupArchiveManifest(BackupArchiveManifest):
    """A format-1 backup archive manifest, whose recovery names machines by alias and host."""

    schema_version: Literal[1] = 1  # pyright: ignore[reportIncompatibleVariableOverride]
    projects: tuple[_LegacyBackupProjectCapture, ...]  # pyright: ignore[reportIncompatibleVariableOverride]


def read_backup_archive_manifest(
    payload: bytes,
) -> BackupArchiveManifest | LegacyBackupArchiveManifest:
    """Parse an archive manifest of either format; a format-1 one is upgraded after cards exist."""

    document = json.loads(payload)
    if isinstance(document, dict) and document.get("schema_version") == 1:
        return LegacyBackupArchiveManifest.model_validate_json(payload)
    return BackupArchiveManifest.model_validate_json(payload)


def upgraded_backup_manifest(
    manifest: BackupArchiveManifest | LegacyBackupArchiveManifest,
    cards: Iterable[SpaceMachineRecord],
) -> BackupArchiveManifest:
    """Name a format-1 archive's machines by the restored space's cards, in memory.

    Each archived alias maps by its host. A machine whose host has no card
    maps to "", which the format-2 recovery descriptor refuses.
    """

    if not isinstance(manifest, LegacyBackupArchiveManifest):
        return manifest
    cards = list(cards)
    projects = []
    for project in manifest.projects:
        payload = project.model_dump(exclude={"recovery"})
        if project.recovery is not None:
            payload["recovery"] = _upgraded_recovery(
                project.recovery, lambda host: machine_id_for_host(host, cards)
            )
        projects.append(BackupProjectCapture.model_validate(payload))
    payload = manifest.model_dump(exclude={"schema_version", "projects"})
    payload["projects"] = tuple(projects)
    upgraded = BackupArchiveManifest.model_validate(payload)
    for field in ("artifact_inventory", "app_data_files"):
        if field not in manifest.model_fields_set:
            upgraded.model_fields_set.discard(field)
    return upgraded


def upgraded_capture_receipt(document: dict[str, Any], snapshot: Path) -> dict[str, Any]:
    """An SQLite capture receipt from an older release, its recovery named by card ids.

    The outgoing release of an update wrote the receipt; each archived alias maps
    by its host to a card in the receipt's own SQLite snapshot, read without
    migration. A receipt that already names cards is returned unchanged.
    """

    projects = document.get("projects")
    if not isinstance(projects, list) or not any(
        _is_legacy_recovery(project.get("recovery")) for project in projects
    ):
        return document
    with closing(
        sqlite3.connect(f"file:{quote(str(snapshot))}?mode=ro&immutable=1", uri=True)
    ) as connection:
        by_host = {
            str(host): str(machine_id)
            for machine_id, host in connection.execute(
                "SELECT machine_id, host FROM space_machines"
            )
        }

    def id_for_host(host: str) -> str:
        return BACKEND_MACHINE_ID if not host else by_host.get(host, "")

    upgraded = []
    for project in projects:
        if _is_legacy_recovery(project.get("recovery")):
            legacy = _LegacyBackupRecoveryDescriptor.model_validate_json(
                json.dumps(project["recovery"])
            )
            project = {
                **project,
                "recovery": _upgraded_recovery(legacy, id_for_host).model_dump(mode="json"),
            }
        upgraded.append(project)
    return {**document, "projects": upgraded}


def local_project_state_paths(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """Each project whose state is on the backend, with its state path, in either schema.

    An update reads the outgoing release's snapshot, which may predate migration 77.
    """

    if "state_machine_id" in _columns(connection, "projects"):
        query = (
            "SELECT project_id, state_path FROM projects WHERE state_machine_id = ? "
            "ORDER BY project_id"
        )
        return [(str(a), str(b)) for a, b in connection.execute(query, (BACKEND_MACHINE_ID,))]
    query = (
        "SELECT project_id, state_location FROM projects WHERE NOT state_remote ORDER BY project_id"
    )
    return [(str(a), str(b)) for a, b in connection.execute(query)]


def _is_legacy_recovery(recovery: object) -> bool:
    machines = recovery.get("machines") if isinstance(recovery, dict) else None
    return isinstance(machines, list) and any(
        isinstance(machine, dict) and "alias" in machine for machine in machines
    )


def _upgraded_recovery(
    legacy: _LegacyBackupRecoveryDescriptor, id_for_host: Callable[[str], str]
) -> BackupCheckoutRecoveryDescriptor:
    archived = legacy.configuration
    machine_ids = {machine.alias: id_for_host(machine.host) for machine in archived.machines}
    configuration = BackupManifestConfiguration.model_validate(
        {
            **archived.model_dump(
                exclude={"machines", "repositories", "agent_profiles", "compute_connections"}
            ),
            "machines": tuple(dict.fromkeys(item for item in machine_ids.values() if item)),
            "repositories": tuple(
                {**repository.model_dump(), "machine": machine_ids.get(repository.machine, "")}
                for repository in archived.repositories
            ),
            "agent_profiles": tuple(
                profile.model_dump(exclude={"run_on"}) for profile in archived.agent_profiles
            ),
        }
    )
    machines: dict[str, dict[str, Any]] = {}
    for machine in legacy.machines:
        machine_id = machine_ids.get(machine.alias, "")
        if machine_id:
            machines.setdefault(
                machine_id,
                {
                    "machine_id": machine_id,
                    "resolved_central_root": machine.resolved_central_root,
                },
            )
    return BackupCheckoutRecoveryDescriptor.model_validate(
        {
            **legacy.model_dump(exclude={"configuration", "configuration_sha256", "machines"}),
            "configuration": configuration,
            "configuration_sha256": configuration.sha256,
            "machines": tuple(machines.values()),
            "repositories": tuple(
                {
                    **repository.model_dump(exclude={"machine_alias"}),
                    "machine_id": machine_ids.get(repository.machine_alias, ""),
                }
                for repository in legacy.repositories
            ),
        }
    )


class _Cards:
    """The space's cards by host during migration 77, with the backend card on its fixed id.

    A backend card made before the fixed id is re-keyed, and so are the Space
    turns and threads that named it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT machine_id FROM space_machines WHERE host = '' AND machine_id != ?",
            (BACKEND_MACHINE_ID,),
        ).fetchone()
        if row is not None:
            old = str(row[0])
            connection.execute(
                "UPDATE space_machines SET machine_id = ? WHERE machine_id = ?",
                (BACKEND_MACHINE_ID, old),
            )
            connection.execute(
                "UPDATE graph_runs SET request_json = json_set(request_json, '$.run_on', ?) "
                "WHERE json_extract(request_json, '$.run_on') = ?",
                (BACKEND_MACHINE_ID, old),
            )
            connection.execute(
                "UPDATE agent_threads SET agent_run_on = ? WHERE agent_run_on = ?",
                (BACKEND_MACHINE_ID, old),
            )
        self.by_host = {
            str(host): str(machine_id)
            for machine_id, host in connection.execute(
                "SELECT machine_id, host FROM space_machines"
            )
        }

    def id_for_host(self, host: str) -> str:
        return BACKEND_MACHINE_ID if not host else self.by_host.get(host, "")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _rekey(
    connection: sqlite3.Connection,
    table: str,
    old: str,
    new: str,
    cards: _Cards | None,
    *,
    unique_keys: tuple[tuple[tuple[str, ...], str], ...] = (),
    newest: str = "rowid",
) -> None:
    """Rename `old` to `new` and map each value; on a key collision keep the newest row.

    `cards` None renames only: the per-project rewrite maps alias values later.
    Each unique key is its columns and a row predicate, as a partial index has.
    """

    if new in _columns(connection, table):
        return
    connection.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
    if cards is None:
        return
    values = [
        str(row[0])
        for row in connection.execute(
            f'SELECT DISTINCT "{new}" FROM "{table}" WHERE "{new}" IS NOT NULL'
        )
    ]
    mapping = {value: value if value == "*" else cards.id_for_host(value) for value in values}
    for key_columns, predicate in unique_keys:
        rows = connection.execute(
            f'SELECT rowid AS row_id, "{new}" AS value, {newest} AS newest, '
            + ", ".join(f'"{column}"' for column in key_columns if column != new)
            + f' FROM "{table}" WHERE "{new}" IS NOT NULL AND ({predicate})'
        ).fetchall()
        groups: dict[tuple[object, ...], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            key = tuple(row[column] for column in key_columns if column != new)
            groups[(*key, mapping[str(row["value"])])].append(row)
        for group in groups.values():
            group.sort(key=lambda row: (str(row["newest"] or ""), int(row["row_id"])), reverse=True)
            connection.executemany(
                f'DELETE FROM "{table}" WHERE rowid = ?', [(row["row_id"],) for row in group[1:]]
            )
    connection.execute(
        f'UPDATE "{table}" SET "{new}" = char(0) || "{new}" WHERE "{new}" IS NOT NULL'
    )
    connection.executemany(
        f'UPDATE "{table}" SET "{new}" = ? WHERE "{new}" = char(0) || ?',
        [(target, value) for value, target in mapping.items()],
    )


def _drop_column(connection: sqlite3.Connection, table: str, column: str) -> None:
    from rcp.storage.resource_versions import forget_compared_column

    if column not in _columns(connection, table):
        return
    forget_compared_column(connection, table, column)
    connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')


def _split_state_location(location: str, remote: bool) -> tuple[str, str]:
    """`host:/path/.research` for a remote state root, or a backend path."""

    if not remote:
        return "", location
    host, separator, path = location.partition(":/")
    if not separator:
        raise RuntimeError("stored remote project state location is invalid")
    return host, "/" + path


def _migrate_projects(connection: sqlite3.Connection, cards: _Cards) -> None:
    from rcp.storage.resource_versions import reinstall_project_settings_versions

    columns = _columns(connection, "projects")
    if "state_machine_id" in columns:
        return
    connection.execute("ALTER TABLE projects RENAME COLUMN state_location TO state_path")
    connection.execute("ALTER TABLE projects ADD COLUMN state_machine_id TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "ALTER TABLE projects ADD COLUMN machine_ids_pending INTEGER NOT NULL DEFAULT 0"
    )
    rows = connection.execute(
        "SELECT project_id, state_path, state_remote FROM projects"
    ).fetchall()
    for project_id, location, remote in rows:
        host, path = _split_state_location(str(location), bool(remote))
        connection.execute(
            "UPDATE projects SET state_machine_id = ?, state_path = ?, machine_ids_pending = 1 "
            "WHERE project_id = ?",
            (cards.id_for_host(host), path, project_id),
        )
    for event in ("insert", "update", "delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS resource_projects_{event}")
    connection.execute("ALTER TABLE projects DROP COLUMN state_remote")
    reinstall_project_settings_versions(connection)


def _upgraded_project_json(project_json: str, cards: _Cards) -> str:
    """A stored ProjectRecord snapshot naming its state card instead of a host location."""

    project = json.loads(project_json)
    host, path = _split_state_location(
        str(project.pop("state_location")), bool(project.pop("state_remote"))
    )
    project["state_machine_id"] = cards.id_for_host(host)
    project["state_path"] = path
    return json.dumps(project)


def _migrate_stage_receipts(connection: sqlite3.Connection, cards: _Cards) -> None:
    """Receipts that name their stage by host: chat layouts and remote provider passes.

    An unresolved remote pass found by its stage card fences a second pass on it.
    """
    rows = connection.execute(
        "SELECT receipt_id, payload_json FROM graph_run_receipts WHERE category IN "
        "('chat_stage_layout', 'remote_provider_started', 'remote_provider_stopped')"
    ).fetchall()
    for receipt_id, payload_json in rows:
        payload = json.loads(payload_json)
        if "stage_host" not in payload:
            continue
        upgraded = {
            ("stage_machine_id" if key == "stage_host" else key): (
                cards.id_for_host(str(value)) if key == "stage_host" else value
            )
            for key, value in payload.items()
        }
        connection.execute(
            "UPDATE graph_run_receipts SET payload_json = ? WHERE receipt_id = ?",
            (json.dumps(upgraded), receipt_id),
        )


def _migrate_artifact_edit_stages(connection: sqlite3.Connection, cards: _Cards) -> None:
    rows = connection.execute(
        "SELECT operation_id, request_json FROM graph_runs "
        "WHERE json_type(request_json, '$.artifact_edit.stage_host') IS NOT NULL"
    ).fetchall()
    for operation_id, request_json in rows:
        request = json.loads(request_json)
        edit = request["artifact_edit"]
        host = edit.pop("stage_host")
        edit["stage_machine_id"] = (
            cards.id_for_host(host or "") if host or edit.get("stage_root") else None
        )
        connection.execute(
            "UPDATE graph_runs SET request_json = ? WHERE operation_id = ?",
            (json.dumps(request), operation_id),
        )


def _rename_credential_folders(file_root: Path, cards: _Cards) -> None:
    """`<data>/providers/<provider>/{local,remote-<sha256 host>}` becomes `machine-<id>`."""

    providers = file_root / "providers"
    if not providers.is_dir():
        return
    for provider in sorted(providers.iterdir()):
        if not provider.is_dir():
            continue
        names = {"local": ""}
        names.update(
            {"remote-" + hashlib.sha256(host.encode()).hexdigest(): host for host in cards.by_host}
        )
        for name, host in names.items():
            source = provider / name
            if not source.is_dir():
                continue
            machine_id = cards.id_for_host(host)
            target = provider / f"machine-{machine_id}"
            if machine_id and not target.exists():
                source.rename(target)


def migrate_machine_identity(connection: sqlite3.Connection, file_root: Path) -> None:
    """Migration 77: name machines by card id in every host-keyed table.

    Host-keyed rows are mapped through the rule. Alias-keyed columns are only
    renamed; `projects.machine_ids_pending` marks every project for the
    per-project rewrite, which maps them with the project's own manifest.
    Readiness and skill inventories are caches and are cleared.
    """

    from rcp.storage.memory import migrate_memory_deletion_machines

    cards = _Cards(connection)
    login_member = (
        ("provider", "machine_id", "member_id"),
        "member_id IS NOT NULL AND removed_at IS NULL",
    )
    _rekey(
        connection,
        "provider_login_states",
        "host",
        "machine_id",
        cards,
        unique_keys=((("provider", "machine_id", "login_id"), "1"), login_member),
        newest="changed_at",
    )
    _rekey(
        connection,
        "provider_login_assignments",
        "host",
        "machine_id",
        cards,
        unique_keys=((("provider", "machine_id", "login_id"), "1"),),
        newest="COALESCE(last_assigned_at, limited_at)",
    )
    _rekey(
        connection,
        "provider_login_settings",
        "host",
        "machine_id",
        cards,
        unique_keys=((("user_id", "provider", "machine_id"), "1"),),
        newest="changed_at",
    )
    for cache in ("provider_readiness_snapshots", "provider_skill_inventories"):
        connection.execute(f"DELETE FROM {cache}")
        _rekey(connection, cache, "host", "machine_id", None)
    connection.execute("DELETE FROM compute_backend_probes")
    for table in ("chat_session_contexts", "writing_sessions", "compute_backend_probes"):
        _rekey(connection, table, "execution_machine", "machine_id", None)
    for table in ("compute_jobs", "watchers", "browser_owners"):
        _rekey(connection, table, "execution_host", "machine_id", cards)
    _drop_column(connection, "compute_jobs", "execution_machine")
    for table in ("graph_runs", "artifact_revision_candidates"):
        _rekey(connection, table, "stage_host", "stage_machine_id", cards)
    for table in ("episode_wrapups", "experiment_episode_state"):
        _rekey(connection, table, "execution_host", "machine_id", cards)
        _rekey(connection, table, "stage_host", "stage_machine_id", cards)
    for table in (
        "graph_runs",
        "artifact_revision_candidates",
        "episode_wrapups",
        "experiment_episode_state",
    ):
        connection.execute(
            f'UPDATE "{table}" SET stage_machine_id = ? '
            "WHERE stage_machine_id IS NULL AND stage_root IS NOT NULL",
            (BACKEND_MACHINE_ID,),
        )
    _migrate_artifact_edit_stages(connection, cards)
    _drop_column(connection, "episode_wrapups", "run_on")
    if "execution_machine" in _columns(connection, "experiment_episode_state"):
        connection.execute(
            "UPDATE experiment_episode_state SET machine_id = '' WHERE execution_machine IS NULL"
        )
    _drop_column(connection, "experiment_episode_state", "execution_machine")
    _migrate_projects(connection, cards)
    migrate_memory_deletion_machines(
        connection, lambda project_json: _upgraded_project_json(project_json, cards)
    )
    _migrate_stage_receipts(connection, cards)
    _rename_credential_folders(file_root, cards)
