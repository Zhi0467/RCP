"""Rendering and execution for package identity and offline migrations."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
import sqlite3
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rcp.build_identity import build_identity
from rcp.server_ops.models import (
    SERVER_CLI_PROTOCOL_VERSION,
    MachineTarget,
    NonsecretField,
    ServerStep,
)
from rcp.storage import AppStore

EXIT_MIGRATION_LOCKED = 1
EXIT_MIGRATION_UNKNOWN = 2


@dataclass(frozen=True)
class _MigrationResult:
    outcome: str
    message: str
    exit_code: int = 0
    ledger_head: int | None = None
    registry_head: int | None = None
    pending: tuple[str, ...] | None = None

    def fields(self) -> tuple[tuple[str, str | int | bool], ...]:
        fields: list[tuple[str, str | int | bool]] = [("outcome", self.outcome)]
        if self.ledger_head is not None:
            fields.append(("ledger_head", self.ledger_head))
        if self.registry_head is not None:
            fields.append(("registry_head", self.registry_head))
        if self.pending is not None:
            fields.append(("pending_migrations", ", ".join(self.pending) or "none"))
        return tuple(fields)


def _print_version(*, machine_readable: bool) -> None:
    identity = build_identity()
    build = str(identity.build) if identity.build is not None else "none"
    commit = identity.commit or "none"
    message = f"rcp {identity.version} build {build} commit {commit}"
    _render_command_result(
        machine_readable=machine_readable,
        command="version",
        title="Report RCP build identity",
        purpose="Identify the exact RCP package build without starting the server.",
        phase="version",
        state="succeeded",
        expected_success="The full version, base version, build, and commit are reported.",
        message=message,
        fields=(
            ("version", identity.version),
            ("base_version", identity.base_version),
            ("build", identity.build if identity.build is not None else "none"),
            ("commit", commit),
            ("outcome", "succeeded"),
        ),
    )


def _run_migrate(
    args: argparse.Namespace,
    data_dir: Path,
    *,
    instance_lock: Callable[[Path], AbstractContextManager[None]],
    lock_held_error: type[Exception],
) -> int:
    try:
        with instance_lock(data_dir):
            result = _run_migrate_locked(data_dir, check=args.check)
    except lock_held_error as exc:
        result = _MigrationResult(
            outcome="locked",
            message=str(exc),
            exit_code=EXIT_MIGRATION_LOCKED,
        )
    except OSError as exc:
        result = _MigrationResult(
            outcome="refused",
            message=f"RCP migration refused: {exc}",
            exit_code=EXIT_MIGRATION_LOCKED,
        )
    _render_migration_result(args, result)
    return result.exit_code


def _run_migrate_locked(data_dir: Path, *, check: bool) -> _MigrationResult:
    database = data_dir / "rcp.sqlite3"
    registry_head = AppStore.storage_schema_registry_head()
    missing = not database.exists() and not database.is_symlink()
    try:
        if missing and check:
            return _MigrationResult(
                outcome="fresh",
                message=(
                    "RCP migration check: fresh; ledger head 0, "
                    f"registry head {registry_head}, no pending migrations."
                ),
                ledger_head=0,
                registry_head=registry_head,
                pending=(),
            )
        if missing:
            pending: tuple[str, ...] = ()
            ledger_head = 0
        else:
            store = AppStore.open_read_only(database)
            ledger_head, registry_head, pending = store.check_storage_schema_migrations()
        if check:
            if pending:
                names = ", ".join(pending)
                return _MigrationResult(
                    outcome="pending",
                    message=(
                        "RCP migration check: pending migrations exist; "
                        f"ledger head {ledger_head}, registry head {registry_head}: {names}."
                    ),
                    ledger_head=ledger_head,
                    registry_head=registry_head,
                    pending=pending,
                )
            return _MigrationResult(
                outcome="current",
                message=(
                    "RCP migration check: current; "
                    f"ledger head {ledger_head}, registry head {registry_head}, "
                    "no pending migrations."
                ),
                ledger_head=ledger_head,
                registry_head=registry_head,
                pending=(),
            )
        store = AppStore(database)
        ledger_head = store.storage_schema_ledger_head()
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return _MigrationResult(
            outcome="unknown",
            message=f"RCP migration refused: unknown storage state: {exc}",
            exit_code=EXIT_MIGRATION_UNKNOWN,
            registry_head=registry_head,
        )
    return _MigrationResult(
        outcome="migrated",
        message=f"RCP migration complete: ledger head {ledger_head}.",
        ledger_head=ledger_head,
        registry_head=registry_head,
        pending=(),
    )


def _render_migration_result(
    args: argparse.Namespace,
    result: _MigrationResult,
) -> None:
    _render_command_result(
        machine_readable=args.machine_readable,
        command="migrate --check" if args.check else "migrate",
        title="Check RCP storage migrations" if args.check else "Apply RCP storage migrations",
        purpose=(
            "Validate storage migration compatibility without changing the database."
            if args.check
            else "Apply every known storage migration without starting the server."
        ),
        phase="migration_check" if args.check else "migration_apply",
        state="succeeded" if result.exit_code == 0 else "failed",
        expected_success=(
            "The schema is current or every pending migration is known."
            if args.check
            else "The schema validates at this RCP build's migration head."
        ),
        message=result.message,
        fields=result.fields(),
    )


def _render_command_result(
    *,
    machine_readable: bool,
    command: str,
    title: str,
    purpose: str,
    phase: str,
    state: str,
    expected_success: str,
    message: str,
    fields: tuple[tuple[str, str | int | bool], ...],
) -> None:
    if not machine_readable:
        print(message, file=sys.stderr if state == "failed" else sys.stdout)
        return
    step = ServerStep(
        number=1,
        title=title,
        purpose=purpose,
        performed_by="system",
        target=MachineTarget(
            host=socket.gethostname(),
            os_account=pwd.getpwuid(os.geteuid()).pw_name,
        ),
        phase=phase,
        state=state,
        expected_success=expected_success,
        message=message,
        fields=tuple(NonsecretField(name=name, value=value) for name, value in fields),
    )
    # These top-level commands reuse the envelope without widening the shipped server command.
    event = {
        "version": SERVER_CLI_PROTOCOL_VERSION,
        "event": "step",
        "command": command,
        "timestamp": datetime.now(UTC).isoformat(),
        "step": step.model_dump(mode="json"),
    }
    print(json.dumps(event, separators=(",", ":")), flush=True)
