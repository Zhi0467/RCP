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
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from rcp.agents.write_scope import RegisteredRepositoryRoot
from rcp.config import MachineConfig, Manifest
from rcp.git_access import ensure_checkout_git_access, missing_deploy_key
from rcp.git_identity import GitIdentity, write_git_identity
from rcp.limits import (
    TERMINAL_IDLE_TIMEOUT_SECONDS,
    TERMINAL_MAX_DIMENSION,
    TERMINAL_POLL_INTERVAL_SECONDS,
    TERMINAL_STARTUP_RECONCILE_TIMEOUT_SECONDS,
    TERMINAL_STOP_THREADS,
    TERMINAL_STOP_TIMEOUT_SECONDS,
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
from rcp.terminals.profile import SHELL_PATH
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

RECORD_FIELDS = frozenset(field.name for field in dataclasses.fields(TerminalSession))
NULLABLE_FIELDS = frozenset({"ended_at", "termination_reason"})
RETIRING_REFUSAL = (
    "An earlier terminal on this working tree could not be stopped, so its shell may "
    "still be running. Opening another would put two on one checkout; the stop is "
    "retried on its own."
)
RETAINED_REFUSAL = (
    "An earlier terminal for this repository could not be stopped on its execution "
    "machine, so its shell may still be running. Opening another is refused until that "
    "one is gone."
)


def manifest_registration(manifest: Manifest, repository_alias: str) -> tuple[str, str, str, str]:
    """What this alias currently names: path, machine, host and account."""
    repository = manifest.repository_map[repository_alias]
    machine = manifest.machine_map[repository.machine]
    return (repository.path, repository.machine, machine.host, machine.os_account)


def session_registration(session: TerminalSession) -> tuple[str, str, str, str]:
    """What the alias named when this session started."""
    return (
        session.declared_path,
        session.declared_machine,
        session.execution_host,
        session.declared_account,
    )


def registration_lapse(manifest: Manifest, session: TerminalSession) -> str | None:
    """Why this session's alias no longer names it, or None while it still does."""
    if session.repository_id not in manifest.repository_map:
        return "repository_unregistered"
    if session_registration(session) != manifest_registration(manifest, session.repository_id):
        return "repository_repointed"
    return None


def working_tree(session: TerminalSession) -> tuple[str, str]:
    """The tree a session holds: the path it resolved, on the host that resolved it.

    Every session resolved its own declaration, locally or on the execution
    machine, so two declarations that spell one tree meet here as one. A
    declaration is only how settings named the tree, and is never compared.
    """
    return (session.path, session.execution_host)


class TerminalManager:
    def __init__(self, data_dir: Path, membership_check: Callable[[str, str], bool]) -> None:
        self.probes = TerminalProbeCache()
        self.data_dir = data_dir
        self.directory = data_dir / "terminals"
        self.membership_check = membership_check
        self.sessions: dict[str, TerminalRuntime] = {}
        self._opening: set[tuple[str, str]] = set()
        self._opening_trees: set[tuple[str, str]] = set()
        # Records left unfinished because a unit could not be confirmed gone,
        # keyed by the alias each was filed under.
        self._unresolved: dict[tuple[str, str], list[TerminalSession]] = {}
        self._local_capability: TerminalCapability | None = None
        self._stops = ThreadPoolExecutor(
            max_workers=TERMINAL_STOP_THREADS, thread_name_prefix="rcp-terminal-stop"
        )
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
        a shell somewhere else. Collisions with sessions under other aliases
        or projects are decided by the tree an open resolves, not here.
        """
        for runtime in list(self.sessions.values()):
            session = runtime.session
            if session.project_id != project_id or session.repository_id != repository_alias:
                continue
            if runtime.retiring:
                raise TerminalUnavailable(RETIRING_REFUSAL)
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
        finished only once that unit is known to be gone.
        """
        session.termination_reason = reason
        self.mark_unresolved(session)
        if await self.unit_confirmed_gone(session):
            self._finish_retained(session, reason)
        else:
            save_metadata(self.directory, session)

    def mark_unresolved(self, session: TerminalSession) -> None:
        """Remember that this repository may still have a shell on it.

        One repository can be spoken for by several records, and each is
        tracked by its own `session_id`, so reconciling a record twice
        replaces its entry while two records never share one.
        """
        retained = self._unresolved.setdefault((session.project_id, session.repository_id), [])
        for index, existing in enumerate(retained):
            if existing.session_id == session.session_id:
                retained[index] = session
                return
        retained.append(session)

    def _finish_retained(self, session: TerminalSession, reason: str) -> None:
        """Finish a retained record now that its unit is known to be gone."""
        session.ended_at = timestamp()
        session.termination_reason = reason
        save_metadata(self.directory, session)
        key = (session.project_id, session.repository_id)
        remaining = [
            item for item in self._unresolved.get(key, []) if item.session_id != session.session_id
        ]
        if remaining:
            self._unresolved[key] = remaining
        else:
            self._unresolved.pop(key, None)

    async def _resolve_unfinished(
        self, project_id: str, repository_alias: str, tree: tuple[str, str] | None = None
    ) -> None:
        """Retry every retained record that speaks for this alias or this tree.

        A retained record means a unit may still own the checkout, and opening
        a second shell there is what one shell per working tree exists to
        prevent. Settings can drop an alias and register the same checkout
        under another name, so a record is matched by the tree it resolved as
        well as by the alias it was filed under. A record this version could
        not read resolved nothing, so its alias is all it has.
        """
        speaking = [
            session
            for key, retained in self._unresolved.items()
            for session in retained
            if key == (project_id, repository_alias)
            or (tree is not None and session.path and working_tree(session) == tree)
        ]
        if not speaking:
            return
        # Confirmed together: this is a member waiting to open, and a record
        # whose machine has gone costs a stop timeout apiece.
        confirmed = await asyncio.gather(*(self.unit_confirmed_gone(item) for item in speaking))
        for session, gone in zip(speaking, confirmed, strict=True):
            if gone:
                # A record retained by startup carries no reason yet; one
                # retained by a failed launch already carries its own.
                self._finish_retained(session, session.termination_reason or "server_restart")
        if not all(confirmed):
            raise TerminalUnavailable(RETAINED_REFUSAL)

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

    def _read_record(self, path: Path) -> TerminalSession | None:
        """This file's record, or None once the repository it names is blocked.

        `save_metadata` writes every field as a string, or null for the two
        that end a record, and names the file for the session in it. A record
        that does not look like that was not written by this version and is
        left as found. It is not read through the dataclass's defaults: an
        absent or mistyped `execution_host` would read as local, and a remote
        unit would then be cleaned up against a local one that was never
        there. Being unable to read a record is not evidence that its shell is
        gone, so whatever repository it still names stays blocked until the
        file is resolved by hand or by the version that wrote it.
        """
        payload: object = None
        try:
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict) or payload.keys() < RECORD_FIELDS:
                raise ValueError("does not name every field this version writes")
            for key, value in payload.items():
                if not isinstance(value, str) and (value is not None or key not in NULLABLE_FIELDS):
                    raise ValueError(f"holds a {type(value).__name__} for {key!r}")
            session = TerminalSession(**payload)
            if session.session_id != path.stem:
                raise ValueError(f"names session {session.session_id!r}")
            return session
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "Terminal record %s cannot be read; leaving it and blocking its repository: %s",
                path.name,
                exc,
            )
            self._block_unreadable(path, payload)
            return None

    def _block_unreadable(self, path: Path, payload: object) -> None:
        """Block the repository a record this version cannot read still names.

        The file, not anything inside it, identifies the blocker: two records
        must never share one identity, or resolving either would release the
        repository from both. Its containment is left blank, which
        `unit_confirmed_gone` never confirms, because nothing here can know
        what the record left running.
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

    async def _reconcile_guarded(self, path: Path) -> None:
        """Reconcile one record, and never let it cost the server its boot.

        Failing to reconcile a record is not evidence that its shell is gone.
        A readable record can still fail in ways no branch anticipates, such
        as a destination `ssh` refuses outright, and each of them leaves a
        unit this startup could not account for, so its repository is blocked.
        """
        session = self._read_record(path)
        if session is None:
            return
        try:
            await self._reconcile_record(session)
        except Exception as exc:
            logger.warning(
                "Terminal record %s could not be reconciled; leaving it and blocking "
                "its repository: %s",
                path.name,
                exc,
            )
            self._block_unreadable(
                path, {"project_id": session.project_id, "repository_id": session.repository_id}
            )

    async def _reconcile_record(self, session: TerminalSession) -> None:
        """Retire one unfinished record, or retain it while its unit may run."""
        if session.ended_at:
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
        # Retained before the stop is tried: a reconciliation that outlasts
        # the startup budget is cancelled with its stop still running, and the
        # record has to keep blocking its repository exactly as it stands.
        self.mark_unresolved(session)
        if await self.unit_confirmed_gone(session):
            self._finish_retained(session, "server_restart")

    async def start(self) -> None:
        if self._started:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.directory / "empty").mkdir(exist_ok=True, mode=0o500)
        # This runs before every other startup owner, so a stale record, a
        # moved data directory, or an unreachable machine must never be able
        # to refuse the server a boot. Records are reconciled together,
        # because shutdown leaves every live remote session unfinished and a
        # machine that went away costs a remote stop timeout per record. The
        # budget is what actually bounds the boot: the stops are blocking
        # calls in one thread pool, so enough of them queue anyway.
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
                logger.warning(
                    "Terminal record %s did not reconcile within the startup budget; "
                    "its repository stays blocked until a later attempt.",
                    started[task].name,
                )
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
        if not self.sessions:
            # Queued stops are dropped; the few already in a thread end on
            # their own timeout, because shutdown must not wait on the
            # network. A session whose stop failed is still here, and closing
            # again is how it is retried, so it keeps the pool it needs.
            self._stops.shutdown(wait=False, cancel_futures=True)
        if errors:
            raise TerminalUnavailable("; ".join(errors))

    async def _end_each(
        self, endings: list[tuple[TerminalRuntime, str]]
    ) -> list[tuple[TerminalRuntime, Exception]]:
        """End these sessions at once rather than one timeout after another.

        A single stop can spend its own timeout against a machine that has
        become unreachable, and every caller here has something waiting behind
        it: shutdown holds the instance lock, a member is waiting on a listing
        or an open, and the sweep cannot look at the next expired shell until
        this one is dealt with. Each ending touches only its own runtime, and
        the manager dictionaries they write are keyed per session.

        Callers hold the manager lock. Each ending is reported against its own
        runtime, because the caller names the failure by what it was ending.
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
        git_identity: GitIdentity | None = None,
        git_key: Path | None = None,
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
                git_identity=git_identity,
                git_key=git_key,
            )
        finally:
            self._opening.discard(key)

    def stop_thread(self, call: Callable[[], object]):
        """Run one blocking stop off the interpreter's shared pool.

        Ending several shells together is only as concurrent as the pool the
        stops run on, and a caller holds the instance lock across all of them.
        On the shared pool a project's worth of unreachable machines would
        queue into timeout-sized batches behind, and ahead of, every other
        blocking call the application makes.
        """
        return asyncio.get_running_loop().run_in_executor(self._stops, call)

    @contextlib.asynccontextmanager
    async def _reserved_tree(self, session: TerminalSession) -> AsyncIterator[None]:
        """Hold this working tree for one open, from resolution to publication.

        `_opening` reserves the alias asked for, and a registration can be
        renamed or moved to another project while this open is still probing
        or launching. A request arriving under the new name carries a
        different alias, so it passes that guard, and neither shell is in
        `sessions` yet for the other to find. The tree itself is what one
        shell per tree has to be reserved by, whoever asks and under whatever
        name.
        """
        tree = working_tree(session)
        async with self._lock:
            if tree in self._opening_trees:
                raise TerminalUnavailable(
                    "Another terminal is already opening on this working tree."
                )
            for runtime in self.sessions.values():
                if working_tree(runtime.session) != tree:
                    continue
                if runtime.retiring:
                    raise TerminalUnavailable(RETIRING_REFUSAL)
                raise TerminalUnavailable(
                    "Another terminal is already open on this working tree. "
                    "End it before opening one here."
                )
            self._opening_trees.add(tree)
        try:
            yield
        finally:
            self._opening_trees.discard(tree)

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
        git_identity: GitIdentity | None,
        git_key: Path | None,
    ) -> TerminalSession:
        """Probe, resolve and launch without the manager lock.

        One unreachable machine can cost a probe timeout plus a launch timeout.
        Holding the lock across that would stall every other project's open,
        end and sweep behind it, so only admission and publication take it.
        """
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
        if not machine.host:
            if git_key is not None:
                resolved = await asyncio.to_thread(
                    ensure_checkout_git_access,
                    str(root),
                    str(git_key),
                    timeout=TERMINAL_STOP_TIMEOUT_SECONDS,
                )
                if resolved is None:
                    raise RuntimeError(missing_deploy_key(str(root)))
            if git_identity is not None:
                identity_path = await asyncio.to_thread(
                    write_git_identity, self.data_dir, git_identity, git_path=SHELL_PATH
                )
                git_read_paths = (*git_read_paths, str(identity_path))
                git_environment = {
                    **(git_environment or {}),
                    "GIT_CONFIG_SYSTEM": str(identity_path),
                }
        # A record retained on this tree blocks the open whatever alias filed
        # it, and a stop that can now be finished releases it.
        await self._resolve_unfinished(project_id, repository_alias, (str(root), machine.host))
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
        async with self._reserved_tree(session):
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
                    git_identity=git_identity,
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
                if runtime.session.termination_reason is None:
                    # Unless the membership end above already finished: `end_runtime`
                    # completes its cleanup before re-raising a cancellation, so
                    # the record is written and the descriptor given back. Ending
                    # again would overwrite why this session ended and act on a
                    # descriptor another launch may already have been handed.
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
            # A record this version could not read: nothing here can know what
            # it left running, so it never stops blocking its repository.
            return False
        try:
            if session.execution_host:
                await self.stop_thread(
                    partial(
                        remote.stop_remote_unit,
                        session.execution_host,
                        session.unit,
                        session.declared_account,
                    )
                )
            else:
                await self.stop_thread(partial(launch.stop_unit, session.unit))
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
        # The lifetime is renewed by input arriving, not by the PTY finishing
        # with it. A backpressured shell can leave this loop waiting for
        # writable space for as long as it likes, and a sweep meanwhile would
        # read the older time and expire a session being typed into.
        runtime.last_activity = time.monotonic()
        runtime.session.last_activity_at = timestamp()
        view = memoryview(data)
        while view:
            try:
                sent = os.write(runtime.master_fd, view)
                view = view[sent:]
            except BlockingIOError:
                await asyncio.sleep(TERMINAL_POLL_INTERVAL_SECONDS)
                if get_runtime(self, project_id, session_id) is not runtime:
                    raise KeyError("Terminal session ended during input.") from None

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
