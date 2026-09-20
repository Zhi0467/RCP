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
from rcp.terminals.backends import TERMINAL_BACKENDS, machine_capability, resolve_backend
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


def test_transient_arguments_escape_percent_and_preserve_dollar_and_space_paths(tmp_path):
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
    # systemd expands `%` specifiers in unit settings, so a literal percent
    # reaches it doubled. `--expand-environment=no` covers `$HOME`, not `%h`.
    escaped = str(repository).replace("%", "%%")
    assert f"--working-directory={escaped}" in argv
    assert f'BindPaths="{escaped}"' in argv
    # Only systemd property values are specifier-expanded; the preflight's own
    # shell arguments take the path literally and must not be doubled.
    properties = [argv[i + 1] for i, item in enumerate(argv) if item == "--property"]
    assert properties
    assert all("%h" not in value.replace("%%h", "") for value in properties)
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
@pytest.mark.parametrize("completed", [False, True])
async def test_remote_255_retires_session_with_completion_evidence(
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
    monkeypatch.setattr(
        remote,
        "stop_remote_unit",
        lambda *args: pytest.fail("Dead link must retire without another SSH connection"),
    )
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
