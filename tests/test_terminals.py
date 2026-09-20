from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
import tty
from pathlib import Path

import pytest

from rcp.agents.write_scope import protected_repository_paths, registered_repository_roots
from rcp.limits import TERMINAL_IDLE_TIMEOUT_SECONDS, TERMINAL_OUTPUT_BUFFER_BYTES
from rcp.terminals import TerminalManager, TerminalSession, TerminalUnavailable, launch
from rcp.terminals.backends import TERMINAL_BACKENDS, machine_capability, resolve_backend
from rcp.terminals.manager import manifest_registration, session_registration
from rcp.terminals.runtime import read_ready
from rcp.terminals.utilities import resolve_repository, save_metadata


class FakeProcess:
    returncode: int | None = None

    def poll(self):
        return self.returncode

    def send_signal(self, signal):
        self.last_signal = signal

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode


@pytest.fixture
def process_factory(monkeypatch):
    slaves = []
    commands = []
    stopped = []

    def start(command, unit):
        master, slave = os.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        slaves.append(slave)
        commands.append(command)
        return FakeProcess(), master

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch, "launch", start)
    monkeypatch.setattr(launch, "stop_unit", stopped.append)
    yield slaves, commands, stopped
    for descriptor in slaves:
        os.close(descriptor)


def record_payload(values):
    """A record with every field present, the way `save_metadata` writes one.

    Reconciliation refuses a record that does not name every field this
    version writes, because a silently defaulted field is a claim — an absent
    `execution_host` says local. Fixtures have to look like real records.
    """
    from dataclasses import asdict

    base = asdict(
        TerminalSession(
            session_id="",
            project_id="",
            member_id="",
            repository_id="",
            path="",
            started_at="",
            last_activity_at="",
            unit="",
        )
    )
    return {**base, **values}


def arguments(manifest):
    return dict(
        project_id="project",
        member_id="member",
        manifest=manifest,
        repository_alias="repo-a",
        repository_inventory=registered_repository_roots(manifest, project_id="project"),
    )


def test_launch_profile_preserves_canonical_denies_and_exact_git_access(manifest, tmp_path):
    root = Path(manifest.repository_map["repo-a"].path)
    state = root / ".research"
    state.mkdir(exist_ok=True)
    credential = tmp_path / "git key"
    credential.write_text("test-only")
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = root / "nested" / ".research"
    argv = launch.launch_command(
        unit="rcp-terminal-test",
        repository=root,
        protected_paths=[str(state), str(missing)],
        git_read_paths=(str(credential),),
        git_environment={"GIT_SSH_COMMAND": "ssh -F /dev/null"},
        empty_directory=empty,
    )
    properties = [argv[index + 1] for index, item in enumerate(argv) if item == "--property"]
    assert "ProtectHome=tmpfs" in properties
    assert "ProtectSystem=strict" in properties
    assert "PrivateTmp=yes" in properties
    assert f'BindPaths="{root}"' in properties
    assert f'ReadOnlyPaths="{state}"' in properties
    assert f'ReadOnlyPaths="{credential}"' in properties
    assert f'BindReadOnlyPaths="{empty}":"{missing}"' in properties
    assert "--pty" in argv
    assert "--service-type=exec" in argv
    assert "ProtectHome=yes" not in properties
    assert "ReadWritePaths" not in " ".join(properties)
    shell_start = argv.index("--")
    assert argv[shell_start + 1 : shell_start + 3] == ["/usr/bin/env", "-i"]
    assert "GIT_SSH_COMMAND=ssh -F /dev/null" in argv
    assert "HISTFILE=/dev/null" in argv


def test_shared_protected_builder_canonicalizes_every_repository_research(manifest, tmp_path):
    repositories = [Path(item.path) for item in manifest.repositories]
    external = tmp_path / "canonical-state"
    external.mkdir()
    (repositories[1] / ".research").symlink_to(external, target_is_directory=True)
    paths = protected_repository_paths(
        manifest=manifest,
        repository_roots=[str(root) for root in repositories],
    )
    assert str(external) in paths
    assert all(str(root / ".research") in paths for root in repositories)
    root, terminal_paths = resolve_repository(
        manifest=manifest,
        project_id="project",
        repository_id="repo-b",
        inventory=registered_repository_roots(manifest, project_id="project"),
        data_dir=tmp_path / "data",
    )
    assert root == repositories[1]
    assert set(paths) <= set(terminal_paths)


def test_canonical_state_cannot_be_a_terminal_root(manifest, tmp_path):
    root = Path(manifest.repository_map["repo-b"].path)
    (root / ".research").symlink_to(root, target_is_directory=True)
    with pytest.raises(TerminalUnavailable, match="Canonical state"):
        resolve_repository(
            manifest=manifest,
            project_id="project",
            repository_id="repo-b",
            inventory=registered_repository_roots(manifest, project_id="project"),
            data_dir=tmp_path / "data",
        )


def test_remote_repository_requires_matching_remote_stage(manifest, tmp_path):
    manifest.machine_map["laptop"].host = "worker.invalid"
    with pytest.raises(TerminalUnavailable, match="requires its execution host"):
        resolve_repository(
            manifest=manifest,
            project_id="project",
            repository_id="repo-a",
            inventory=registered_repository_roots(manifest, project_id="project"),
            data_dir=tmp_path / "data",
        )


