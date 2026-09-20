"""Member shell lifetime and metadata; shell bytes are never stored on disk."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import logging
import os
import signal
import struct
import subprocess
import termios
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from rcp.agents.write_scope import RegisteredRepositoryRoot
from rcp.config import MachineConfig, Manifest
from rcp.limits import (
    TERMINAL_IDLE_TIMEOUT_SECONDS,
    TERMINAL_MAX_DIMENSION,
    TERMINAL_POLL_INTERVAL_SECONDS,
    TERMINAL_STARTUP_RECONCILE_TIMEOUT_SECONDS,
    TERMINAL_SUBSCRIBER_QUEUE_SIZE,
)
from rcp.terminals import launch, remote
from rcp.terminals.backends import TerminalCapability, machine_capability
from rcp.terminals.models import (
    TerminalFrame,
    TerminalRuntime,
    TerminalSession,
    TerminalUnavailable,
)
from rcp.terminals.probe import TerminalProbe, TerminalProbeCache
from rcp.terminals.runtime import (
    end_runtime,
    get_runtime,
    read_ready,
    release_runtime,
    sweep_loop,
)
from rcp.terminals.utilities import resolve_repository, save_metadata, timestamp
from rcp.transport.run_stage import RemoteRunStage

logger = logging.getLogger(__name__)


def manifest_registration(manifest: Manifest, repository_alias: str) -> tuple[str, str, str, str]:
    """What this alias currently names: path, machine, host and account."""
    repository = manifest.repository_map[repository_alias]
    machine = manifest.machine_map[repository.machine]
    return (repository.path, repository.machine, machine.host, machine.os_account)


def manifest_checkout(manifest: Manifest, repository_alias: str) -> tuple[str, str, str]:
    """The working tree this alias names: path, host and account.

    A machine alias is the label RCP gives a host, not part of what makes two
    things the same working tree, so it is absent here although
    `manifest_registration` keeps it. Asking whether an alias still names what
    a session opened on and asking whether a record speaks for the tree about
    to be opened are different questions, and a rename answers them
    differently.
    """
    repository = manifest.repository_map[repository_alias]
    machine = manifest.machine_map[repository.machine]
    return (repository.path, machine.host, machine.os_account)


def session_checkout(session: TerminalSession) -> tuple[str, str, str]:
    """The working tree this session opened on, as it recorded it."""
    return (session.declared_path, session.execution_host, session.declared_account)


# Every field of `TerminalSession` holds a string; two of them may be null
# instead. `test_every_terminal_record_field_holds_a_string` fails if that
# stops being true, because this check would then refuse valid records.
_NULLABLE_RECORD_FIELDS = {"ended_at", "termination_reason"}
_RECORD_FIELDS = frozenset(field.name for field in dataclasses.fields(TerminalSession))


def _record_is_complete(payload: object) -> bool:
    """Whether a record names every field this version writes.

    `asdict` writes them all, so a record missing one was not written by this
    application. The dataclass would default it silently, and a default is a
    claim: an absent `execution_host` says local, and cleaning a remote record
    up against a local unit that was never there reports success and retires
    it while its own unit still runs.
    """
    return isinstance(payload, dict) and payload.keys() >= _RECORD_FIELDS


def _record_values_are_well_typed(payload: object) -> bool:
    """Whether a persisted record's values are the kind its fields declare."""
    if not isinstance(payload, dict):
        return False
    for name, value in payload.items():
        if value is None and name in _NULLABLE_RECORD_FIELDS:
            continue
        if not isinstance(value, str):
            return False
    return True


def _readable_record(payload: object) -> tuple[TerminalSession | None, str]:
    """This version's view of a record, or why it cannot read it.

    A record naming every field this version knows plus one it does not is
    still a record of a shell that may be running, so a caller that must block
    on it is told what was wrong rather than left to guess.
    """
    if not _record_is_complete(payload):
        return None, "does not name every field this version writes"
    if not _record_values_are_well_typed(payload):
        return None, "holds values of the wrong kind"
    try:
        # TerminalSession is a dataclass, so a record written by another
        # version raises TypeError rather than a validation error.
        return TerminalSession(**payload), ""
    except TypeError as exc:
        return None, f"is not this version's: {exc}"


