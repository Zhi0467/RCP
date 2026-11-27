"""Stage an agent's watcher definitions and apply its edits as one transaction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from rcp.core.models import GraphState
from rcp.limits import (
    TURN_JOURNAL_MAX_WATCHER_BYTES,
    TURN_JOURNAL_MAX_WATCHER_FILES,
)
from rcp.machine_cards import host_of
from rcp.regular_tree_reader import read_regular_tree
from rcp.storage import AppStore, GraphCondition, WatcherCheckRecord, WatcherRecord
from rcp.storage.watchers import _watcher_records
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.watcher_workspace import direct_json_stem as _watcher_id
from rcp.transport.watcher_workspace import replace_agent_folder
from rcp.watchers import (
    WatcherBinding,
    WatcherCheckRunner,
    WatchSpec,
    external_check,
    graph_check,
    run_watcher_check,
    validate_graph_conditions,
    validate_watch_specs,
)


class FileCheck(WatchSpec):
    machine_id: str | None = None

    def identity(self, machine_id: str) -> tuple[str, str, str, str, str | None]:
        return (
            self.machine_id or machine_id,
            self.check_command,
            self.log_path,
            self.cwd,
            self.cancel_command,
        )


class WatcherFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    checks: list[FileCheck] = Field(default_factory=list)
    graph: GraphCondition | None = None

    @model_validator(mode="after")
    def one_definition(self) -> Self:
        if bool(self.checks) == (self.graph is not None):
            raise ValueError("a watcher needs external checks or one graph condition")
        return self


def _file(record: WatcherRecord) -> WatcherFile:
    checks = record.live_checks
    if len(checks) == 1 and checks[0].definition == "graph":
        return WatcherFile(purpose=record.purpose, graph=checks[0].condition)
    return WatcherFile(
        purpose=record.purpose,
        checks=[
            FileCheck.model_validate(
                check.model_dump(
                    include={"machine_id", "check_command", "log_path", "cwd", "cancel_command"},
                    exclude_none=True,
                )
            )
            for check in checks
        ],
    )


def watcher_files(store: AppStore, binding: WatcherBinding) -> dict[str, str]:
    """The live, editable definitions to stage for the next turn."""

    return {
        f"{watcher.watcher_id}.json": _file(watcher).model_dump_json(exclude_none=True, indent=2)
        for watcher in store.agent_watchers(binding.agent_id, open_only=True)
        if watcher.project_id == binding.project_id
        and watcher.state in {"watching", "check_failing"}
    }


def watcher_binding(
    store: AppStore, operation_id: str, agent_id: str, machine_id: str
) -> WatcherBinding:
    task = store.agent_task(operation_id)
    if task is None:
        raise ValueError("Watcher arming requires a durable originating turn.")
    return WatcherBinding(
        project_id=task.project_id,
        agent_id=agent_id,
        origin_turn_id=operation_id,
        graph_target=task.graph_target,
        machine_id=machine_id,
    )


def stage_watcher_folder(
    store: AppStore,
    binding: WatcherBinding,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    *,
    attempt: int = 0,
) -> dict[str, str]:
    """Save the baseline independently of the writable folder on every handoff pass."""
    role = f"watcher_folder:{attempt}"
    saved = store.agent_task_contract(binding.origin_turn_id, role)
    if saved is None:
        files = watcher_files(store, binding)
        saved = json.dumps(files, sort_keys=True)
        store.record_agent_task_contract(
            binding.origin_turn_id, role, saved, hashlib.sha256(saved.encode()).hexdigest()
        )
    else:
        files = json.loads(saved)
    if remote_stage is None:
        replace_agent_folder(workspace, "watchers", files)
    else:
        remote_stage.replace_agent_folder("watchers", files)
    return files


def read_agent_folder(
    workspace: Path, remote_stage: RemoteRunStage | None, folder: str
) -> dict[str, str]:
    """An agent-edited folder's direct files, within the turn journal's watcher bounds."""

    files, size = TURN_JOURNAL_MAX_WATCHER_FILES, TURN_JOURNAL_MAX_WATCHER_BYTES
    tree = (
        read_regular_tree(
            workspace,
            folder,
            max_entries=files,
            max_files=files,
            max_file_bytes=size,
            max_total_bytes=size,
        )
        if remote_stage is None
        else remote_stage.read_workspace_directory(
            folder, max_entries=files, max_files=files, max_file_bytes=size, max_total_bytes=size
        )
    )
    if tree.directories:
        raise ValueError(f"files must be directly inside {folder}/")
    return {name: data.decode("utf-8") for name, data in tree.files.items()}


def read_watcher_folder(workspace: Path, remote_stage: RemoteRunStage | None) -> dict[str, str]:
    return read_agent_folder(workspace, remote_stage, "watchers")


def _external_identity(check: WatcherCheckRecord) -> tuple[str, str, str, str, str | None]:
    assert check.check_command is not None and check.log_path is not None and check.cwd is not None
    return check.machine_id, check.check_command, check.log_path, check.cwd, check.cancel_command


def _applied_folder(
    connection: sqlite3.Connection, operation_id: str, digest: str
) -> tuple[list[WatcherRecord], list[str]] | None:
    row = connection.execute(
        "SELECT payload_json FROM graph_run_receipts WHERE operation_id = ? "
        "AND category = 'watcher_folder_applied' "
        "AND json_extract(payload_json, '$.sha256') = ? LIMIT 1",
        (operation_id, digest),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    ids = payload["watcher_ids"]
    records = (
        _watcher_records(connection, f"w.watcher_id IN ({','.join('?' * len(ids))})", ids)
        if ids
        else []
    )
    return records, payload.get("cancelled_ids", [])


@dataclass
class WatcherFolderChange:
    binding: WatcherBinding
    digest: str
    original: dict[str, WatcherFile]
    additions: list[WatcherRecord]
    edits: list[tuple[str, str, list[str], list[WatcherCheckRecord]]]
    cancellations: list[str]
    applied: list[WatcherRecord] | None = None

    @property
    def changed_ids(self) -> list[str]:
        if self.applied is not None:
            return [record.watcher_id for record in self.applied]
        return [record.watcher_id for record in self.additions] + [item[0] for item in self.edits]

    def commit(self, store: AppStore, connection: sqlite3.Connection) -> list[WatcherRecord]:
        """Apply observations, edits, cancellation and the retry receipt together."""
        previous = _applied_folder(connection, self.binding.origin_turn_id, self.digest)
        if previous is not None:
            return previous[0]
        if not (self.edits or self.additions or self.cancellations):
            return []
        for name, definition in self.original.items():
            watcher_id = _watcher_id(name)
            matching = _watcher_records(
                connection,
                "w.watcher_id = ? AND w.agent_id = ? AND w.project_id = ?",
                (watcher_id, self.binding.agent_id, self.binding.project_id),
            )
            if (
                not matching
                or not matching[0].open
                or matching[0].state == "finished"
                or _file(matching[0]) != definition
            ):
                raise ValueError(f"watcher {watcher_id} is no longer live or has changed")
        if self.cancellations:
            store.cancel_watchers(self.cancellations, by="agent", connection=connection)
        for watcher_id, purpose, removed_ids, new_checks in self.edits:
            store.revise_watcher(
                connection,
                watcher_id,
                purpose=purpose,
                removed_check_ids=removed_ids,
                new_checks=new_checks,
                at=store.now(),
            )
        for watcher in self.additions:
            store.arm_watcher(watcher, connection=connection)
        touched = self.changed_ids
        store._insert_agent_task_receipt(
            connection,
            self.binding.origin_turn_id,
            "watcher_folder_applied",
            json.dumps(
                {"sha256": self.digest, "watcher_ids": touched, "cancelled_ids": self.cancellations}
            ),
            tier="summary",
            created_at=store.now(),
        )
        return (
            _watcher_records(
                connection, f"w.watcher_id IN ({','.join('?' * len(touched))})", touched
            )
            if touched
            else []
        )


def prepare_watcher_files(
    store: AppStore,
    binding: WatcherBinding,
    baseline: Mapping[str, str],
    files: Mapping[str, str],
    *,
    state: GraphState | None = None,
    check_runner: WatcherCheckRunner = run_watcher_check,
) -> WatcherFolderChange:
    """Validate edits and observe new checks before acquiring the writer lock."""

    digest = hashlib.sha256(
        json.dumps([dict(baseline), dict(files)], sort_keys=True).encode("utf-8")
    ).hexdigest()
    with store.connection() as connection:
        previous = _applied_folder(connection, binding.origin_turn_id, digest)
        if previous is not None:
            return WatcherFolderChange(binding, digest, {}, [], [], previous[1], previous[0])
    existing = {
        record.watcher_id: record
        for record in store.agent_watchers(binding.agent_id)
        if record.project_id == binding.project_id
    }
    wanted = {name: WatcherFile.model_validate_json(raw) for name, raw in files.items()}
    original = {name: WatcherFile.model_validate_json(raw) for name, raw in baseline.items()}
    seen: set[tuple[object, ...]] = set()
    for name, definition in wanted.items():
        _watcher_id(name)
        keys: list[tuple[object, ...]] = [
            item.identity(binding.machine_id) for item in definition.checks
        ]
        if definition.graph:
            keys.append((binding.graph_target.key, definition.graph.model_dump_json()))
        for key in keys:
            if key in seen:
                raise ValueError(f"duplicate check in watcher file {name}")
            seen.add(key)
    changed = sorted(
        name for name in original.keys() | wanted.keys() if original.get(name) != wanted.get(name)
    )
    created_at = store.now()
    additions: list[WatcherRecord] = []
    edits: list[tuple[str, str, list[str], list[WatcherCheckRecord]]] = []
    cancellations: list[str] = []
    for name in changed:
        watcher_id = _watcher_id(name)
        if name not in original and store.watcher(watcher_id) is not None:
            raise ValueError(f"watcher id {watcher_id} is already in use")
        prior = existing.get(watcher_id) if name in original else None
        if name in original and (prior is None or not prior.open or prior.state == "finished"):
            raise ValueError(f"watcher {watcher_id} is no longer live")
        if name not in wanted:
            cancellations.append(watcher_id)
            continue
        definition = wanted[name]
        old = (
            {_external_identity(c): c for c in prior.live_checks if c.definition == "external"}
            if prior
            else {}
        )
        if prior and original[name].graph != definition.graph:
            cancellations.append(watcher_id)
            watcher_id = str(uuid5(NAMESPACE_URL, f"{binding.origin_turn_id}:replace:{watcher_id}"))
            prior = None
            old = {}
        new_checks: list[WatcherCheckRecord] = []
        if definition.graph is not None and prior is None:
            if state is None:
                raise ValueError("a graph watcher needs the current graph state")
            validate_graph_conditions([definition.graph], state)
            new_checks.append(
                graph_check(
                    watcher_id,
                    definition.graph,
                    state,
                    graph_target=binding.graph_target,
                    machine_id=binding.machine_id,
                    created_at=created_at,
                )
            )
        for item in definition.checks:
            key = item.identity(binding.machine_id)
            if old.pop(key, None) is not None:
                continue
            if key[0] != binding.machine_id:
                raise ValueError("a new watcher check must use this turn's machine")
            (result,) = validate_watch_specs(
                [item], host_of(store, binding.machine_id), check_runner=check_runner
            )
            check_id = str(
                uuid5(NAMESPACE_URL, json.dumps([binding.origin_turn_id, watcher_id, *key]))
            )
            new_checks.append(
                external_check(check_id, item, result, machine_id=key[0], created_at=created_at)
            )
        if prior is None:
            additions.append(
                WatcherRecord(
                    watcher_id=watcher_id,
                    agent_id=binding.agent_id,
                    project_id=binding.project_id,
                    purpose=definition.purpose,
                    origin_turn_id=binding.origin_turn_id,
                    created_at=created_at,
                    checks=new_checks,
                )
            )
        else:
            edits.append(
                (
                    watcher_id,
                    definition.purpose,
                    [check.check_id for check in old.values()],
                    new_checks,
                )
            )
    return WatcherFolderChange(
        binding,
        digest,
        {name: original[name] for name in changed if name in original},
        additions,
        edits,
        cancellations,
    )


def reconcile_watcher_files(
    store: AppStore,
    binding: WatcherBinding,
    baseline: Mapping[str, str],
    files: Mapping[str, str],
    *,
    state: GraphState | None = None,
    check_runner: WatcherCheckRunner = run_watcher_check,
) -> list[WatcherRecord]:
    change = prepare_watcher_files(
        store, binding, baseline, files, state=state, check_runner=check_runner
    )
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return change.commit(store, connection)