@pytest.mark.asyncio
async def test_missing_systemd_fails_closed(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        with pytest.raises(TerminalUnavailable, match="systemd-run.*not installed"):
            await manager.open(**arguments(manifest))
        assert manager.list("project") == []
        assert list(manager.directory.glob("*.json")) == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_membership_gate_precedes_launch(manifest, tmp_path, process_factory):
    manager = TerminalManager(tmp_path / "data", lambda project, member: False)
    await manager.start()
    try:
        with pytest.raises(PermissionError, match="membership"):
            await manager.open(**arguments(manifest))
        assert not process_factory[1]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_shared_session_survives_navigation_and_streams_pty(
    manifest, tmp_path, process_factory
):
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        again = await manager.open(**{**arguments(manifest), "member_id": "second-member"})
        assert session is again
        assert len(process_factory[1]) == 1
        queue = manager.attach("project", session.session_id)
        assert session.state == "live"
        os.write(process_factory[0][0], b"hello terminal")
        assert await asyncio.wait_for(queue.get(), 2) == b"hello terminal"
        manager.detach("project", session.session_id, queue)
        assert session.state == "idle"
        assert manager.get("project", session.session_id) is session
        restored = manager.attach("project", session.session_id)
        assert restored.get_nowait() == b"hello terminal"
        await manager.write("project", session.session_id, b"git status\n")
        assert os.read(process_factory[0][0], 1024) == b"git status\n"
        manager.resize("project", session.session_id, 100, 40)
        with pytest.raises(KeyError):
            manager.get("different-project", session.session_id)
        await manager.end("project", session.session_id)
        assert await restored.get() is None
        assert manager.list("project") == []
        receipt = json.loads((manager.directory / f"{session.session_id}.json").read_text())
        assert receipt["termination_reason"] == "ended"
        assert receipt["member_id"] == "member"
        assert "hello terminal" not in json.dumps(receipt)
        assert "git status" not in json.dumps(receipt)
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle_timeout", "membership_lost", "shell_exited"])
async def test_lifecycle_ends_each_session(manifest, tmp_path, process_factory, reason):
    membership = True
    manager = TerminalManager(tmp_path / "data", lambda project, member: membership)
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        runtime = manager.sessions[session.session_id]
        queue = manager.attach("project", session.session_id)
        if reason == "idle_timeout":
            runtime.last_activity = time.monotonic() - TERMINAL_IDLE_TIMEOUT_SECONDS - 1
            os.write(process_factory[0][0], b"noisy forgotten command")
            read_ready(runtime)
        elif reason == "membership_lost":
            membership = False
        else:
            runtime.process.returncode = 0
        await manager.sweep()
        assert session.termination_reason == reason
        assert session.ended_at is not None
        assert process_factory[2] == [session.unit]
        assert manager.list("project") == []
        assert None in list(queue._queue)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_shutdown_stops_every_shell_at_once(manifest, tmp_path, process_factory, monkeypatch):
    """Shutdown holds the instance lock, so its stops cannot be cumulative.

    A single mirrored stop can spend its whole timeout against a machine that
    has gone unreachable. Retiring a project's shells one after another would
    outlast the window a replacement server waits for this one to go, so they
    have to overlap. A barrier proves the overlap without timing anything: a
    serial shutdown never brings a second stop here, and the barrier breaks.
    """
    meeting = threading.Barrier(2)

    def stop(unit):
        meeting.wait(timeout=5)

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        for alias in ("repo-a", "repo-b"):
            await manager.open(**{**arguments(manifest), "repository_alias": alias})
        assert len(manager.sessions) == 2
        monkeypatch.setattr(launch, "stop_unit", stop)
        await manager.close()
        assert manager.sessions == {}
    finally:
        meeting.abort()


@pytest.mark.asyncio
async def test_restart_cleans_only_own_unfinished_metadata(tmp_path, process_factory):
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    session = TerminalSession(
        session_id="test",
        project_id="project",
        member_id="member",
        repository_id="repo",
        path="/checkout",
        started_at="start",
        last_activity_at="start",
        unit=f"{manager._unit_prefix}-test",
    )
    save_metadata(manager.directory, session)
    await manager.start()
    try:
        assert process_factory[2] == [session.unit]
        recorded = json.loads((manager.directory / "test.json").read_text())
        assert recorded["termination_reason"] == "server_restart"
        assert recorded["ended_at"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_startup_survives_a_record_whose_unit_cannot_be_stopped(
    tmp_path, monkeypatch, caplog
):
    """`terminals.start()` is the first startup owner, so raising out of it
    refuses the server a boot until someone hand-deletes a JSON record.
    """
    from rcp.terminals import remote

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    session = TerminalSession(
        session_id="stranded",
        project_id="project",
        member_id="member",
        repository_id="repo",
        path="/checkout",
        started_at="start",
        last_activity_at="start",
        unit=f"{manager._unit_prefix}-stranded",
        containment="mirrored",
        execution_host="worker.invalid",
    )
    save_metadata(manager.directory, session)

    def unreachable(*args):
        raise TerminalUnavailable("The execution machine is unreachable.")

    monkeypatch.setattr(remote, "stop_remote_unit", unreachable)
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        recorded = json.loads((manager.directory / "stranded.json").read_text())
        # Retained unfinished: the record is the retry.
        assert recorded["ended_at"] is None
        assert any("stranded" in message or "retries" in message for message in caplog.messages)
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retained_reason", "expected"),
    [(None, "server_restart"), ("launch_failed", "launch_failed")],
)
async def test_a_late_stop_finishes_its_record_with_the_same_reason(
    manifest, tmp_path, monkeypatch, retained_reason, expected
):
    """Deferring cleanup must not cost the record its termination reason.

    A record retired on the first startup attempt says why. One whose unit
    could only be stopped later has to say the same thing, and one retained by
    a failed launch keeps the reason it already had.
    """
    from rcp.terminals import remote

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    session = TerminalSession(
        session_id="deferred",
        project_id="project",
        member_id="member",
        repository_id="repo",
        path="/checkout",
        started_at="start",
        last_activity_at="start",
        unit=f"{manager._unit_prefix}-deferred",
        containment="mirrored",
        execution_host="worker.invalid",
        termination_reason=retained_reason,
    )
    save_metadata(manager.directory, session)

    reachable = False

    def stop(*args):
        if not reachable:
            raise TerminalUnavailable("The execution machine is unreachable.")

    monkeypatch.setattr(remote, "stop_remote_unit", stop)
    await manager.start()
    try:
        assert json.loads((manager.directory / "deferred.json").read_text())["ended_at"] is None
        reachable = True
        await manager._resolve_unfinished("project", manifest, "repo")
        recorded = json.loads((manager.directory / "deferred.json").read_text())
        assert recorded["ended_at"]
        assert recorded["termination_reason"] == expected
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_failed_mirrored_launch_keeps_its_record_when_the_unit_may_survive(
    manifest, tmp_path, process_factory, monkeypatch, caplog
):
    """A mirrored launch can create its unit and then fail before readiness.
    Finishing that record would hide the unit from startup reconciliation.
    """

    def fail(command, unit, **kwargs):
        raise TerminalUnavailable("systemd-run did not confirm the required terminal profile.")

    def unstoppable(unit):
        raise TerminalUnavailable("systemctl is unavailable.")

    monkeypatch.setattr(launch, "launch", fail)
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(TerminalUnavailable):
            await manager.open(**arguments(manifest))
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["ended_at"] is None
        assert receipt["termination_reason"] == "launch_failed"
        assert any("may still be running" in message for message in caplog.messages)
    finally:
        await manager.close()


def test_a_local_machine_without_a_runnable_shell_is_not_offered(manifest, monkeypatch):
    """The cooperative helper writes its readiness marker before execv, so an
    unrunnable shell would otherwise admit a session that is already gone.
    """
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Darwin")
    monkeypatch.setattr("rcp.terminals.backends.os.access", lambda path, mode: False)
    capability = machine_capability(manifest.machine_map["laptop"])
    assert capability.backend is None
    assert "/bin/bash" in capability.reason


@pytest.mark.asyncio
async def test_startup_survives_a_record_written_by_another_version(tmp_path, caplog):
    """`TerminalSession` is a dataclass, so a record carrying an unknown or
    missing field raises TypeError rather than a validation error. Startup runs
    before every other owner, so that must be skipped, not fatal.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    (manager.directory / "skewed.json").write_text(
        json.dumps(record_payload({"session_id": "skewed", "a_field_from_another_version": True}))
    )
    (manager.directory / "unparsable.json").write_text("{ not json")
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        # Both are left exactly as found; neither stopped the boot.
        assert json.loads((manager.directory / "skewed.json").read_text())[
            "a_field_from_another_version"
        ]
        assert (manager.directory / "unparsable.json").read_text() == "{ not json"
        assert len([m for m in caplog.messages if "leaving it" in m]) == 2
        # Neither names a repository, so neither can block one.
        assert manager._unresolved == {}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_record_cannot_name_a_file_outside_the_terminal_directory(tmp_path, caplog):
    """Reconciliation writes some records back, and a record's `session_id`
    is whatever was written into it. `../rcp-server` is this data directory's
    own server metadata, so deriving a destination from one must refuse to
    leave the directory rather than overwrite what it lands on.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    server_metadata = manager.data_dir / "rcp-server.json"
    server_metadata.write_text('{"instance": "original"}')
    (manager.directory / "escape.json").write_text(
        json.dumps(
            record_payload(
                {
                    "session_id": "../rcp-server",
                    "project_id": "project",
                    "member_id": "member",
                    "repository_id": "repo-a",
                    "path": "/checkout",
                    "started_at": "start",
                    "last_activity_at": "start",
                    # Not this directory's prefix, so reconciliation retires it,
                    # which is the branch that writes the record back.
                    "unit": "rcp-terminal-elsewhere-escape",
                }
            )
        )
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        assert json.loads(server_metadata.read_text()) == {"instance": "original"}
        assert any("carries session id" in message for message in caplog.messages)
        assert ("project", "repo-a") in manager._unresolved
    finally:
        await manager.close()


def test_a_record_path_refuses_an_identifier_that_leaves_its_directory(tmp_path):
    """The self-naming check catches these first, but this is the guard that
    every `save_metadata` caller sits behind, including any later one.
    """
    from rcp.terminals.utilities import record_path

    assert record_path(tmp_path, "ordinary") == tmp_path / "ordinary.json"
    for identifier in ("", "../rcp-server", "nested/session", "/etc/rcp-absolute"):
        with pytest.raises(TerminalUnavailable, match="does not name a record"):
            record_path(tmp_path, identifier)


@pytest.mark.asyncio
async def test_startup_reconciles_records_together(tmp_path, monkeypatch):
    """Shutdown leaves every live remote session unfinished on purpose, so a
    machine that went away costs a remote stop timeout per record, and startup
    runs before every other owner. A barrier proves the overlap without timing
    anything: reconciling in turn never brings a second stop here.
    """
    from rcp.terminals import remote

    meeting = threading.Barrier(2)
    met = []

    def stop(host, unit, account=""):
        meeting.wait(timeout=5)
        met.append(unit)

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    for name in ("first", "second"):
        (manager.directory / f"{name}.json").write_text(
            json.dumps(
                record_payload(
                    {
                        "session_id": name,
                        "project_id": "project",
                        "member_id": "member",
                        "repository_id": f"repo-{name}",
                        "path": f"/checkout/{name}",
                        "started_at": "start",
                        "last_activity_at": "start",
                        "unit": f"{manager._unit_prefix}-{name}",
                        "containment": "mirrored",
                        "execution_host": "worker.invalid",
                    }
                )
            )
        )
    monkeypatch.setattr(remote, "stop_remote_unit", stop)
    await manager.start()
    try:
        assert sorted(met) == sorted(
            f"{manager._unit_prefix}-{name}" for name in ("first", "second")
        )
        for name in ("first", "second"):
            record = json.loads((manager.directory / f"{name}.json").read_text())
            assert record["termination_reason"] == "server_restart"
            assert record["ended_at"]
    finally:
        meeting.abort()
        await manager.close()


@pytest.mark.asyncio
async def test_a_record_cannot_overwrite_a_sibling_record(tmp_path, caplog):
    """A damaged record naming another session's id would retire that session
    under its own name. The record it overwrote may be an unfinished one whose
    whole job is to keep a repository blocked, which would then be unblocked
    with its unit still running.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    retained = {
        "session_id": "real",
        "project_id": "project",
        "member_id": "member",
        "repository_id": "repo-a",
        "path": "/checkout",
        "started_at": "start",
        "last_activity_at": "start",
        "unit": f"{manager._unit_prefix}-real",
        # Unknown to this version, so it is retained and blocks its repository
        # without any network call.
        "containment": "from-a-newer-version",
    }
    (manager.directory / "real.json").write_text(json.dumps(retained))
    (manager.directory / "damaged.json").write_text(
        json.dumps({**retained, "unit": "rcp-terminal-elsewhere-real", "containment": "mirrored"})
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        assert json.loads((manager.directory / "real.json").read_text()) == retained
        assert ("project", "repo-a") in manager._unresolved
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_record_from_another_version_still_blocks_its_repository(
    manifest, tmp_path, process_factory, caplog
):
    """Being unable to read a record is not evidence that its shell is gone.

    A record carrying every field this version knows plus one it does not is
    still a record of a unit that may own the checkout. Skipping it entirely
    would let the next open put a second shell there.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    (manager.directory / "newer.json").write_text(
        json.dumps(
            record_payload(
                {
                    "session_id": "newer",
                    "project_id": "project",
                    "member_id": "member",
                    "repository_id": "repo-a",
                    "path": "/checkout",
                    "started_at": "start",
                    "last_activity_at": "start",
                    "unit": f"{manager._unit_prefix}-newer",
                    "containment": "mirrored",
                    "a_field_from_another_version": True,
                }
            )
        )
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        assert json.loads((manager.directory / "newer.json").read_text())["session_id"] == "newer"
        assert ("project", "repo-a") in manager._unresolved
        with pytest.raises(TerminalUnavailable, match="may still be running"):
            await manager.open(**arguments(manifest))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_retained_record_refuses_a_reopen_until_its_unit_is_gone(
    manifest, tmp_path, process_factory, monkeypatch
):
    """Startup keeps a record whose unit it could not stop. That unit is not in
    `sessions`, so nothing else stops a member opening a second shell on the
    same checkout while the first may still be writing it.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    save_metadata(
        manager.directory,
        TerminalSession(
            session_id="stranded",
            project_id="project",
            member_id="member",
            repository_id="repo-a",
            path=str(manifest.repository_map["repo-a"].path),
            started_at="start",
            last_activity_at="start",
            unit=f"{manager._unit_prefix}-stranded",
        ),
    )
    stoppable = False

    def stop(unit):
        if not stoppable:
            raise TerminalUnavailable("systemctl is unavailable.")

    monkeypatch.setattr(launch, "stop_unit", stop)
    await manager.start()
    try:
        with pytest.raises(TerminalUnavailable, match="until that one is gone"):
            await manager.open(**arguments(manifest))
        assert manager.list("project") == []
        # Once the unit is confirmed gone the record finishes and opening works.
        stoppable = True
        session = await manager.open(**arguments(manifest))
        assert session.repository_id == "repo-a"
        assert json.loads((manager.directory / "stranded.json").read_text())["ended_at"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_startup_survives_a_record_whose_field_types_are_wrong(tmp_path, caplog):
    """A dataclass enforces no types, so a record can carry a value that breaks
    reconciliation itself rather than its construction: an unhashable project
    id cannot become the key that blocks its repository.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    (manager.directory / "typed.json").write_text(
        json.dumps(
            record_payload(
                {
                    "session_id": "typed",
                    "project_id": [],
                    "member_id": "member",
                    "repository_id": "repo",
                    "path": "/checkout",
                    "started_at": "start",
                    "last_activity_at": "start",
                    "unit": f"{manager._unit_prefix}-typed",
                    "containment": "a-containment-from-another-version",
                }
            )
        )
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        # Left exactly as found, and the boot completed.
        assert json.loads((manager.directory / "typed.json").read_text())["project_id"] == []
        assert any("wrong kind" in message for message in caplog.messages)
        # Nothing here names a repository, so nothing can be blocked for one.
        assert manager._unresolved == {}
    finally:
        await manager.close()


def test_every_terminal_record_field_holds_a_string():
    """`_record_values_are_well_typed` assumes this, and refuses valid records
    if it stops being true. A field of any other type has to be added to it
    deliberately rather than discovered as a rejected record.
    """
    from dataclasses import fields
    from typing import get_type_hints

    from rcp.terminals.manager import _NULLABLE_RECORD_FIELDS

    hints = get_type_hints(TerminalSession)
    for field in fields(TerminalSession):
        expected = str | None if field.name in _NULLABLE_RECORD_FIELDS else str
        annotation = hints[field.name]
        # A Literal of strings is still a string field.
        if getattr(annotation, "__origin__", None) is not None and not isinstance(
            annotation, type(str | None)
        ):
            annotation = str
        assert annotation == expected, field.name


@pytest.mark.asyncio
async def test_every_unresolved_record_has_to_be_accounted_for(
    manifest, tmp_path, process_factory, monkeypatch
):
    """One repository can be spoken for by more than one retained record: a
    unit that outlived a restart, and a record this version cannot read. If
    only the last one tracked were kept, confirming it gone would unblock the
    repository while the other may still be running.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    common = {
        "project_id": "project",
        "member_id": "member",
        "repository_id": "repo-a",
        "path": "/checkout",
        "started_at": "start",
        "last_activity_at": "start",
    }
    # A real unit this startup cannot stop, so its record is retained.
    (manager.directory / "stranded.json").write_text(
        json.dumps(
            record_payload(
                {
                    **common,
                    "session_id": "stranded",
                    "unit": f"{manager._unit_prefix}-stranded",
                    "containment": "mirrored",
                }
            )
        )
    )
    # A second record for the same repository that this version cannot read.
    (manager.directory / "newer.json").write_text(
        json.dumps(
            record_payload(
                {
                    **common,
                    "session_id": "newer",
                    "unit": f"{manager._unit_prefix}-newer",
                    "containment": "mirrored",
                    "a_field_from_another_version": True,
                }
            )
        )
    )

    stoppable = {"value": False}

    def stop(unit):
        if not stoppable["value"]:
            raise TerminalUnavailable("systemctl user manager unavailable")

    monkeypatch.setattr(launch, "stop_unit", stop)
    await manager.start()
    try:
        assert len(manager._unresolved[("project", "repo-a")]) == 2
        # The stoppable one is now confirmed gone, and the unreadable one is
        # not; the repository stays refused on the strength of the second.
        stoppable["value"] = True
        with pytest.raises(TerminalUnavailable, match="may still be running"):
            await manager.open(**arguments(manifest))
        assert [session.session_id for session in manager._unresolved[("project", "repo-a")]] == [
            "newer"
        ]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_renamed_alias_cannot_open_over_the_old_alias_blocker(
    manifest, tmp_path, process_factory, monkeypatch
):
    """Settings can drop an alias and register the same checkout under another
    name. A blocker filed under the old alias then names a unit on the very
    working tree the new alias is about to open, and an alias-only lookup
    never finds it.
    """
    checkout = Path(manifest.repository_map["repo-a"].path)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    (manager.directory / "stranded.json").write_text(
        json.dumps(
            record_payload(
                {
                    "session_id": "stranded",
                    "project_id": "project",
                    "member_id": "member",
                    "repository_id": "repo-a",
                    "path": str(checkout),
                    "started_at": "start",
                    "last_activity_at": "start",
                    "unit": f"{manager._unit_prefix}-stranded",
                    "containment": "mirrored",
                    "declared_path": str(checkout),
                    "declared_machine": "laptop",
                }
            )
        )
    )

    def unstoppable(unit):
        raise TerminalUnavailable("systemctl user manager unavailable")

    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    await manager.start()
    try:
        assert ("project", "repo-a") in manager._unresolved
        # The alias is renamed: repo-a is dropped and repo-b now names the
        # very checkout the stranded unit may still own.
        manifest.repositories = [item for item in manifest.repositories if item.alias != "repo-a"]
        manifest.repository_map["repo-b"].path = str(checkout)
        # The machine alias is renamed in the same settings pass. It is the
        # label RCP gives a host, so the tree is unchanged and the blocker
        # still speaks for it.
        manifest.machines[0].alias = "workstation"
        manifest.repository_map["repo-b"].machine = "workstation"
        with pytest.raises(TerminalUnavailable, match="may still be running"):
            await manager.open(**{**arguments(manifest), "repository_alias": "repo-b"})
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_reconciled_at_all_blocks_its_repository(
    manifest, tmp_path, process_factory, caplog
):
    """Failing to reconcile a record is not evidence that its shell is gone.

    An `execution_host` can be a string and still be one `ssh` refuses, which
    raises where no branch expects it. Whatever the reason, the record names a
    unit this startup could not account for, so the repository is blocked
    rather than merely logged about.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    (manager.directory / "unreachable.json").write_text(
        json.dumps(
            record_payload(
                {
                    "session_id": "unreachable",
                    "project_id": "project",
                    "member_id": "member",
                    "repository_id": "repo-a",
                    "path": "/checkout",
                    "started_at": "start",
                    "last_activity_at": "start",
                    "unit": f"{manager._unit_prefix}-unreachable",
                    "containment": "mirrored",
                    "execution_host": "bad\nhost",
                }
            )
        )
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        # Left as written, and the boot completed.
        assert json.loads((manager.directory / "unreachable.json").read_text())["ended_at"] is None
        assert any("blocking its repository" in message for message in caplog.messages)
        assert ("project", "repo-a") in manager._unresolved
        with pytest.raises(TerminalUnavailable, match="may still be running"):
            await manager.open(**arguments(manifest))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_malformed_record_cannot_borrow_another_blocker_identity(
    manifest, tmp_path, process_factory, monkeypatch
):
    """A record this version cannot read can claim any session id, including
    one a real record already holds. If both blockers were filed under that
    id, stopping the real unit would release the repository from the other
    one too, while it may name a unit of its own.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    common = {
        "project_id": "project",
        "member_id": "member",
        "repository_id": "repo-a",
        "path": "/checkout",
        "started_at": "start",
        "last_activity_at": "start",
        "containment": "mirrored",
    }
    (manager.directory / "real.json").write_text(
        json.dumps(
            record_payload({**common, "session_id": "real", "unit": f"{manager._unit_prefix}-real"})
        )
    )
    # Claims the other record's identity, and cannot be read besides.
    (manager.directory / "borrowed.json").write_text(
        json.dumps(
            record_payload(
                {
                    **common,
                    "session_id": "real",
                    "unit": f"{manager._unit_prefix}-borrowed",
                    "a_field_from_another_version": True,
                }
            )
        )
    )

    stoppable = {"value": False}

    def stop(unit):
        if not stoppable["value"]:
            raise TerminalUnavailable("systemctl user manager unavailable")

    monkeypatch.setattr(launch, "stop_unit", stop)
    await manager.start()
    try:
        assert sorted(
            session.session_id for session in manager._unresolved[("project", "repo-a")]
        ) == ["borrowed", "real"]
        stoppable["value"] = True
        with pytest.raises(TerminalUnavailable, match="may still be running"):
            await manager.open(**arguments(manifest))
        assert [session.session_id for session in manager._unresolved[("project", "repo-a")]] == [
            "borrowed"
        ]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_startup_gives_up_on_a_record_that_outlasts_its_budget(tmp_path, monkeypatch, caplog):
    """Reconciling records at once is not the same as finishing at once: the
    stops are blocking calls in a shared pool, so enough of them queue anyway.
    The budget is what bounds the boot, and a record still running when it
    expires has to keep blocking its repository rather than be forgotten.
    """
    from rcp.terminals import remote

    release = threading.Event()

    def never(*args):
        assert release.wait(10)

    monkeypatch.setattr(remote, "stop_remote_unit", never)
    monkeypatch.setattr("rcp.terminals.manager.TERMINAL_STARTUP_RECONCILE_TIMEOUT_SECONDS", 0.25)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    (manager.directory / "wedged.json").write_text(
        json.dumps(
            record_payload(
                {
                    "session_id": "wedged",
                    "project_id": "project",
                    "member_id": "member",
                    "repository_id": "repo-a",
                    "path": "/checkout",
                    "started_at": "start",
                    "last_activity_at": "start",
                    "unit": f"{manager._unit_prefix}-wedged",
                    "containment": "mirrored",
                    "execution_host": "worker.invalid",
                }
            )
        )
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        assert any("startup budget" in message for message in caplog.messages)
        assert ("project", "repo-a") in manager._unresolved
        assert json.loads((manager.directory / "wedged.json").read_text())["ended_at"] is None
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_a_record_missing_a_field_is_not_read_as_a_local_one(tmp_path, caplog, monkeypatch):
    """A silently defaulted field is a claim, not an absence.

    `asdict` writes every field, so a record missing one was not written by
    this application. An absent `execution_host` defaults to empty, which says
    local, and cleaning a remote record up against a local unit that was never
    there reports success and retires it while its own unit still runs.
    """
    from rcp.terminals import remote

    monkeypatch.setattr(
        launch, "stop_unit", lambda unit: pytest.fail("This record cannot be trusted")
    )
    monkeypatch.setattr(
        remote, "stop_remote_unit", lambda *args: pytest.fail("This record cannot be trusted")
    )
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    complete = record_payload(
        {
            "session_id": "partial",
            "project_id": "project",
            "member_id": "member",
            "repository_id": "repo-a",
            "path": "/checkout",
            "started_at": "start",
            "last_activity_at": "start",
            "unit": f"{manager._unit_prefix}-partial",
            "containment": "mirrored",
        }
    )
    del complete["execution_host"]
    (manager.directory / "partial.json").write_text(json.dumps(complete))
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        assert json.loads((manager.directory / "partial.json").read_text())["ended_at"] is None
        assert any("every field this version writes" in message for message in caplog.messages)
        assert ("project", "repo-a") in manager._unresolved
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_remote_record_is_never_cleaned_up_as_a_local_one(tmp_path, caplog, monkeypatch):
    """A wrong type that happens to be falsey reads as an ordinary empty
    value. An `execution_host` of `[]` would send a remote record down the
    local stop, which reports success against a unit that was never there and
    retires the record while its remote unit may still be running.
    """
    from rcp.terminals import remote

    monkeypatch.setattr(
        launch, "stop_unit", lambda unit: pytest.fail("A remote record has no local unit")
    )
    monkeypatch.setattr(
        remote, "stop_remote_unit", lambda *args: pytest.fail("This record cannot be trusted")
    )
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    (manager.directory / "mistyped.json").write_text(
        json.dumps(
            record_payload(
                {
                    "session_id": "mistyped",
                    "project_id": "project",
                    "member_id": "member",
                    "repository_id": "repo-a",
                    "path": "/checkout",
                    "started_at": "start",
                    "last_activity_at": "start",
                    "unit": f"{manager._unit_prefix}-mistyped",
                    "containment": "mirrored",
                    "execution_host": [],
                }
            )
        )
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        # Left exactly as written: never retired, never rewritten.
        assert json.loads((manager.directory / "mistyped.json").read_text())["ended_at"] is None
        assert ("project", "repo-a") in manager._unresolved
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_startup_leaves_a_record_whose_containment_it_does_not_know(
    manifest, tmp_path, monkeypatch, caplog
):
    """`containment` is a Literal on a dataclass, so nothing enforces it at
    runtime. An unknown value must not fall through the mirrored check and be
    retired, which would hide a possible shell from every later startup.
    """
    from rcp.terminals import remote

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    session = TerminalSession(
        session_id="skewed",
        project_id="project",
        member_id="member",
        repository_id="repo",
        path="/checkout",
        started_at="start",
        last_activity_at="start",
        unit=f"{manager._unit_prefix}-skewed",
    )
    save_metadata(manager.directory, session)
    record = json.loads((manager.directory / "skewed.json").read_text())
    record["containment"] = "mirrored-with-something-new"
    (manager.directory / "skewed.json").write_text(json.dumps(record))
    monkeypatch.setattr(
        remote, "stop_remote_unit", lambda *args: pytest.fail("Containment is unknown here")
    )
    monkeypatch.setattr(
        launch, "stop_unit", lambda *args: pytest.fail("Containment is unknown here")
    )
    with caplog.at_level(logging.WARNING):
        await manager.start()
    try:
        recorded = json.loads((manager.directory / "skewed.json").read_text())
        assert recorded["ended_at"] is None
        assert any("does not know" in message for message in caplog.messages)
        # It may name a running unit, and this version cannot confirm otherwise,
        # so it must keep blocking rather than admit a second shell there.
        with pytest.raises(TerminalUnavailable, match="until that one is gone"):
            await manager.open(**dict(arguments(manifest), repository_alias="repo"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_startup_retires_a_record_belonging_to_another_data_directory(tmp_path, monkeypatch):
    """The unit prefix hashes the data directory, so moving it makes every
    unfinished record foreign. That must not be a permanent startup failure.
    """
    from rcp.terminals import remote

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    session = TerminalSession(
        session_id="foreign",
        project_id="project",
        member_id="member",
        repository_id="repo",
        path="/checkout",
        started_at="start",
        last_activity_at="start",
        unit="rcp-terminal-000000000000-foreign",
        containment="mirrored",
    )
    save_metadata(manager.directory, session)
    monkeypatch.setattr(
        remote,
        "stop_remote_unit",
        lambda *args: pytest.fail("A unit this directory does not own is not ours to stop"),
    )
    monkeypatch.setattr(
        launch, "stop_unit", lambda *args: pytest.fail("A foreign unit is not ours to stop")
    )
    await manager.start()
    try:
        recorded = json.loads((manager.directory / "foreign.json").read_text())
        assert recorded["ended_at"]
        assert recorded["termination_reason"] == "unit_identity_mismatch"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_one_blocked_launch_does_not_stall_another_repository(
    manifest, tmp_path, monkeypatch
):
    """Probe and launch run outside the manager lock. One unreachable machine
    otherwise stalls every other member's open, end and sweep behind it.
    """
    released = threading.Event()
    started = threading.Event()
    slaves = []
    blocking = True

    def start(command, unit, **kwargs):
        nonlocal blocking
        if blocking:
            blocking = False
            started.set()
            # Hold the worker thread the way an unreachable SSH handshake does.
            released.wait(timeout=10)
        master, slave = os.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        slaves.append(slave)
        return FakeProcess(), master

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch, "launch", start)
    monkeypatch.setattr(launch, "stop_unit", lambda *args: None)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        blocked = asyncio.create_task(manager.open(**arguments(manifest)))
        assert await asyncio.to_thread(started.wait, 5)
        second = dict(arguments(manifest), repository_alias="repo-b")
        # The first launch still holds its worker thread here.
        await asyncio.wait_for(manager.open(**second), timeout=5)
        assert [session.repository_id for session in manager.list("project")] == ["repo-b"]
        released.set()
        await asyncio.wait_for(blocked, timeout=10)
        assert {session.repository_id for session in manager.list("project")} == {
            "repo-a",
            "repo-b",
        }
    finally:
        released.set()
        await manager.close()
        for descriptor in slaves:
            with contextlib.suppress(OSError):
                os.close(descriptor)


@pytest.mark.asyncio
async def test_a_viewer_that_falls_behind_is_detached_not_ended(
    manifest, tmp_path, process_factory
):
    """A full subscriber queue must not reuse the session-end sentinel, or a
    slow connection reports a running shell as terminated.
    """
    from rcp.terminals.models import DETACHED

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        runtime = manager.sessions[session.session_id]
        queue = manager.attach("project", session.session_id)
        while not queue.full():
            queue.put_nowait(b"backlog")
        os.write(process_factory[0][0], b"one more chunk")
        while read_ready(runtime):
            pass
        assert queue.get_nowait() is DETACHED
        assert queue not in runtime.subscribers
        # The shell itself is untouched.
        assert manager.list("project") == [session]
        assert session.ended_at is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_replay_is_bounded_and_never_saved(manifest, tmp_path, process_factory):
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        runtime = manager.sessions[session.session_id]
        runtime.replay.extend(b"x" * TERMINAL_OUTPUT_BUFFER_BYTES)
        os.write(process_factory[0][0], b"new-output")
        read_ready(runtime)
        assert len(runtime.replay) == TERMINAL_OUTPUT_BUFFER_BYTES
        assert runtime.replay.endswith(b"new-output")
        assert all(
            "replay" not in json.loads(path.read_text())
            for path in manager.directory.glob("*.json")
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_open_cancellation_stops_the_inflight_launch(
    manifest, tmp_path, process_factory, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    original = launch.launch

    def delayed(command, unit):
        started.set()
        assert release.wait(5)
        return original(command, unit)

    monkeypatch.setattr(launch, "launch", delayed)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        opening = asyncio.create_task(manager.open(**arguments(manifest)))
        assert await asyncio.to_thread(started.wait, 5)
        opening.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert manager.list("project") == []
        assert len(process_factory[2]) == 1
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["termination_reason"] == "opening_cancelled"
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_an_open_cancelled_while_publishing_does_not_leave_its_shell(
    manifest, tmp_path, process_factory, monkeypatch
):
    """Publication waits for the manager lock, which an end can hold across a
    `systemctl` call. The shell already exists by then, so a requester that
    leaves during that wait must not strand it: `_opening` is cleared either
    way, and the next open would put a second shell on the same checkout.
    """

    class SignallingLock(asyncio.Lock):
        """Announce that someone is waiting, so the test needs no sleep."""

        def __init__(self):
            super().__init__()
            self.waiting = asyncio.Event()

        async def acquire(self):
            if self.locked():
                self.waiting.set()
            return await super().acquire()

    started = threading.Event()
    release = threading.Event()
    original = launch.launch
    launched = []

    def delayed(command, unit):
        started.set()
        assert release.wait(5)
        launched.append(original(command, unit))
        return launched[-1]

    monkeypatch.setattr(launch, "launch", delayed)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    lock = SignallingLock()
    manager._lock = lock
    try:
        opening = asyncio.create_task(manager.open(**arguments(manifest)))
        assert await asyncio.to_thread(started.wait, 5)
        await lock.acquire()
        release.set()
        await asyncio.wait_for(lock.waiting.wait(), 5)
        opening.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert manager.sessions == {}
        assert manager.list("project") == []
        process, master_fd = launched[0]
        with pytest.raises(OSError):
            os.fstat(master_fd)
        assert process.poll() is not None
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["termination_reason"] == "opening_cancelled"
        assert receipt["unit"] in process_factory[2]
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_a_cancelled_open_whose_stop_fails_leaves_no_reusable_session(
    manifest, tmp_path, process_factory, monkeypatch
):
    """The launch wins the race, then its unit refuses to stop.

    Nothing drains this PTY, because a cancelled open never registers a
    reader. Publishing the runtime would let the next open hand back a
    session whose output never arrives, and a stop that raises would leave it
    in `sessions` where neither the sweep nor startup retires it.
    """
    started = threading.Event()
    release = threading.Event()
    original = launch.launch
    launched = []

    def delayed(command, unit):
        started.set()
        assert release.wait(5)
        launched.append(original(command, unit))
        return launched[-1]

    def unstoppable(unit):
        raise TerminalUnavailable("systemctl user manager unavailable")

    monkeypatch.setattr(launch, "launch", delayed)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        opening = asyncio.create_task(manager.open(**arguments(manifest)))
        assert await asyncio.to_thread(started.wait, 5)
        opening.cancel()
        monkeypatch.setattr(launch, "stop_unit", unstoppable)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert manager.sessions == {}
        assert manager.list("project") == []
        # The record retains the unit; the descriptor and process are given
        # back, or repeated cancellations would spend the server's descriptors.
        process, master_fd = launched[0]
        with pytest.raises(OSError):
            os.fstat(master_fd)
        assert process.poll() is not None
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["termination_reason"] == "opening_cancelled"
        assert receipt["ended_at"] is None
        with pytest.raises(TerminalUnavailable, match="may still be running"):
            await manager.open(**arguments(manifest))
    finally:
        release.set()
        monkeypatch.setattr(launch, "stop_unit", process_factory[2].append)
        await manager.close()


@pytest.mark.asyncio
async def test_a_cancelled_open_whose_launch_then_fails_still_tracks_its_unit(
    manifest, tmp_path, process_factory, monkeypatch
):
    """The failure is raised while the cancellation is being handled, so it
    cannot reach the ordinary launch-failure branch. Without cleanup of its own
    the record is left untracked while `_opening` is cleared, which is enough
    for the next attempt to put a second shell on the checkout.
    """
    started = threading.Event()
    release = threading.Event()

    def delayed(command, unit):
        started.set()
        assert release.wait(5)
        raise TerminalUnavailable("systemd-run did not confirm the required terminal profile.")

    def unstoppable(unit):
        raise TerminalUnavailable("systemctl is unavailable.")

    monkeypatch.setattr(launch, "launch", delayed)
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        opening = asyncio.create_task(manager.open(**arguments(manifest)))
        assert await asyncio.to_thread(started.wait, 5)
        opening.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["ended_at"] is None
        assert receipt["termination_reason"] == "launch_failed"
        assert manager.list("project") == []
        # Its unit is unaccounted for, so the next open is refused rather than
        # quietly starting a second shell on the same checkout.
        with pytest.raises(TerminalUnavailable, match="until that one is gone"):
            await manager.open(**arguments(manifest))
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_stale_aliases_are_retired_together(manifest, tmp_path, process_factory, monkeypatch):
    """Every caller of this is a member waiting on a listing, an open or an
    attach. A stale alias whose machine has gone costs a stop timeout, so
    retiring them in turn would spend one per alias before answering. A
    barrier proves the overlap without timing anything.
    """
    meeting = threading.Barrier(2)

    def stop(unit):
        meeting.wait(timeout=5)

    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        for alias in ("repo-a", "repo-b"):
            await manager.open(**{**arguments(manifest), "repository_alias": alias})
        assert len(manager.sessions) == 2
        # Both aliases stop naming what their shells opened on.
        for alias in ("repo-a", "repo-b"):
            moved = tmp_path / f"moved-{alias}"
            (moved / ".research").mkdir(parents=True)
            manifest.repository_map[alias].path = str(moved)
        monkeypatch.setattr(launch, "stop_unit", stop)
        await manager.reconcile_registrations("project", manifest)
        assert manager.list("project") == []
    finally:
        meeting.abort()
        await manager.close()


@pytest.mark.asyncio
async def test_repointing_a_repository_does_not_hand_back_the_old_checkout(
    manifest, tmp_path, process_factory
):
    """Reusing an alias for a different path must not answer a request for one
    checkout with a live shell on another.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        first = await manager.open(**arguments(manifest))
        assert first.declared_path == manifest.repository_map["repo-a"].path
        moved = tmp_path / "moved-checkout"
        (moved / ".research").mkdir(parents=True)
        manifest.repository_map["repo-a"].path = str(moved)
        second = await manager.open(**arguments(manifest))
        assert second.session_id != first.session_id
        assert second.declared_path == str(moved)
        # The same path on another machine is another registration too.
        assert session_registration(second) != session_registration(first)
        assert [session.session_id for session in manager.list("project")] == [second.session_id]
        retired = json.loads((manager.directory / f"{first.session_id}.json").read_text())
        assert retired["termination_reason"] == "repository_repointed"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_moving_a_repository_to_another_machine_does_not_hand_back_the_old_shell(
    manifest, tmp_path, process_factory
):
    """Two machines can share a path string, so the declared path alone does
    not identify a registration; the machine and its account belong to it too.
    """
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        first = await manager.open(**arguments(manifest))
        assert first.declared_machine == manifest.repository_map["repo-a"].machine
        # Same declared path, different account on the same machine alias.
        machine = manifest.machine_map[manifest.repository_map["repo-a"].machine]
        machine.os_account = "someone-else"
        assert manifest_registration(manifest, "repo-a") != session_registration(first)
        second = await manager.open(**arguments(manifest))
        assert second.session_id != first.session_id
        assert second.declared_account == "someone-else"
        retired = json.loads((manager.directory / f"{first.session_id}.json").read_text())
        assert retired["termination_reason"] == "repository_repointed"
    finally:
        await manager.close()


def test_failed_required_profile_never_launches_a_fallback(tmp_path, monkeypatch):
    commands = []
    stopped = []

    def failed_process(command, **kwargs):
        commands.append(command)
        process = FakeProcess()
        process.returncode = 1
        return process

    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch.subprocess, "Popen", failed_process)
    monkeypatch.setattr(launch, "stop_unit", stopped.append)
    command = ["systemd-run", "--unit=required-profile"]
    with pytest.raises(TerminalUnavailable, match="required terminal mount profile"):
        launch.launch(command, "required-profile")
    assert commands == [command]
    assert stopped == ["required-profile"]


@pytest.mark.asyncio
async def test_failed_stop_keeps_metadata_and_can_be_retried(
    manifest, tmp_path, process_factory, monkeypatch
):
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    session = await manager.open(**arguments(manifest))
    runtime = manager.sessions[session.session_id]
    runtime.last_activity = time.monotonic() - TERMINAL_IDLE_TIMEOUT_SECONDS - 1
    original = launch.stop_unit

    def failed_stop(unit):
        raise TerminalUnavailable("systemctl user manager unavailable")

    monkeypatch.setattr(launch, "stop_unit", failed_stop)
    with pytest.raises(TerminalUnavailable, match="systemctl"):
        await manager.sweep()
    assert manager.get("project", session.session_id) is session
    assert session.ended_at is None
    monkeypatch.setattr(launch, "stop_unit", original)
    await manager.sweep()
    assert session.termination_reason == "idle_timeout"
    await manager.close()


def test_preflight_refuses_a_writable_canonical_directory(tmp_path):
    import subprocess

    state = tmp_path / ".research"
    state.mkdir()
    findmnt = tmp_path / "findmnt"
    findmnt.write_text("#!/bin/sh\nprintf 'ro\\n'\n")
    findmnt.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            launch._SHELL_PREFLIGHT,
            "rcp-terminal",
            str(state),
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
    )
    assert result.returncode == 1
    assert launch._READY_MARKER not in result.stdout
    assert b"canonical-state read-only mounts are unavailable" in result.stderr


def test_launcher_waits_for_preflight_and_leaves_prompt_in_pty(monkeypatch):
    descriptors = []

    def ready_process(command, **kwargs):
        descriptors.append(os.dup(kwargs["stdout"]))
        os.write(kwargs["stdout"], launch._READY_MARKER + b"ready prompt$ ")
        return FakeProcess()

    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch.subprocess, "Popen", ready_process)
    process, master = launch.launch(["systemd-run"], "test-unit")
    try:
        assert os.read(master, 1024) == b"ready prompt$ "
        assert process.poll() is None
    finally:
        os.close(master)
        for descriptor in descriptors:
            os.close(descriptor)


def test_launcher_refuses_missing_preflight_marker(monkeypatch):
    descriptors = []
    stopped = []

    def quiet_process(command, **kwargs):
        descriptors.append(os.dup(kwargs["stdout"]))
        return FakeProcess()

    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch.subprocess, "Popen", quiet_process)
    monkeypatch.setattr(launch, "stop_unit", stopped.append)
    monkeypatch.setattr(launch, "TERMINAL_LAUNCH_TIMEOUT_SECONDS", 0.01)
    try:
        with pytest.raises(TerminalUnavailable, match="did not confirm"):
            launch.launch(["systemd-run"], "test-unit")
        assert stopped == ["test-unit"]
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_a_timeout_confirming_a_failed_stop_is_reported_not_raised(monkeypatch):
    """`TimeoutExpired` is neither an OSError nor a RuntimeError, so one
    escaping the stop path would abort startup reconciliation instead of
    leaving its record for a later attempt.
    """
    import subprocess

    def answer(argv, **kwargs):
        if "show" in argv:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 1, "", "Unit is wedged")

    monkeypatch.setattr(
        launch.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(launch.subprocess, "run", answer)
    with pytest.raises(TerminalUnavailable, match="Could not stop terminal unit"):
        launch.stop_unit("wedged")


def test_collected_unit_cleanup_accepts_explicit_not_found(monkeypatch):
    import subprocess

    responses = iter(
        [
            subprocess.CompletedProcess([], 5, "", "Unit not loaded"),
            subprocess.CompletedProcess([], 4, "not-found\n", ""),
        ]
    )
    # Stopping needs systemctl alone, so the launch prerequisites are absent here.
    monkeypatch.setattr(
        launch.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(
        launch,
        "availability_diagnostic",
        lambda: pytest.fail("Retiring a running unit must not recheck launch prerequisites"),
    )
    monkeypatch.setattr(launch.subprocess, "run", lambda *args, **kwargs: next(responses))
    launch.stop_unit("already-collected")


def test_transient_arguments_pass_percent_and_space_paths_through_literally(tmp_path):
    """A transient unit is built over D-Bus, not parsed from a unit file, so
    systemd applies no specifier expansion to it.

    Measured on the execution host (systemd 249): `%h` stays `%h` in both
    properties and command arguments, and doubling it breaks a real path —
    `--working-directory` with `%%` fails to change directory at all.
    """
    repository = tmp_path / "repo %h $HOME with space"
    repository.mkdir()
    protected = repository / ".research"
    argv = launch.launch_command(
        unit="literal-paths",
        repository=repository,
        protected_paths=[str(protected)],
        git_read_paths=(),
        git_environment={},
        empty_directory=tmp_path,
    )
    assert "--expand-environment=no" in argv
    assert f"--working-directory={repository}" in argv
    assert f'BindPaths="{repository}"' in argv
    assert argv[-1] == str(protected)
    assert "%%" not in " ".join(argv)
    assert launch._SHELL_PREFLIGHT in argv


def test_preflight_refuses_a_service_manager_that_expanded_its_own_variables(tmp_path):
    import re
    import subprocess

    # A manager older than 254 rejects `--expand-environment=no`, so a launch
    # there omits it. If such a manager does expand the command line, every
    # `$name` it does not know is emptied before bash reads the script. The
    # preflight must say so rather than verify emptied paths.
    expanded = re.sub(r"\$\{?(\w+)\}?", "", launch._SHELL_PREFLIGHT)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", expanded, "rcp-terminal", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "rewrote the containment preflight" in result.stderr


def test_preflight_refuses_a_writable_mount_even_when_directory_permissions_deny_write(tmp_path):
    import subprocess

    findmnt = tmp_path / "findmnt"
    findmnt.write_text("#!/bin/sh\nprintf 'rw,relatime\\n'\n")
    findmnt.chmod(0o755)
    # Model directory permissions only; this test exercises the independent
    # effective-mount check without requiring mount privileges on the test host.
    permission_probe = 'test() { case "$1" in -w) [[ "$2" == "$PWD" ]];; -e) return 0;; esac; };\n'
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            permission_probe + launch._SHELL_PREFLIGHT,
            "rcp-terminal",
            str(tmp_path / ".research"),
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
    )
    assert result.returncode == 1
    assert launch._READY_MARKER not in result.stdout
    assert b"canonical-state mount verification failed" in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["sweep", "close", "end_all"])
