from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import tty
from pathlib import Path

import pytest

from rcp.agents.write_scope import protected_repository_paths, registered_repository_roots
from rcp.limits import TERMINAL_IDLE_TIMEOUT_SECONDS, TERMINAL_OUTPUT_BUFFER_BYTES
from rcp.terminals import TerminalManager, TerminalSession, TerminalUnavailable, launch
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

    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch, "launch", start)
    monkeypatch.setattr(launch, "stop_unit", stopped.append)
    yield slaves, commands, stopped
    for descriptor in slaves:
        os.close(descriptor)


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


def test_remote_repository_never_reaches_launcher(manifest, tmp_path):
    manifest.machine_map["laptop"].host = "worker.invalid"
    with pytest.raises(TerminalUnavailable, match="remote repositories"):
        resolve_repository(
            manifest=manifest,
            project_id="project",
            repository_id="repo-a",
            inventory=registered_repository_roots(manifest, project_id="project"),
            data_dir=tmp_path / "data",
        )


@pytest.mark.asyncio
async def test_missing_systemd_fails_closed(manifest, tmp_path, monkeypatch):
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


def test_collected_unit_cleanup_accepts_explicit_not_found(monkeypatch):
    import subprocess

    responses = iter(
        [
            subprocess.CompletedProcess([], 5, "", "Unit not loaded"),
            subprocess.CompletedProcess([], 4, "not-found\n", ""),
        ]
    )
    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch.subprocess, "run", lambda *args, **kwargs: next(responses))
    launch.stop_unit("already-collected")


def test_transient_arguments_preserve_literal_percent_dollar_and_space_paths(tmp_path):
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
    assert launch._SHELL_PREFLIGHT in argv


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