def registration_lapse(manifest: Manifest, session: TerminalSession) -> str | None:
    """Why this session's alias no longer names it, or None while it still does."""
    if session.repository_id not in manifest.repository_map:
        return "repository_unregistered"
    if session_registration(session) != manifest_registration(manifest, session.repository_id):
        return "repository_repointed"
    return None


def session_registration(session: TerminalSession) -> tuple[str, str, str, str]:
    """What the alias named when this session started."""
    return (
        session.declared_path,
        session.declared_machine,
        session.execution_host,
        session.declared_account,
    )


class TerminalManager:
    def __init__(self, data_dir: Path, membership_check: Callable[[str, str], bool]) -> None:
        self.probes = TerminalProbeCache()
        self.data_dir = data_dir
        self.directory = data_dir / "terminals"
        self.membership_check = membership_check
        self.sessions: dict[str, TerminalRuntime] = {}
        self._opening: set[tuple[str, str]] = set()
        # Records left unfinished because a unit could not be confirmed gone.
        self._unresolved: dict[tuple[str, str], list[TerminalSession]] = {}
        self._local_capability: TerminalCapability | None = None
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        self._started = False
        self._unit_prefix = (
            "rcp-terminal-" + hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
        )

    async def _settle_registration(
        self, project_id: str, manifest: Manifest, repository_alias: str
    ) -> TerminalSession | None:
        """The session this alias still names, retiring one it no longer does.

        The caller holds the manager lock. An alias that names another path,
        machine or account has stopped naming what its session opened on, so
        handing that session back would answer a request for one checkout with
        a shell somewhere else.
        """
        for runtime in list(self.sessions.values()):
            session = runtime.session
            if session.project_id != project_id or session.repository_id != repository_alias:
                continue
            if runtime.retiring:
                # Its stop failed, so its shell may still own the checkout.
                # Handing it back would offer a session already ending, and
                # opening another would put two shells on one working tree.
                raise TerminalUnavailable(
                    "An earlier terminal for this repository could not be stopped, so its "
                    "shell may still be running. Opening another would put two on one "
                    "checkout; it is retried on its own."
                )
            reason = registration_lapse(manifest, session)
            if reason is None:
                return session
            await end_runtime(self, runtime, reason)
            return None
        return None

    async def retire_repointed(
        self, project_id: str, manifest: Manifest, repository_alias: str
    ) -> None:
        """Settle one alias, for a caller whose answer is about that alias.

        An open request uses this rather than settling the whole project: it
        must not make a member wait on the machines of aliases its answer does
        not mention. The polling list settles those.
        """
        async with self._lock:
            await self._settle_registration(project_id, manifest, repository_alias)

    async def reconcile_registrations(self, project_id: str, manifest: Manifest) -> None:
        """Retire this project's sessions whose alias has stopped naming them.

        Nothing recomputes a registration on its own. `open` settles the alias
        it was asked for, and the member whose repository was repointed or
        unregistered while their shell ran is not going to ask: the stale
        session is exactly what hides the control that would replace it. So
        the projections a member reaches a session through settle it instead.

        They are retired together. Every caller here is a member waiting on a
        listing, an open or an attach, and a stale alias whose machine has
        gone costs a stop timeout, so retiring them in turn would spend one
        per alias before answering. One that cannot be stopped keeps its
        session and is retried by the next call, which is what a failed stop
        does everywhere here.
        """
        async with self._lock:
            stale = []
            for runtime in list(self.sessions.values()):
                if runtime.session.project_id != project_id:
                    continue
                reason = registration_lapse(manifest, runtime.session)
                if reason is not None:
                    stale.append((runtime, reason))
            if not stale:
                return
            failures = await self._end_each(stale)
        for runtime, failure in failures:
            logger.warning(
                "Terminal session for repository %s could not be retired after its "
                "registration changed; it is retried: %s",
                runtime.session.repository_id,
                failure,
            )

    async def _record_launch_failure(
        self, session: TerminalSession, reason: str = "launch_failed"
    ) -> None:
        """Finish or retain an unopened session's record; never leave it untracked.

        The intent is persisted before the launch, and an unfinished record is
        both what startup reconciles and what blocks a reopen. A mirrored
        launch may have created its unit before failing, so the record is
        finished only once that unit is known to be gone. The caller names the
        reason, because a launch that failed and one that was cancelled after
        succeeding end for different reasons and the record is the audit.
        """
        session.termination_reason = reason
        if await self.unit_confirmed_gone(session):
            session.ended_at = timestamp()
        else:
            self.mark_unresolved(session)
        save_metadata(self.directory, session)

    def mark_unresolved(self, session: TerminalSession) -> None:
        """Remember that this repository may still have a shell on it.

        A repository can be spoken for by more than one record: a unit that
        outlived a restart and a record this version cannot read both claim
        it, and keeping only the last would unblock the repository as soon as
        that one was confirmed gone, while the other may still be running.

        `session_id` is what distinguishes them, and every caller derives it
        from the record's own file rather than from anything the record
        claims, so reconciling one record twice replaces its blocker while
        two records never share one.
        """
        retained = self._unresolved.setdefault((session.project_id, session.repository_id), [])
        for index, existing in enumerate(retained):
            if existing.session_id == session.session_id:
                retained[index] = session
                return
        retained.append(session)

    async def _resolve_unfinished(
        self, project_id: str, manifest: Manifest, repository_alias: str
    ) -> None:
        """Retry every retained record that still speaks for this checkout.

        A retained record means a unit may still own the checkout. Opening a
        second shell there would let two of them write one working tree, which
        the one-session-per-repository rule exists to prevent.

        The alias alone does not find them all. Settings can drop an alias and
        register the same checkout under another name, and the old alias's
        blocker then names a unit on the very working tree the new alias is
        about to open. The tree itself — path, host and account — identifies
        it across that rename, and across a rename of the machine alias too,
        which is a label rather than part of what makes two things one tree.
        A record this version could not read declares nothing, so its alias is
        all it has, which is why both are consulted.
        """
        target = (
            manifest_checkout(manifest, repository_alias)
            if repository_alias in manifest.repository_map
            else None
        )
        speaking = [
            (key, session)
            for key, retained in self._unresolved.items()
            for session in retained
            if key[0] == project_id
            and (
                key[1] == repository_alias
                or (target is not None and session_checkout(session) == target)
            )
        ]
        if not speaking:
            return
        # Confirmed together: this is a member waiting to open, and a record
        # whose machine has gone costs a stop timeout apiece.
        confirmed = await asyncio.gather(
            *(self.unit_confirmed_gone(session) for _, session in speaking)
        )
        refused = False
        for (key, session), gone in zip(speaking, confirmed, strict=True):
            if not gone:
                # One record still speaking for this checkout is enough.
                refused = True
                continue
            session.ended_at = timestamp()
            # A record retained by startup carries no reason yet, and one
            # retained by a failed launch already carries its own. Finishing
            # late must leave the same durable reason as finishing on the
            # first attempt would have.
            session.termination_reason = session.termination_reason or "server_restart"
            save_metadata(self.directory, session)
            remaining = [
                item
                for item in self._unresolved.get(key, [])
                if item.session_id != session.session_id
            ]
            if remaining:
                self._unresolved[key] = remaining
            else:
                self._unresolved.pop(key, None)
        if refused:
            raise TerminalUnavailable(
                "An earlier terminal for this repository could not be stopped on its "
                "execution machine, so its shell may still be running. Opening another "
                "is refused until that one is gone."
            )

    async def capability(
        self, machine: MachineConfig, probe: TerminalProbe | None
    ) -> TerminalCapability:
        """Answer a machine's terminal capability, caching the local one.

        A local answer spawns three probe processes and cannot change while
        this process runs, but the repositories view polls it every few
        seconds. Refresh is what re-reads it, alongside the remote probes.
        """
        if machine.host:
            return await asyncio.to_thread(machine_capability, machine, probe)
        if self._local_capability is None:
            self._local_capability = await asyncio.to_thread(machine_capability, machine, None)
        return self._local_capability

    def invalidate_local_capability(self) -> None:
        self._local_capability = None

    async def _reconcile_guarded(self, path: Path) -> None:
        """Reconcile one record, and never let it cost the server its boot.

        A record can be truncated, carry fields this version does not know, or
        carry values of the wrong type entirely; a dataclass enforces none of
        that. Guarding the whole record ends that class rather than naming each
        way one can be malformed. An unreconciled record stays put.

        Failing to reconcile a record is not evidence that its shell is gone,
        so the repository is blocked as well. A record can fail here in ways
        no branch below anticipates — a destination `ssh` refuses, a stop that
        raises something unlisted — and every one of them leaves a unit this
        startup could not account for.
        """
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            # Nothing was read, so nothing names a repository to block.
            logger.warning("Terminal record %s cannot be read; leaving it: %s", path.name, exc)
            return
        try:
            await self._reconcile_record(path, payload)
        except Exception as exc:
            logger.warning(
                "Terminal record %s could not be reconciled; leaving it and blocking "
                "its repository: %s",
                path.name,
                exc,
            )
            self._block_unreadable_repository(path, payload)

    def _block_record_path(self, path: Path) -> None:
        """Block whatever repository this record names, reading it afresh.

        A reconciliation that ran out of the startup budget was never shown to
        be unreadable; only its stop was slow. Retaining what the record
        actually says is what lets `unit_confirmed_gone` retry that stop on a
        later attempt and release the repository, which is the retry the
        budget promises. The opaque blocker nothing can ever confirm gone is
        for a record this version genuinely cannot read.
        """
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        session, _unreadable = _readable_record(payload)
        if session is None or session.session_id != path.stem:
            # A record naming another session is not this file's to speak for;
            # `_reconcile_record` says why.
            self._block_unreadable_repository(path, payload)
            return
        if session.ended_at:
            # It finished inside the budget after all, so nothing is blocked.
            return
        self.mark_unresolved(session)

    def _block_unreadable_repository(self, path: Path, payload: object) -> None:
        """Keep a record this version cannot read from yielding a second shell.

        A record naming every field this version knows plus one it does not is
        still a record of a shell that may be running. Only its identity is
        trusted here, because the rest is this version reading another
        version's file. `unit_confirmed_gone` treats a containment outside the
        two known ones as never confirmable, so the repository stays blocked
        until the record is resolved by hand or by the version that wrote it.
        """
        if not isinstance(payload, dict):
            return
        project_id = payload.get("project_id")
        repository_id = payload.get("repository_id")
        if not (isinstance(project_id, str) and project_id):
            return
        if not (isinstance(repository_id, str) and repository_id):
            return
        self.mark_unresolved(
            TerminalSession(
                # The file, not the identifier inside it. A record this
                # version cannot read can claim any `session_id`, including
                # one another record already holds, and two blockers sharing
                # an identity means resolving either releases the repository
                # from both. One file is one record, so one file is one
                # blocker.
                session_id=path.stem,
                project_id=project_id,
                member_id="",
                repository_id=repository_id,
                path="",
                started_at="",
                last_activity_at="",
                unit="",
                containment="",  # type: ignore[arg-type]
            )
        )

    async def _reconcile_record(self, path: Path, payload: object) -> None:
        """Retire, retain or block one persisted record, or raise to its guard."""
        # A mistyped value that happens to be falsey reads as an ordinary
        # empty one rather than raising — an `execution_host` of `[]` makes a
        # remote record look local and get cleaned up against a local unit
        # that was never there — and the per-record guard only catches records
        # that raise. An unreadable record can still say which repository it
        # belongs to, so it blocks that one.
        session, unreadable = _readable_record(payload)
        if session is None:
            logger.warning(
                "Terminal record %s %s; leaving it and blocking its repository.",
                path.name,
                unreadable,
            )
            self._block_unreadable_repository(path, payload)
            return
        if session.ended_at:
            return
        if session.session_id != path.stem:
            # Every record this application writes is named for the session in
            # it. One that is not was damaged or written by something else, and
            # acting on it would retire a unit under another record's name and
            # overwrite that record — including an unfinished one whose whole
            # job is to keep a repository blocked. It may still describe a live
            # shell, so it blocks its own repository instead.
            logger.warning(
                "Terminal record %s carries session id %r; leaving it and blocking its repository.",
                path.name,
                session.session_id,
            )
            self._block_unreadable_repository(path, payload)
            return
        if session.containment not in {"mirrored", "cooperative"}:
            # A dataclass does not enforce its Literal, so a version-skewed
            # value would otherwise skip the unit stop and then be retired,
            # hiding a possible mirrored shell from every later startup.
            logger.warning(
                "Terminal record %s has containment %r this version does not know; leaving it.",
                session.session_id,
                session.containment,
            )
            # It may name a running unit, so it also blocks its repository.
            self.mark_unresolved(session)
            return
        if session.unit != f"{self._unit_prefix}-{session.session_id}":
            # Another data directory wrote this record. Its unit is not ours
            # to stop, and refusing to boot would need a hand deletion.
            logger.warning(
                "Terminal record %s names unit %s, which this data directory does not own.",
                session.session_id,
                session.unit,
            )
            session.ended_at = timestamp()
            session.termination_reason = "unit_identity_mismatch"
            save_metadata(self.directory, session)
            return
        if session.containment == "mirrored":
            try:
                if session.execution_host:
                    await asyncio.to_thread(
                        remote.stop_remote_unit,
                        session.execution_host,
                        session.unit,
                        session.declared_account,
                    )
                else:
                    await asyncio.to_thread(launch.stop_unit, session.unit)
            except (
                TerminalUnavailable,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                # The unfinished record is itself the retry. Keep it.
                logger.warning(
                    "Could not stop terminal unit %s; a later startup retries it: %s",
                    session.unit,
                    exc,
                )
                self.mark_unresolved(session)
                return
        session.ended_at = timestamp()
        session.termination_reason = "server_restart"
        save_metadata(self.directory, session)

    async def start(self) -> None:
        if self._started:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.directory / "empty").mkdir(exist_ok=True, mode=0o500)
        # Only this application's persisted unit names are stopped. Persisting
        # intent before launch also covers a crash during systemd admission.
        # Reconciliation is best effort per record. This runs before every other
        # startup owner, so a stale record, a moved data directory, or an
        # unreachable machine must never be able to refuse the server a boot.
        # Records are reconciled together. Shutdown deliberately leaves every
        # live remote session unfinished, so a machine that went away takes a
        # remote stop timeout per record, and in turn that is startup spent
        # before any other owner runs. They touch separate files and separate
        # repositories, so they are safe to run at once.
        #
        # At once is not all at the same time: each stop is a blocking call in
        # the shared thread pool, so enough of them queue into timeout-sized
        # batches anyway. The budget is what actually bounds the boot. A
        # record still running when it expires keeps its record and blocks its
        # repository, exactly as one that raised would.
        started = {
            asyncio.create_task(self._reconcile_guarded(path)): path
            for path in self.directory.glob("*.json")
        }
        if started:
            _, unfinished = await asyncio.wait(
                started, timeout=TERMINAL_STARTUP_RECONCILE_TIMEOUT_SECONDS
            )
            for task in unfinished:
                task.cancel()
                path = started[task]
                logger.warning(
                    "Terminal record %s did not reconcile within the startup budget; "
                    "leaving it and blocking its repository.",
                    path.name,
                )
                self._block_record_path(path)
            if unfinished:
                # Cancelling a task waiting on a worker thread returns at
                # once; the thread finishes its own stop with nobody reading.
                await asyncio.gather(*unfinished, return_exceptions=True)
        self._started = True
        self._sweeper = asyncio.create_task(sweep_loop(self))

    async def close(self) -> None:
        self._started = False
        if self._sweeper:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        async with self._lock:
            failures = await self._end_each(
                [(runtime, "server_shutdown") for runtime in self.sessions.values()]
            )
        errors = [str(failure) for _, failure in failures]
        self._started = False
        await self.probes.close()
        if errors:
            raise TerminalUnavailable("; ".join(errors))

    async def _end_each(
        self, endings: list[tuple[TerminalRuntime, str]]
    ) -> list[tuple[TerminalRuntime, Exception]]:
        """End these sessions at once rather than one timeout after another.

        A single stop can spend its own timeout against a machine that has
        become unreachable, and every caller here has something waiting behind
        it. Shutdown and the update boundary hold the instance lock until they
        return, and a replacement server waits only
        `SERVER_SHUTDOWN_TIMEOUT_SECONDS` for the old one to go; a member is
        waiting on the listing, open or attach that settles registrations; and
        the lifecycle sweep cannot look at the next expired shell until this
        one is dealt with, so one unreachable machine would hold back retiring
        every other. Each ending touches only its own runtime, and the two
        manager dictionaries they write are keyed per session, so they can run
        together.

        Callers hold the manager lock; ending concurrently does not widen what
        else may run. Each ending is reported against its own runtime, because
        the caller names the failure by what it was ending.
        """
        outcomes = await asyncio.gather(
            *(end_runtime(self, runtime, reason) for runtime, reason in endings),
            return_exceptions=True,
        )
        failures = []
        for (runtime, _), outcome in zip(endings, outcomes, strict=True):
            # `gather` returns a cancellation rather than raising it. Only the
            # ordinary failures are collected here, as a serial loop's
            # `except Exception` did.
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, Exception):
                failures.append((runtime, outcome))
        return failures

    async def open(
        self,
        *,
        project_id: str,
        member_id: str,
        manifest: Manifest,
        repository_alias: str,
        repository_inventory: list[RegisteredRepositoryRoot],
        git_read_paths: tuple[str, ...] = (),
        git_environment: dict[str, str] | None = None,
        remote_git_key_relative: str | None = None,
    ) -> TerminalSession:
        key = (project_id, repository_alias)
        async with self._lock:
            if not self._started:
                raise TerminalUnavailable("Terminal manager has not completed startup cleanup.")
            if not self.membership_check(project_id, member_id):
                raise PermissionError("Project membership is required to open a terminal.")
            existing = await self._settle_registration(project_id, manifest, repository_alias)
            if existing is not None:
                return existing
            if key in self._opening:
                raise TerminalUnavailable("A terminal for this repository is already opening.")
            self._opening.add(key)
        try:
            return await self._open(
                project_id=project_id,
                member_id=member_id,
                manifest=manifest,
                repository_alias=repository_alias,
                repository_inventory=repository_inventory,
                git_read_paths=git_read_paths,
                git_environment=git_environment,
                remote_git_key_relative=remote_git_key_relative,
            )
        finally:
            self._opening.discard(key)

    async def _open(
        self,
        *,
        project_id: str,
        member_id: str,
        manifest: Manifest,
        repository_alias: str,
        repository_inventory: list[RegisteredRepositoryRoot],
        git_read_paths: tuple[str, ...],
        git_environment: dict[str, str] | None,
        remote_git_key_relative: str | None,
    ) -> TerminalSession:
        """Probe, resolve and launch without the manager lock.

        One unreachable machine can cost a probe timeout plus a launch timeout.
        Holding the lock across that would stall every other project's open,
        end and sweep behind it, so only admission and publication take it.
        """
        await self._resolve_unfinished(project_id, manifest, repository_alias)
        repository = manifest.repository_map[repository_alias]
        machine = manifest.machine_map[repository.machine]
        probe = await self.probes.ensure(machine) if machine.host else None
        capability = await self.capability(machine, probe)
        if capability.backend is None:
            raise TerminalUnavailable(capability.reason)
        root, protected = await asyncio.to_thread(
            resolve_repository,
            manifest=manifest,
            project_id=project_id,
            repository_id=repository_alias,
            inventory=repository_inventory,
            data_dir=self.data_dir,
            remote_stage=RemoteRunStage(machine.host) if machine.host else None,
        )
        session_id = uuid.uuid4().hex
        session = TerminalSession(
            session_id=session_id,
            project_id=project_id,
            member_id=member_id,
            repository_id=repository_alias,
            path=str(root),
            declared_path=repository.path,
            declared_machine=repository.machine,
            declared_account=machine.os_account,
            started_at=timestamp(),
            last_activity_at=timestamp(),
            unit=f"{self._unit_prefix}-{session_id}",
            containment=capability.backend.containment,
            execution_host=machine.host,
        )
        save_metadata(self.directory, session)
        if machine.host:
            start = asyncio.to_thread(
                remote.start_remote,
                machine.host,
                unit=session.unit,
                repository=root,
                protected_paths=protected,
                containment=session.containment,
                expand_environment_option=(probe.expand_environment_option if probe else True),
                git_key_relative=remote_git_key_relative,
                os_account=machine.os_account,
            )
        else:
            start = asyncio.to_thread(
                capability.backend.start,
                unit=session.unit,
                repository=root,
                protected_paths=protected,
                git_read_paths=git_read_paths,
                git_environment=git_environment or {},
                empty_directory=self.directory / "empty",
            )
        pending = asyncio.create_task(start)
        try:
            process, master_fd = await asyncio.shield(pending)
        except asyncio.CancelledError as cancelled:
            # A disconnected request cannot abandon a launch still running
            # in its worker thread. Finish admission, then stop its unit.
            try:
                process, master_fd = await pending
            except Exception:
                # It failed after all. This raises inside the cancellation
                # handler and so cannot reach the launch-failure branch below,
                # which would leave its record untracked while `_opening` is
                # cleared — enough for the next attempt to start a second
                # shell. Clean up here and let the cancellation stand.
                await self._record_launch_failure(session)
                raise cancelled from None
            await self._abandon_opening(
                TerminalRuntime(session, process, master_fd, time.monotonic())
            )
            raise
        except Exception:
            await self._record_launch_failure(session)
            raise
        runtime = TerminalRuntime(session, process, master_fd, time.monotonic())
        if machine.host:
            runtime.completion = remote.CompletionParser()
        try:
            async with self._lock:
                self.sessions[session_id] = runtime
                asyncio.get_running_loop().add_reader(master_fd, read_ready, runtime)
                if not self.membership_check(project_id, member_id):
                    await end_runtime(self, runtime, "membership_lost")
                    raise PermissionError(
                        "Project membership ended while the terminal was opening."
                    )
        except asyncio.CancelledError:
            # The shell is already running, and this waits for a lock another
            # end may hold across a `systemctl` call. A requester that leaves
            # during that wait publishes nothing, so without this the shell
            # and its record are both abandoned while `_opening` is cleared,
            # and the next attempt opens a second one on the same checkout.
            await self._abandon_opening(runtime)
            raise
        return session

    async def _abandon_opening(self, runtime: TerminalRuntime) -> None:
        """Stop, retain and release a session its requester no longer wants.

        Reached once the launch has produced a shell: from a cancellation
        while the launch was still in its worker thread, and from one while
        publication waited for the manager lock.
        """
        try:
            async with self._lock:
                # An unpublished runtime has no reader, so reuse would hand
                # back a session whose output never arrives, and a stop that
                # fails would strand it in `sessions` where nothing retires
                # it. `end_runtime` tolerates a runtime that was never added.
                await end_runtime(self, runtime, "opening_cancelled")
        except Exception:
            # The unit outlived the launch this cancellation stopped. As with
            # a failed launch, the record is the only thing that can block a
            # reopen before the next startup reconciles it. The runtime is
            # still given back: the record retains the unit, not this
            # descriptor and process.
            await self._record_launch_failure(runtime.session, "opening_cancelled")
            await release_runtime(self, runtime)

    async def unit_confirmed_gone(self, session: TerminalSession) -> bool:
        """Whether this session's record can be finished rather than retained.

        A cooperative session has no unit. A mirrored one may still have its
        own, and neither the local launcher's cleanup nor a remote supervisor's
        hangup is acknowledged, so it is stopped here to find out.
        """
        if session.containment == "cooperative":
            return True
        if session.containment != "mirrored":
            # This version cannot know what this record left running, so it can
            # never be confirmed gone and never stops blocking its repository.
            return False
        try:
            if session.execution_host:
                await asyncio.to_thread(
                    remote.stop_remote_unit,
                    session.execution_host,
                    session.unit,
                    session.declared_account,
                )
            else:
                await asyncio.to_thread(launch.stop_unit, session.unit)
        except (TerminalUnavailable, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            logger.warning(
                "Terminal unit %s may still be running; it is retried before a reopen: %s",
                session.unit,
                exc,
            )
            return False
        return True

    async def end_all(self, reason: str = "server_maintenance") -> None:
        async with self._lock:
            failures = await self._end_each(
                [(runtime, reason) for runtime in self.sessions.values()]
            )
        if failures:
            raise TerminalUnavailable("; ".join(str(failure) for _, failure in failures))

    def list(self, project_id: str) -> list[TerminalSession]:
        return [
            runtime.session
            for runtime in self.sessions.values()
            if runtime.session.project_id == project_id and not runtime.retiring
        ]

    def get(self, project_id: str, session_id: str) -> TerminalSession:
        return get_runtime(self, project_id, session_id).session

    async def end(self, project_id: str, session_id: str, reason: str = "ended") -> None:
        async with self._lock:
            await end_runtime(self, get_runtime(self, project_id, session_id), reason)

    def attach(self, project_id: str, session_id: str) -> asyncio.Queue[TerminalFrame]:
        runtime = get_runtime(self, project_id, session_id)
        queue: asyncio.Queue[TerminalFrame] = asyncio.Queue(TERMINAL_SUBSCRIBER_QUEUE_SIZE)
        if runtime.replay:
            queue.put_nowait(bytes(runtime.replay))
        runtime.subscribers.add(queue)
        runtime.session.state = "live"
        return queue

    def detach(self, project_id: str, session_id: str, queue: asyncio.Queue[TerminalFrame]) -> None:
        runtime = self.sessions.get(session_id)
        if runtime and runtime.session.project_id == project_id:
            runtime.subscribers.discard(queue)
            if not runtime.subscribers:
                runtime.session.state = "idle"

    async def write(self, project_id: str, session_id: str, data: bytes) -> None:
        runtime = get_runtime(self, project_id, session_id)
        view = memoryview(data)
        while view:
            try:
                sent = os.write(runtime.master_fd, view)
                view = view[sent:]
            except BlockingIOError:
                await asyncio.sleep(TERMINAL_POLL_INTERVAL_SECONDS)
                if get_runtime(self, project_id, session_id) is not runtime:
                    raise KeyError("Terminal session ended during input.") from None
        runtime.last_activity = time.monotonic()
        runtime.session.last_activity_at = timestamp()

    def resize(self, project_id: str, session_id: str, cols: int, rows: int) -> None:
        if not (1 <= cols <= TERMINAL_MAX_DIMENSION and 1 <= rows <= TERMINAL_MAX_DIMENSION):
            raise ValueError("Terminal dimensions are outside the supported range.")
        runtime = get_runtime(self, project_id, session_id)
        fcntl.ioctl(runtime.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        # systemd-run forwards SIGWINCH to its inner PTY. Its outer PTY is not
        # the controlling terminal after start_new_session, so notify explicitly.
        runtime.process.send_signal(signal.SIGWINCH)

    def _expiry_reason(self, runtime: TerminalRuntime) -> str | None:
        """Why this session must end now, or None while it may keep running.

        Deciding this touches only local state: the membership store, the
        child's exit status and the clock. Nothing here waits on a machine, so
        every live session can be classified before the first stop begins.
        """
        if runtime.retiring:
            # Already decided by an ending that failed; this pass retries it.
            return runtime.retiring
        session = runtime.session
        if not self.membership_check(session.project_id, session.member_id):
            return "membership_lost"
        if runtime.process.poll() is not None:
            # Drain completion evidence before classifying SSH's ambiguous 255.
            while read_ready(runtime):
                pass
            return (
                "link_dropped"
                if session.execution_host
                and runtime.process.poll() == 255
                and runtime.completion.exit_code is None
                else "shell_exited"
            )
        if time.monotonic() - runtime.last_activity >= TERMINAL_IDLE_TIMEOUT_SECONDS:
            return "idle_timeout"
        return None

    async def sweep(self) -> None:
        """Retire every session whose lifetime has ended, in one pass.

        Classification comes first and stops come second. An expired session
        on a machine that has gone costs a stop timeout, so deciding and
        stopping one at a time let that one shell delay even the membership
        check for every session behind it, and `sweep_loop` sleeps again only
        once the whole pass returns.

        Both happen under one hold of the lock. Deciding is all local state,
        so it costs the hold nothing, and a decision made before the wait
        would be acted on after it: input arriving meanwhile renews the very
        lifetime an `idle_timeout` ended, and a route may retire a session
        this pass still holds.
        """
        errors = []
        expired = []
        async with self._lock:
            for runtime in list(self.sessions.values()):
                try:
                    reason = self._expiry_reason(runtime)
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                if reason is not None:
                    expired.append((runtime, reason))
            failures = await self._end_each(expired)
        errors.extend(str(failure) for _, failure in failures)
        if errors:
            raise TerminalUnavailable("; ".join(errors))