async def test_one_failed_stop_does_not_starve_other_sessions(
    manifest,
    tmp_path,
    process_factory,
    monkeypatch,
    operation,
):
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    first = await manager.open(**arguments(manifest))
    second = await manager.open(**{**arguments(manifest), "repository_alias": "repo-b"})
    for runtime in manager.sessions.values():
        runtime.last_activity = time.monotonic() - TERMINAL_IDLE_TIMEOUT_SECONDS - 1
    original = launch.stop_unit

    def partial_failure(unit):
        if unit == first.unit:
            raise TerminalUnavailable("first unit cannot be stopped")
        original(unit)

    monkeypatch.setattr(launch, "stop_unit", partial_failure)
    try:
        with pytest.raises(TerminalUnavailable, match="first unit"):
            await getattr(manager, operation)()
        assert manager.get("project", first.session_id) is first
        assert second.ended_at is not None
        assert second.unit in process_factory[2]
    finally:
        monkeypatch.setattr(launch, "stop_unit", original)
        await manager.close()


@pytest.mark.parametrize("os_name", ["Linux", "Darwin", "macOS", "FreeBSD"])
@pytest.mark.parametrize("is_remote", [False, True])
def test_backend_selection_belongs_to_machine(os_name, is_remote):
    backend = resolve_backend(os_name, is_remote)
    expected = "systemd_user" if os_name == "Linux" else "pty"
    assert backend is TERMINAL_BACKENDS[expected]
    assert backend.supports(os_name, is_remote)
    assert backend.display_name


@pytest.mark.parametrize("missing", ["systemd-run", "systemctl", "findmnt"])
def test_linux_missing_prerequisite_is_unavailable(manifest, monkeypatch, missing):
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch.shutil, "which", lambda name: None if name == missing else name)
    capability = machine_capability(manifest.machine_map["laptop"])
    assert capability.backend is None
    assert missing in capability.reason


def test_linux_unreachable_manager_is_unavailable(manifest, monkeypatch):
    import subprocess

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch.sys, "platform", "linux")
    monkeypatch.setattr(launch.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        launch.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1 if command[0] == "systemctl" else 0, "", "Cannot connect to user bus"
        ),
    )
    capability = machine_capability(manifest.machine_map["laptop"])
    assert capability.backend is None
    assert "systemctl" in capability.reason
    assert "Cannot connect to user bus" in capability.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", ["mount profile rejected", "findmnt verification failed"])
async def test_mirrored_launch_failure_never_creates_cooperative_session(
    manifest, tmp_path, process_factory, monkeypatch, diagnostic
):
    commands = []

    def fail(command, unit, **kwargs):
        commands.append(command)
        raise TerminalUnavailable(diagnostic)

    monkeypatch.setattr(launch, "launch", fail)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        with pytest.raises(TerminalUnavailable, match=diagnostic):
            await manager.open(**arguments(manifest))
        assert len(commands) == 1
        assert commands[0][0] == "systemd-run"
        assert manager.list("project") == []
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["containment"] == "mirrored"
        # The intent is persisted before the launch, and this stub's unit stop
        # succeeds, so the record finishes rather than waiting for startup.
        assert receipt["ended_at"]
        assert receipt["termination_reason"] == "launch_failed"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cooperative_pty_reports_missing_protection_and_scrubs_environment(
    manifest, tmp_path, monkeypatch
):
    from rcp.api.terminal_projection import terminal_session_payload

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Darwin")
    monkeypatch.setenv("RCP_TEST_SECRET", "must-not-inherit")

    def unexpected(*args, **kwargs):
        pytest.fail("A cooperative PTY must not probe or stop systemd")

    monkeypatch.setattr(launch, "availability_diagnostic", unexpected)
    monkeypatch.setattr(launch, "stop_unit", unexpected)
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        payload = terminal_session_payload(session, {})
        assert payload["containment"] == "cooperative"
        assert "protection is unavailable" in payload["protection_notice"]
        assert "no filesystem fence" in payload["protection_notice"]
        queue = manager.attach("project", session.session_id)
        # Disable input echo so only evaluated output can satisfy the assertions.
        await manager.write("project", session.session_id, b"stty -echo\n")
        await manager.write(
            "project",
            session.session_id,
            b'printf \'cwd=%s secret=%s job=%s\\n\' "$PWD" "${RCP_TEST_SECRET-unset}" "$-"\n',
        )
        output = bytearray()
        async with asyncio.timeout(5):
            while b"secret=unset job=" not in output:
                output.extend(await queue.get())
        assert f"cwd={session.path} secret=unset".encode() in output
        # Bash's monitor flag proves the shell has working interactive job control.
        assert b"m" in output.split(b"secret=unset job=")[-1].splitlines()[0]
        process = manager.sessions[session.session_id].process
        await manager.end("project", session.session_id)
        assert process.poll() is not None
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["containment"] == "cooperative"
        assert receipt["termination_reason"] == "ended"
    finally:
        await manager.close()


def test_cooperative_shell_hangs_up_when_server_pty_closes(tmp_path):
    command = launch.cooperative_command({})
    assert command[:2] == ["/usr/bin/env", "-i"]
    assert "systemd-run" not in command
    assert launch._SHELL_PREFLIGHT not in command
    process, master = launch.launch(command, None, cwd=tmp_path)
    try:
        os.close(master)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            launch.stop_cooperative(process)


@pytest.mark.asyncio
async def test_restart_retires_cooperative_metadata_without_systemd(tmp_path, monkeypatch):
    manager = TerminalManager(tmp_path / "data", lambda project, member: True)
    manager.directory.mkdir(parents=True)
    session = TerminalSession(
        session_id="test",
        project_id="project",
        member_id="member",
        repository_id="repo",
        path="/checkout",
        started_at="start",
        last_activity_at="start",
        unit=f"{manager._unit_prefix}-test",
        containment="cooperative",
    )
    save_metadata(manager.directory, session)
    monkeypatch.setattr(launch, "stop_unit", lambda unit: pytest.fail("No cooperative unit exists"))
    await manager.start()
    try:
        receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
        assert receipt["termination_reason"] == "server_restart"
    finally:
        await manager.close()


def test_cooperative_shell_entry_point_works_in_packaged_backend(monkeypatch):
    from rcp import __main__
    from rcp.terminals import pty_shell

    monkeypatch.setattr(launch.sys, "frozen", True, raising=False)
    command = launch.cooperative_command({})
    assert command[-2:] == [launch.sys.executable, "_terminal-shell"]
    called = []
    monkeypatch.setattr(pty_shell, "main", lambda: called.append("shell"))
    monkeypatch.setattr(launch.sys, "argv", command[-2:])
    __main__.main()
    assert called == ["shell"]


@pytest.mark.parametrize("remote_os,expected", [("Linux", "mirrored"), ("Darwin", "cooperative")])
def test_remote_capability_uses_probed_os(manifest, monkeypatch, remote_os, expected):
    from rcp.terminals.probe import TerminalProbe

    machine = manifest.machine_map["laptop"]
    machine.host = "worker.invalid"
    monkeypatch.setattr(
        "rcp.terminals.backends.platform.system",
        lambda: "Darwin" if remote_os == "Linux" else "Linux",
    )
    monkeypatch.setattr(
        launch, "availability_diagnostic", lambda: pytest.fail("No local probe for remote machine")
    )
    capability = machine_capability(machine, TerminalProbe(remote_os, "reachable", "Ready."))
    assert capability.containment == expected
    assert capability.os_name == remote_os


def test_remote_paths_refuse_canonical_state_and_protect_symlinks(manifest, tmp_path):
    from .test_write_scope import _remote_manifest, _RemoteScopeStage

    manifest = _remote_manifest(manifest)
    stage = _RemoteScopeStage(
        overrides={
            "/declared/repo-a": "/srv/repo-a",
            "/declared/repo-b": "/srv/repo-b",
            "/srv/repo-b/.research": "/srv/canonical-b",
        }
    )
    kwargs = dict(
        manifest=manifest,
        project_id="project",
        repository_id="repo-a",
        inventory=registered_repository_roots(manifest, project_id="project"),
        data_dir=tmp_path / "data",
        remote_stage=stage,
    )
    root, protected = resolve_repository(**kwargs)
    assert str(root) == "/srv/repo-a"
    assert "/srv/canonical-b" in protected
    assert "/declared/repo-b/.research" in protected
    stage.overrides["/srv/repo-b/.research"] = "/srv/repo-a"
    with pytest.raises(TerminalUnavailable, match="Canonical state"):
        resolve_repository(**kwargs)


@pytest.mark.asyncio
async def test_remote_linux_probe_failure_cannot_launch_cooperative(
    manifest, tmp_path, monkeypatch
):
    from rcp.terminals import remote
    from rcp.terminals.probe import TerminalProbe, TerminalProbeCache

    manifest.machine_map["laptop"].host = "worker.invalid"
    manager = TerminalManager(tmp_path / "data", lambda *args: True)
    manager.probes = TerminalProbeCache(
        lambda machine: TerminalProbe("Linux", "incapable", "Linger is disabled.")
    )
    monkeypatch.setattr(
        remote,
        "start_remote",
        lambda *args, **kwargs: pytest.fail("Probe failure must refuse launch"),
    )
    await manager.start()
    try:
        with pytest.raises(TerminalUnavailable, match="Linger is disabled"):
            await manager.open(**arguments(manifest))
        assert manager.list("project") == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_an_unconfirmed_remote_unit_blocks_a_second_shell(
    manifest, tmp_path, process_factory, monkeypatch
):
    """A supervisor can exit with its own cleanup still failed.

    `run_session` writes the completion marker, then raises out of its own
    `stop_unit`, and reports that only through an exit status a dropped link
    produces too. Retiring the record on that evidence would hide a live unit
    from startup and let a later open put a second shell on the checkout.
    """
    from rcp.terminals import remote
    from rcp.terminals.probe import TerminalProbe, TerminalProbeCache

    from .test_write_scope import _remote_manifest, _RemoteScopeStage

    manifest = _remote_manifest(manifest)
    manager = TerminalManager(tmp_path / "data", lambda *args: True)
    manager.probes = TerminalProbeCache(
        lambda machine: TerminalProbe("Linux", "reachable", "Ready.")
    )
    monkeypatch.setattr(
        "rcp.terminals.manager.RemoteRunStage", lambda host: _RemoteScopeStage(host=host)
    )
    monkeypatch.setattr(
        remote, "start_remote", lambda host, **kwargs: launch.launch(["ssh-double"], None)
    )

    def unconfirmed(*args):
        raise TerminalUnavailable("systemctl is unavailable on the execution machine.")

    monkeypatch.setattr(remote, "stop_remote_unit", unconfirmed)
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        manager.sessions[session.session_id].process.returncode = 255
        await manager.sweep()
        assert session.ended_at is None
        receipt = json.loads((manager.directory / f"{session.session_id}.json").read_text())
        assert receipt["ended_at"] is None
        assert manager.list("project") == []
        with pytest.raises(TerminalUnavailable, match="may still be running"):
            await manager.open(**arguments(manifest))
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_remote_255_confirms_the_unit_before_retiring(
    manifest, tmp_path, process_factory, monkeypatch, completed
):
    from rcp.terminals import remote
    from rcp.terminals.probe import TerminalProbe, TerminalProbeCache
    from rcp.transport.remote_terminal import EXIT_PREFIX, EXIT_SUFFIX

    from .test_write_scope import _remote_manifest, _RemoteScopeStage

    manifest = _remote_manifest(manifest)
    manager = TerminalManager(tmp_path / "data", lambda *args: True)
    manager.probes = TerminalProbeCache(
        lambda machine: TerminalProbe("Linux", "reachable", "Ready.")
    )
    monkeypatch.setattr(
        "rcp.terminals.manager.RemoteRunStage", lambda host: _RemoteScopeStage(host=host)
    )
    monkeypatch.setattr(
        remote, "start_remote", lambda host, **kwargs: launch.launch(["ssh-double"], None)
    )
    confirmations = []
    monkeypatch.setattr(remote, "stop_remote_unit", lambda *args: confirmations.append(args))
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        runtime = manager.sessions[session.session_id]
        queue = manager.attach("project", session.session_id)
        if completed:
            # Completion can sit behind multiple output chunks when SSH exits.
            monkeypatch.setattr("rcp.terminals.runtime.TERMINAL_IO_CHUNK_BYTES", 5)
            os.write(process_factory[0][0], b"last output" + EXIT_PREFIX + b"255" + EXIT_SUFFIX)
        runtime.process.returncode = 255
        await manager.sweep()
        assert session.termination_reason == ("shell_exited" if completed else "link_dropped")
        # The supervisor writes its completion marker before the cleanup that
        # can fail, so a finished shell is not evidence the unit went with it.
        assert confirmations
        assert session.ended_at
        assert manager.list("project") == []
        visible = bytearray()
        while (chunk := await queue.get()) is not None:
            visible.extend(chunk)
        assert bytes(visible) == (b"last output" if completed else b"")
        receipt = json.loads((manager.directory / f"{session.session_id}.json").read_text())
        assert receipt["termination_reason"] == session.termination_reason
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_hanging_up_a_live_remote_shell_finishes_only_on_a_confirmed_stop(
    manifest, tmp_path, monkeypatch, process_factory, confirmed
):
    """Hanging up the SSH PTY asks the remote supervisor to stop its unit, but
    nothing acknowledges that. An unconfirmed stop must leave the intent
    unfinished so the next startup reconciles the unit instead of skipping it.
    """
    from rcp.terminals import remote
    from rcp.terminals.probe import TerminalProbe, TerminalProbeCache

    from .test_write_scope import _remote_manifest, _RemoteScopeStage

    manifest = _remote_manifest(manifest)
    manager = TerminalManager(tmp_path / "data", lambda *args: True)
    manager.probes = TerminalProbeCache(
        lambda machine: TerminalProbe("Linux", "reachable", "Ready.")
    )
    monkeypatch.setattr(
        "rcp.terminals.manager.RemoteRunStage", lambda host: _RemoteScopeStage(host=host)
    )
    monkeypatch.setattr(
        remote, "start_remote", lambda host, **kwargs: launch.launch(["ssh-double"], None)
    )
    stops = []

    def stop(host, unit, os_account=""):
        # Cleanup carries the recorded account so it cannot run as another one.
        stops.append((host, unit, os_account))
        if not confirmed:
            raise TerminalUnavailable("The execution machine is unreachable.")

    monkeypatch.setattr(remote, "stop_remote_unit", stop)
    await manager.start()
    try:
        session = await manager.open(**arguments(manifest))
        await manager.end("project", session.session_id)
        assert stops == [(session.execution_host, session.unit, session.declared_account)]
        assert manager.list("project") == []
        receipt = json.loads((manager.directory / f"{session.session_id}.json").read_text())
        assert bool(receipt["ended_at"]) is confirmed
        assert receipt["termination_reason"] == "ended"
    finally:
        await manager.close()


def test_local_systemd_older_than_254_omits_the_expansion_option(monkeypatch):
    """A local host hits the same systemd 254 boundary as a remote one.

    Assuming the newer manager killed every launch on the real execution host,
    which runs 249; nothing locally would have caught it.
    """
    import subprocess as sp

    def fake(command, **kwargs):
        return sp.CompletedProcess(command, 0, "systemd 249 (249.11-0ubuntu3.22)\n", "")

    assert launch.local_systemd_version(runner=fake) == 249

    def newer(command, **kwargs):
        return sp.CompletedProcess(command, 0, "systemd 257 (257.1-1)\n", "")

    assert launch.local_systemd_version(runner=newer) == 257

    def broken(command, **kwargs):
        raise OSError("no systemd-run")

    assert launch.local_systemd_version(runner=broken) == 0
