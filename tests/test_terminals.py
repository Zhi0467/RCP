"""Member terminal lifetime: launch profile, one shell per tree, records, stops."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import threading
import time
import tty
from dataclasses import asdict, fields
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from rcp import __main__
from rcp.agents.write_scope import protected_repository_paths, registered_repository_roots
from rcp.api.terminal_projection import terminal_session_payload
from rcp.limits import TERMINAL_IDLE_TIMEOUT_SECONDS, TERMINAL_OUTPUT_BUFFER_BYTES
from rcp.terminals import (
    TerminalManager,
    TerminalSession,
    TerminalUnavailable,
    launch,
    pty_shell,
    remote,
)
from rcp.terminals.backends import TERMINAL_BACKENDS, machine_capability, resolve_backend
from rcp.terminals.manager import manifest_registration, session_registration
from rcp.terminals.models import DETACHED
from rcp.terminals.probe import TerminalProbe, TerminalProbeCache
from rcp.terminals.runtime import read_ready
from rcp.terminals.utilities import record_path, resolve_repository, save_metadata
from rcp.transport.remote_terminal import EXIT_PREFIX, EXIT_SUFFIX

from .test_write_scope import _remote_manifest, _RemoteScopeStage

RECORD_FIELDS = {field.name for field in fields(TerminalSession)}


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


class Membership:
    """A membership check the test can revoke, and that records who asked."""

    def __init__(self) -> None:
        self.allowed = True
        self.checks: list[str] = []

    def __call__(self, project: str, member: str) -> bool:
        self.checks.append(member)
        return self.allowed


def unstoppable(*args):
    raise TerminalUnavailable("systemctl user manager unavailable")


def expire(runtime):
    runtime.last_activity = time.monotonic() - TERMINAL_IDLE_TIMEOUT_SECONDS - 1


def arguments(manifest, repository_alias="repo-a", project_id="project"):
    return dict(
        project_id=project_id,
        member_id="member",
        manifest=manifest,
        repository_alias=repository_alias,
        repository_inventory=registered_repository_roots(manifest, project_id=project_id),
    )


def session_record(manager, name, **overrides) -> TerminalSession:
    """A session as this manager persists one: local and mirrored unless told otherwise."""
    values = dict(
        session_id=name,
        project_id="project",
        member_id="member",
        repository_id="repo-a",
        path="/checkout",
        started_at="start",
        last_activity_at="start",
        unit=f"{manager._unit_prefix}-{name}",
        containment="mirrored",
    )
    values.update(overrides)
    return TerminalSession(**values)


def write_record(manager, name, **overrides) -> Path:
    """Write `<name>.json` as `save_metadata` would.

    Overrides may name fields no version knows; an override of `...` drops the field.
    """
    known = {k: v for k, v in overrides.items() if k in RECORD_FIELDS and v is not ...}
    payload = asdict(session_record(manager, name, **known))
    payload.update({key: value for key, value in overrides.items() if value is not ...})
    for key in [key for key, value in overrides.items() if value is ...]:
        del payload[key]
    path = manager.directory / f"{name}.json"
    path.write_text(json.dumps(payload))
    return path


def read_record(manager, name) -> dict:
    return json.loads((manager.directory / f"{name}.json").read_text())


def rename_alias(manifest, old, new, **changes):
    """Drop `old` from the manifest and register the same repository as `new`."""
    repository = manifest.repository_map[old]
    manifest.repositories = [item for item in manifest.repositories if item.alias != old] + [
        repository.model_copy(update={"alias": new, **changes})
    ]


@pytest.fixture
def doubles(monkeypatch):
    """Local Linux with a PTY double for every launch; `.stopped` records unit stops."""
    state = SimpleNamespace(slaves=[], commands=[], stopped=[])

    def start(command, unit):
        master, slave = os.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        state.slaves.append(slave)
        state.commands.append(command)
        return FakeProcess(), master

    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch, "availability_diagnostic", lambda: None)
    monkeypatch.setattr(launch, "launch", start)
    monkeypatch.setattr(launch, "stop_unit", state.stopped.append)
    yield state
    for descriptor in state.slaves:
        os.close(descriptor)


@pytest.fixture
def membership():
    return Membership()


@pytest_asyncio.fixture
async def unstarted(tmp_path, membership, monkeypatch):
    """A manager whose record directory exists and whose startup has not run.

    Tests that reconcile records write them here first. The background sweep is
    disabled so every test drives `sweep()` itself.
    """

    async def never_sweeps(manager):
        await asyncio.Event().wait()

    monkeypatch.setattr("rcp.terminals.manager.sweep_loop", never_sweeps)
    manager = TerminalManager(tmp_path / "data", membership)
    manager.directory.mkdir(parents=True)
    yield manager
    with contextlib.suppress(TerminalUnavailable):
        await manager.close()


@pytest_asyncio.fixture
async def manager(unstarted):
    await unstarted.start()
    return unstarted


@pytest_asyncio.fixture
async def remote_machine(manifest, manager, doubles, monkeypatch):
    """A reachable remote Linux machine whose SSH launch is a PTY double.

    `overrides` maps declared paths to what the execution machine resolves,
    `stops` records confirmed unit stops, and `failure` makes them unconfirmed.
    """
    setup = SimpleNamespace(
        manifest=_remote_manifest(manifest), overrides={}, stops=[], failure=None
    )
    replaced = manager.probes
    manager.probes = TerminalProbeCache(
        lambda machine: TerminalProbe("Linux", "reachable", "Ready.")
    )
    await replaced.close()
    monkeypatch.setattr(
        "rcp.terminals.manager.RemoteRunStage",
        lambda host: _RemoteScopeStage(host=host, overrides=setup.overrides),
    )
    monkeypatch.setattr(
        remote, "start_remote", lambda host, **kwargs: launch.launch(["ssh-double"], None)
    )

    def stop(host, unit, os_account=""):
        if setup.failure is not None:
            raise setup.failure
        setup.stops.append((host, unit, os_account))

    monkeypatch.setattr(remote, "stop_remote_unit", stop)
    return setup


# --- Launch profile and repository resolution -------------------------------


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


def test_remote_paths_refuse_canonical_state_and_protect_symlinks(manifest, tmp_path):
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


def test_transient_arguments_pass_percent_and_space_paths_through_literally(tmp_path):
    """A transient unit is built over D-Bus, not parsed from a unit file, so
    systemd applies no specifier expansion to it: `%h` stays `%h`, and doubling
    it breaks a real path.
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


def test_a_record_path_refuses_an_identifier_that_leaves_its_directory(tmp_path):
    """Every `save_metadata` caller sits behind this guard."""
    assert record_path(tmp_path, "ordinary") == tmp_path / "ordinary.json"
    for identifier in ("", "../rcp-server", "nested/session", "/etc/rcp-absolute"):
        with pytest.raises(TerminalUnavailable, match="does not name a record"):
            record_path(tmp_path, identifier)


# --- Launcher and preflight -------------------------------------------------


def test_failed_required_profile_never_launches_a_fallback(monkeypatch):
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


def _preflight(tmp_path, *, findmnt_output, protected, prelude=""):
    findmnt = tmp_path / "findmnt"
    findmnt.write_text(f"#!/bin/sh\nprintf '{findmnt_output}\\n'\n")
    findmnt.chmod(0o755)
    return subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            prelude + launch._SHELL_PREFLIGHT,
            "rcp-terminal",
            protected,
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
    )


def test_preflight_refuses_a_writable_canonical_directory(tmp_path):
    state = tmp_path / ".research"
    state.mkdir()
    result = _preflight(tmp_path, findmnt_output="ro", protected=str(state))
    assert result.returncode == 1
    assert launch._READY_MARKER not in result.stdout
    assert b"canonical-state read-only mounts are unavailable" in result.stderr


def test_preflight_refuses_a_writable_mount_even_when_directory_permissions_deny_write(tmp_path):
    # Model directory permissions only; the effective-mount check is what is
    # under test, and this host has no mount privileges.
    permission_probe = 'test() { case "$1" in -w) [[ "$2" == "$PWD" ]];; -e) return 0;; esac; };\n'
    result = _preflight(
        tmp_path,
        findmnt_output="rw,relatime",
        protected=str(tmp_path / ".research"),
        prelude=permission_probe,
    )
    assert result.returncode == 1
    assert launch._READY_MARKER not in result.stdout
    assert b"canonical-state mount verification failed" in result.stderr


def test_preflight_refuses_a_service_manager_that_expanded_its_own_variables(tmp_path):
    # A manager older than 254 rejects `--expand-environment=no`, so a launch
    # there omits it. If such a manager does expand the command line, every
    # `$name` it does not know is emptied before bash reads the script.
    expanded = re.sub(r"\$\{?(\w+)\}?", "", launch._SHELL_PREFLIGHT)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", expanded, "rcp-terminal", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "rewrote the containment preflight" in result.stderr


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
    """`TimeoutExpired` is neither an OSError nor a RuntimeError, so one escaping
    the stop path would abort startup reconciliation instead of leaving its
    record for a later attempt.
    """

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


def test_local_systemd_older_than_254_omits_the_expansion_option():
    """A local host hits the same systemd 254 boundary as a remote one; the
    real execution host runs 249.
    """

    def version(text):
        return lambda command, **kwargs: subprocess.CompletedProcess(command, 0, text, "")

    assert launch.local_systemd_version(runner=version("systemd 249 (249.11-0ubuntu3.22)\n")) == 249
    assert launch.local_systemd_version(runner=version("systemd 257 (257.1-1)\n")) == 257

    def broken(command, **kwargs):
        raise OSError("no systemd-run")

    assert launch.local_systemd_version(runner=broken) == 0


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


def test_cooperative_shell_entry_point_works_in_packaged_backend(monkeypatch):
    monkeypatch.setattr(launch.sys, "frozen", True, raising=False)
    command = launch.cooperative_command({})
    assert command[-2:] == [launch.sys.executable, "_terminal-shell"]
    called = []
    monkeypatch.setattr(pty_shell, "main", lambda: called.append("shell"))
    monkeypatch.setattr(launch.sys, "argv", command[-2:])
    __main__.main()
    assert called == ["shell"]


# --- Machine capability -----------------------------------------------------


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


def test_a_local_machine_without_a_runnable_shell_is_not_offered(manifest, monkeypatch):
    """The cooperative helper writes its readiness marker before execv, so an
    unrunnable shell would otherwise admit a session that is already gone.
    """
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Darwin")
    monkeypatch.setattr("rcp.terminals.backends.os.access", lambda path, mode: False)
    capability = machine_capability(manifest.machine_map["laptop"])
    assert capability.backend is None
    assert "/bin/bash" in capability.reason


@pytest.mark.parametrize("remote_os,expected", [("Linux", "mirrored"), ("Darwin", "cooperative")])
def test_remote_capability_uses_probed_os(manifest, monkeypatch, remote_os, expected):
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


@pytest.mark.asyncio
async def test_missing_systemd_fails_closed(manifest, manager, monkeypatch):
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Linux")
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    with pytest.raises(TerminalUnavailable, match="systemd-run.*not installed"):
        await manager.open(**arguments(manifest))
    assert manager.list("project") == []
    assert list(manager.directory.glob("*.json")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", ["mount profile rejected", "findmnt verification failed"])
async def test_mirrored_launch_failure_never_creates_cooperative_session(
    manifest, manager, doubles, monkeypatch, diagnostic
):
    commands = []

    def fail(command, unit, **kwargs):
        commands.append(command)
        raise TerminalUnavailable(diagnostic)

    monkeypatch.setattr(launch, "launch", fail)
    with pytest.raises(TerminalUnavailable, match=diagnostic):
        await manager.open(**arguments(manifest))
    assert len(commands) == 1
    assert commands[0][0] == "systemd-run"
    assert manager.list("project") == []
    receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
    assert receipt["containment"] == "mirrored"
    # The intent is persisted before the launch, and this double's unit stop
    # succeeds, so the record finishes rather than waiting for startup.
    assert receipt["ended_at"]
    assert receipt["termination_reason"] == "launch_failed"


@pytest.mark.asyncio
async def test_remote_linux_probe_failure_cannot_launch_cooperative(manifest, manager, monkeypatch):
    manifest.machine_map["laptop"].host = "worker.invalid"
    manager.probes = TerminalProbeCache(
        lambda machine: TerminalProbe("Linux", "incapable", "Linger is disabled.")
    )
    monkeypatch.setattr(
        remote,
        "start_remote",
        lambda *args, **kwargs: pytest.fail("Probe failure must refuse launch"),
    )
    with pytest.raises(TerminalUnavailable, match="Linger is disabled"):
        await manager.open(**arguments(manifest))
    assert manager.list("project") == []


@pytest.mark.asyncio
async def test_cooperative_pty_reports_missing_protection_and_scrubs_environment(
    manifest, manager, monkeypatch
):
    monkeypatch.setattr("rcp.terminals.backends.platform.system", lambda: "Darwin")
    monkeypatch.setenv("RCP_TEST_SECRET", "must-not-inherit")

    def unexpected(*args, **kwargs):
        pytest.fail("A cooperative PTY must not probe or stop systemd")

    monkeypatch.setattr(launch, "availability_diagnostic", unexpected)
    monkeypatch.setattr(launch, "stop_unit", unexpected)
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


@pytest.mark.asyncio
async def test_restart_retires_cooperative_metadata_without_systemd(unstarted, monkeypatch):
    save_metadata(unstarted.directory, session_record(unstarted, "test", containment="cooperative"))
    monkeypatch.setattr(launch, "stop_unit", lambda unit: pytest.fail("No cooperative unit exists"))
    await unstarted.start()
    assert read_record(unstarted, "test")["termination_reason"] == "server_restart"


# --- Session lifecycle ------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_gate_precedes_launch(manifest, manager, membership, doubles):
    membership.allowed = False
    with pytest.raises(PermissionError, match="membership"):
        await manager.open(**arguments(manifest))
    assert not doubles.commands


@pytest.mark.asyncio
async def test_shared_session_survives_navigation_and_streams_pty(manifest, manager, doubles):
    session = await manager.open(**arguments(manifest))
    again = await manager.open(**{**arguments(manifest), "member_id": "second-member"})
    assert session is again
    assert len(doubles.commands) == 1
    queue = manager.attach("project", session.session_id)
    assert session.state == "live"
    os.write(doubles.slaves[0], b"hello terminal")
    assert await asyncio.wait_for(queue.get(), 2) == b"hello terminal"
    manager.detach("project", session.session_id, queue)
    assert session.state == "idle"
    assert manager.get("project", session.session_id) is session
    restored = manager.attach("project", session.session_id)
    assert restored.get_nowait() == b"hello terminal"
    await manager.write("project", session.session_id, b"git status\n")
    assert os.read(doubles.slaves[0], 1024) == b"git status\n"
    manager.resize("project", session.session_id, 100, 40)
    with pytest.raises(KeyError):
        manager.get("different-project", session.session_id)
    await manager.end("project", session.session_id)
    assert await restored.get() is None
    assert manager.list("project") == []
    receipt = read_record(manager, session.session_id)
    assert receipt["termination_reason"] == "ended"
    assert receipt["member_id"] == "member"
    assert "hello terminal" not in json.dumps(receipt)
    assert "git status" not in json.dumps(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle_timeout", "membership_lost", "shell_exited"])
async def test_lifecycle_ends_each_session(manifest, manager, membership, doubles, reason):
    session = await manager.open(**arguments(manifest))
    runtime = manager.sessions[session.session_id]
    queue = manager.attach("project", session.session_id)
    if reason == "idle_timeout":
        expire(runtime)
        os.write(doubles.slaves[0], b"noisy forgotten command")
        read_ready(runtime)
    elif reason == "membership_lost":
        membership.allowed = False
    else:
        runtime.process.returncode = 0
    await manager.sweep()
    assert session.termination_reason == reason
    assert session.ended_at is not None
    assert doubles.stopped == [session.unit]
    assert manager.list("project") == []
    assert None in list(queue._queue)


@pytest.mark.asyncio
async def test_a_viewer_that_falls_behind_is_detached_not_ended(manifest, manager, doubles):
    """A full subscriber queue must not reuse the session-end sentinel, or a
    slow connection reports a running shell as terminated.
    """
    session = await manager.open(**arguments(manifest))
    runtime = manager.sessions[session.session_id]
    queue = manager.attach("project", session.session_id)
    while not queue.full():
        queue.put_nowait(b"backlog")
    os.write(doubles.slaves[0], b"one more chunk")
    while read_ready(runtime):
        pass
    assert queue.get_nowait() is DETACHED
    assert queue not in runtime.subscribers
    assert manager.list("project") == [session]
    assert session.ended_at is None


@pytest.mark.asyncio
async def test_replay_is_bounded_and_never_saved(manifest, manager, doubles):
    session = await manager.open(**arguments(manifest))
    runtime = manager.sessions[session.session_id]
    runtime.replay.extend(b"x" * TERMINAL_OUTPUT_BUFFER_BYTES)
    os.write(doubles.slaves[0], b"new-output")
    read_ready(runtime)
    assert len(runtime.replay) == TERMINAL_OUTPUT_BUFFER_BYTES
    assert runtime.replay.endswith(b"new-output")
    assert all(
        "replay" not in json.loads(path.read_text()) for path in manager.directory.glob("*.json")
    )


async def _backpressured_write(manager, session):
    """A write nothing drains, so it fills the PTY and waits for space."""
    writing = asyncio.create_task(
        manager.write("project", session.session_id, b"x" * (8 * 1024 * 1024))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not writing.done(), "the PTY took the whole write, so nothing was waiting"
    return writing


@pytest.mark.asyncio
async def test_input_stops_when_its_session_ends_mid_write(manifest, manager, doubles):
    """The descriptor a waiting write holds is closed when the session ends and
    its number can be handed to another shell, so the runtime is checked again
    before the write resumes.
    """
    session = await manager.open(**arguments(manifest))
    writing = await _backpressured_write(manager, session)
    try:
        await manager.end("project", session.session_id)
        with pytest.raises(KeyError):
            await writing
    finally:
        writing.cancel()
        with contextlib.suppress(asyncio.CancelledError, KeyError):
            await writing


@pytest.mark.asyncio
async def test_input_renews_the_lifetime_before_the_pty_drains_it(manifest, manager, doubles):
    session = await manager.open(**arguments(manifest))
    expire(manager.sessions[session.session_id])
    writing = await _backpressured_write(manager, session)
    try:
        await manager.sweep()
        assert manager.get("project", session.session_id) is session
    finally:
        writing.cancel()
        with contextlib.suppress(asyncio.CancelledError, KeyError):
            await writing


@pytest.mark.asyncio
async def test_input_during_a_sweeps_wait_renews_the_lifetime(manifest, manager, doubles):
    """A sweep that decided a session was idle before waiting for the lock would
    act on that decision after the input that renewed it had already arrived.
    """
    session = await manager.open(**arguments(manifest))
    expire(manager.sessions[session.session_id])
    await manager._lock.acquire()
    sweeping = asyncio.create_task(manager.sweep())
    # Let the pass get as far as it can before the lock stops it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await manager.write("project", session.session_id, b"still here\n")
    manager._lock.release()
    await sweeping
    assert manager.get("project", session.session_id) is session
    assert session.termination_reason is None


@pytest.mark.asyncio
async def test_one_blocked_launch_does_not_stall_another_repository(manifest, manager, monkeypatch):
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
    try:
        blocked = asyncio.create_task(manager.open(**arguments(manifest)))
        assert await asyncio.to_thread(started.wait, 5)
        # The first launch still holds its worker thread here.
        await asyncio.wait_for(manager.open(**arguments(manifest, "repo-b")), timeout=5)
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


# --- Stops: concurrency and failure ------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_stops_every_shell_at_once(manifest, manager, doubles, monkeypatch):
    """Shutdown holds the instance lock, and a replacement server waits only so
    long for this one to go, so stops must overlap. A barrier proves the
    overlap without timing anything: a serial shutdown never brings a second
    stop here, and the barrier breaks.
    """
    meeting = threading.Barrier(2)
    monkeypatch.setattr(launch, "stop_unit", lambda unit: meeting.wait(timeout=5))
    for alias in ("repo-a", "repo-b"):
        await manager.open(**arguments(manifest, alias))
    assert len(manager.sessions) == 2
    try:
        await manager.close()
        assert manager.sessions == {}
    finally:
        meeting.abort()


@pytest.mark.asyncio
async def test_stale_aliases_are_retired_together(
    manifest, manager, doubles, monkeypatch, tmp_path
):
    """Every caller of this is a member waiting on a listing, an open or an
    attach, and a stale alias whose machine has gone costs a stop timeout.
    """
    meeting = threading.Barrier(2)
    for alias in ("repo-a", "repo-b"):
        await manager.open(**arguments(manifest, alias))
    assert len(manager.sessions) == 2
    # Both aliases stop naming what their shells opened on.
    for alias in ("repo-a", "repo-b"):
        moved = tmp_path / f"moved-{alias}"
        (moved / ".research").mkdir(parents=True)
        manifest.repository_map[alias].path = str(moved)
    monkeypatch.setattr(launch, "stop_unit", lambda unit: meeting.wait(timeout=5))
    try:
        await manager.reconcile_registrations("project", manifest)
        assert manager.list("project") == []
    finally:
        meeting.abort()


@pytest.mark.asyncio
async def test_expired_sessions_are_classified_before_any_stop(
    manifest, manager, membership, doubles, monkeypatch
):
    """A stop can spend its timeout against a machine that has gone. Deciding
    and stopping one session at a time would let that one shell delay even the
    membership check for every session behind it.
    """
    for alias in ("repo-a", "repo-b"):
        await manager.open(**arguments(manifest, alias))
    checks_before_each_stop = []
    membership.allowed = False
    membership.checks.clear()
    monkeypatch.setattr(
        launch, "stop_unit", lambda unit: checks_before_each_stop.append(len(membership.checks))
    )
    await manager.sweep()
    assert manager.list("project") == []
    # Both shells were decided on before either stop began.
    assert checks_before_each_stop == [2, 2]


@pytest.mark.asyncio
async def test_failed_stop_keeps_metadata_and_can_be_retried(
    manifest, manager, doubles, monkeypatch
):
    session = await manager.open(**arguments(manifest))
    expire(manager.sessions[session.session_id])
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    with pytest.raises(TerminalUnavailable, match="systemctl"):
        await manager.sweep()
    # Kept for the retry, and unreachable while its shell may still be running.
    assert manager.sessions[session.session_id].retiring == "idle_timeout"
    with pytest.raises(KeyError):
        manager.get("project", session.session_id)
    assert manager.list("project") == []
    assert session.ended_at is None
    monkeypatch.setattr(launch, "stop_unit", doubles.stopped.append)
    await manager.sweep()
    assert session.termination_reason == "idle_timeout"
    assert session.ended_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["sweep", "close", "end_all"])
async def test_one_failed_stop_does_not_starve_other_sessions(
    manifest, manager, doubles, monkeypatch, operation
):
    first = await manager.open(**arguments(manifest))
    second = await manager.open(**arguments(manifest, "repo-b"))
    for runtime in manager.sessions.values():
        expire(runtime)

    def partial_failure(unit):
        if unit == first.unit:
            raise TerminalUnavailable("first unit cannot be stopped")
        doubles.stopped.append(unit)

    monkeypatch.setattr(launch, "stop_unit", partial_failure)
    decided_by = {
        "sweep": "idle_timeout",
        "close": "server_shutdown",
        "end_all": "server_maintenance",
    }
    with pytest.raises(TerminalUnavailable, match="first unit"):
        await getattr(manager, operation)()
    # Retained for the retry, carrying the reason that decided it, and no
    # longer a session a member can reach or be handed back.
    assert manager.sessions[first.session_id].retiring == decided_by[operation]
    with pytest.raises(KeyError):
        manager.get("project", first.session_id)
    assert manager.list("project") == []
    assert second.ended_at is not None
    assert second.unit in doubles.stopped


@pytest.mark.asyncio
async def test_a_failed_retirement_drops_the_output_still_queued(
    manifest, manager, doubles, monkeypatch
):
    """A viewer already attached reads its queue without consulting the manager
    again. The decision is that it stops receiving from this session, so what
    is queued goes with the session rather than ahead of the end signal.
    """
    session = await manager.open(**arguments(manifest))
    queue = manager.attach("project", session.session_id)
    queue.put_nowait(b"output from a checkout it may no longer name")
    expire(manager.sessions[session.session_id])
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    with pytest.raises(TerminalUnavailable, match="systemctl"):
        await manager.sweep()
    assert queue.get_nowait() is None
    assert queue.empty()


@pytest.mark.asyncio
async def test_a_retiring_shell_blocks_its_tree_under_a_new_alias(
    manifest, manager, doubles, monkeypatch
):
    """A failed local stop leaves no retained record, only a runtime marked
    retiring. Settings can drop the alias it carries and register the same
    checkout under another; the shell that may still be running is the same.
    """
    # Not the canonical-state repository, which settings cannot unregister.
    session = await manager.open(**arguments(manifest, "repo-b"))
    expire(manager.sessions[session.session_id])
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    with pytest.raises(TerminalUnavailable, match="systemctl"):
        await manager.sweep()
    assert manager.list("project") == []
    rename_alias(manifest, "repo-b", "repo-b-renamed")
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await manager.open(**arguments(manifest, "repo-b-renamed"))


# --- Cancelled opens --------------------------------------------------------


@pytest.fixture
def delayed_launch(monkeypatch):
    """A launch that waits in its worker thread until the test releases it."""
    gate = SimpleNamespace(started=threading.Event(), release=threading.Event(), launched=[])
    original = launch.launch

    def delayed(command, unit):
        gate.started.set()
        assert gate.release.wait(5)
        gate.launched.append(original(command, unit))
        return gate.launched[-1]

    monkeypatch.setattr(launch, "launch", delayed)
    yield gate
    gate.release.set()


@pytest.mark.asyncio
async def test_open_cancellation_stops_the_inflight_launch(
    manifest, manager, doubles, delayed_launch
):
    opening = asyncio.create_task(manager.open(**arguments(manifest)))
    assert await asyncio.to_thread(delayed_launch.started.wait, 5)
    opening.cancel()
    delayed_launch.release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert manager.list("project") == []
    assert len(doubles.stopped) == 1
    receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
    assert receipt["termination_reason"] == "opening_cancelled"


@pytest.mark.asyncio
async def test_an_open_cancelled_while_publishing_does_not_leave_its_shell(
    manifest, manager, doubles, delayed_launch
):
    """Publication waits for the manager lock, which an end can hold across a
    `systemctl` call. The shell already exists by then, so a requester that
    leaves during that wait must not strand it.
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

    lock = SignallingLock()
    manager._lock = lock
    opening = asyncio.create_task(manager.open(**arguments(manifest)))
    assert await asyncio.to_thread(delayed_launch.started.wait, 5)
    await lock.acquire()
    delayed_launch.release.set()
    await asyncio.wait_for(lock.waiting.wait(), 5)
    opening.cancel()
    lock.release()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert manager.sessions == {}
    process, master_fd = delayed_launch.launched[0]
    with pytest.raises(OSError):
        os.fstat(master_fd)
    assert process.poll() is not None
    receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
    assert receipt["termination_reason"] == "opening_cancelled"
    assert receipt["unit"] in doubles.stopped


@pytest.mark.asyncio
async def test_a_cancelled_open_whose_stop_fails_leaves_no_reusable_session(
    manifest, manager, doubles, delayed_launch, monkeypatch
):
    """The launch wins the race, then its unit refuses to stop. Nothing drains
    this PTY, because a cancelled open never registers a reader, so the runtime
    must not be published; and a stop that raises must not leave it in
    `sessions` where neither the sweep nor startup retires it.
    """
    opening = asyncio.create_task(manager.open(**arguments(manifest)))
    assert await asyncio.to_thread(delayed_launch.started.wait, 5)
    opening.cancel()
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    delayed_launch.release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert manager.sessions == {}
    # The record retains the unit; the descriptor and process are given back,
    # or repeated cancellations would spend the server's descriptors.
    process, master_fd = delayed_launch.launched[0]
    with pytest.raises(OSError):
        os.fstat(master_fd)
    assert process.poll() is not None
    receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
    assert receipt["termination_reason"] == "opening_cancelled"
    assert receipt["ended_at"] is None
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await manager.open(**arguments(manifest))


@pytest.mark.asyncio
async def test_a_cancelled_open_whose_launch_then_fails_still_tracks_its_unit(
    manifest, manager, doubles, monkeypatch
):
    """The failure is raised while the cancellation is being handled, so it
    cannot reach the ordinary launch-failure branch; its record must still be
    tracked, or the next attempt puts a second shell on the checkout.
    """
    started = threading.Event()
    release = threading.Event()

    def delayed(command, unit):
        started.set()
        assert release.wait(5)
        raise TerminalUnavailable("systemd-run did not confirm the required terminal profile.")

    monkeypatch.setattr(launch, "launch", delayed)
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
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
    with pytest.raises(TerminalUnavailable, match="until that one is gone"):
        await manager.open(**arguments(manifest))


@pytest.mark.asyncio
async def test_a_cancel_during_the_membership_end_does_not_end_it_again(
    manifest, manager, membership, doubles, monkeypatch
):
    """`end_runtime` finishes its cleanup before it re-raises a cancellation, so
    ending again would overwrite why the session ended and act on a descriptor
    another launch may already have been handed.
    """
    stopping = threading.Event()
    release = threading.Event()
    stops = []

    def stop(unit):
        stops.append(unit)
        stopping.set()
        assert release.wait(5)

    monkeypatch.setattr(launch, "stop_unit", stop)

    def admitted_once(project, member):
        # Admitted on entry, gone by the time the shell is published.
        membership.checks.append(member)
        return len(membership.checks) == 1

    manager.membership_check = admitted_once
    opening = asyncio.create_task(manager.open(**arguments(manifest)))
    assert await asyncio.to_thread(stopping.wait, 5)
    opening.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    records = list(manager.directory.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["termination_reason"] == "membership_lost"
    assert len(stops) == 1


# --- Registrations and one shell per working tree -----------------------------


@pytest.mark.asyncio
async def test_repointing_a_repository_does_not_hand_back_the_old_checkout(
    manifest, manager, doubles, tmp_path
):
    first = await manager.open(**arguments(manifest))
    assert first.declared_path == manifest.repository_map["repo-a"].path
    moved = tmp_path / "moved-checkout"
    (moved / ".research").mkdir(parents=True)
    manifest.repository_map["repo-a"].path = str(moved)
    second = await manager.open(**arguments(manifest))
    assert second.session_id != first.session_id
    assert second.declared_path == str(moved)
    assert session_registration(second) != session_registration(first)
    assert [session.session_id for session in manager.list("project")] == [second.session_id]
    assert read_record(manager, first.session_id)["termination_reason"] == "repository_repointed"


@pytest.mark.asyncio
async def test_moving_a_repository_to_another_account_does_not_hand_back_the_old_shell(
    manifest, manager, doubles
):
    """Two machines can share a path string, so the declared path alone does
    not identify a registration; the machine and its account belong to it too.
    """
    first = await manager.open(**arguments(manifest))
    assert first.declared_machine == manifest.repository_map["repo-a"].machine
    machine = manifest.machine_map[manifest.repository_map["repo-a"].machine]
    machine.os_account = "someone-else"
    assert manifest_registration(manifest, "repo-a") != session_registration(first)
    second = await manager.open(**arguments(manifest))
    assert second.session_id != first.session_id
    assert second.declared_account == "someone-else"
    assert read_record(manager, first.session_id)["termination_reason"] == "repository_repointed"


@pytest.mark.asyncio
async def test_a_rename_during_an_open_cannot_start_a_second_shell_on_the_tree(
    manifest, manager, doubles, monkeypatch
):
    """The alias reservation is only the tree's name. Renaming the registration
    while an open is still launching lets the next request arrive under a
    different alias, and neither shell is in `sessions` yet for the other's
    admission check to find.
    """
    launching = threading.Event()
    release = threading.Event()
    launched = []
    original = launch.launch

    def slow(command, unit):
        launched.append(unit)
        if len(launched) == 1:
            launching.set()
            assert release.wait(5)
        return original(command, unit)

    monkeypatch.setattr(launch, "launch", slow)
    opening = asyncio.create_task(manager.open(**arguments(manifest, "repo-b")))
    try:
        assert await asyncio.to_thread(launching.wait, 5)
        rename_alias(manifest, "repo-b", "repo-b-again")
        with pytest.raises(TerminalUnavailable, match="already opening on this working tree"):
            await manager.open(**arguments(manifest, "repo-b-again"))
        assert len(launched) == 1
    finally:
        release.set()
        with contextlib.suppress(Exception):
            await opening


@pytest.mark.asyncio
async def test_one_working_tree_holds_one_shell_across_projects(manifest, manager, doubles):
    """A checkout can leave one project's manifest and arrive in another while
    the first still has a shell on it. Which project asked is not what decides.
    """
    await manager.open(**arguments(manifest, "repo-b"))
    with pytest.raises(TerminalUnavailable, match="already open on this working tree"):
        await manager.open(**arguments(manifest, "repo-b", project_id="other-project"))


@pytest.mark.asyncio
async def test_two_remote_declarations_of_one_tree_hold_one_shell(manager, remote_machine):
    """A remote declaration is resolved on its own machine, so two of them can
    name one tree, and what each session resolved is the only place they meet.
    """
    manifest = remote_machine.manifest
    remote_machine.overrides.update(
        {"/declared/repo-b": "/canonical/tree", "/declared/moved": "/canonical/tree"}
    )
    first = await manager.open(**arguments(manifest, "repo-b"))
    assert (first.path, first.declared_path) == ("/canonical/tree", "/declared/repo-b")
    rename_alias(manifest, "repo-b", "repo-b-moved", path="/declared/moved")
    with pytest.raises(TerminalUnavailable, match="already open on this working tree"):
        await manager.open(**arguments(manifest, "repo-b-moved"))


@pytest.mark.asyncio
async def test_a_retained_record_blocks_the_tree_it_resolved(
    manifest, manager, doubles, monkeypatch, tmp_path
):
    """A record is matched by the tree its shell resolved, never by its
    declaration: a symlink and its target name one tree, and the link can be
    repointed after the shell started.
    """
    repository = manifest.repository_map["repo-a"]
    link = tmp_path / "moving-link"
    link.symlink_to(Path(repository.path))
    manager.mark_unresolved(
        session_record(
            manager,
            "earlier",
            repository_id="alias-since-dropped",
            path=str(Path(repository.path).resolve()),
            declared_path=str(link),
            declared_machine=repository.machine,
        )
    )
    # The declaration now names repo-b, but the shell holds repo-a.
    link.unlink()
    link.symlink_to(Path(manifest.repository_map["repo-b"].path).resolve())
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await manager.open(**arguments(manifest))
    await manager.open(**arguments(manifest, "repo-b"))


@pytest.mark.asyncio
async def test_a_retained_remote_record_blocks_its_tree_under_another_spelling(
    manager, remote_machine
):
    """Nothing live remains for the reservation to find, so the record is what
    has to be matched, by what each side resolved on the execution machine.
    """
    manifest = remote_machine.manifest
    remote_machine.overrides.update(
        {"/declared/repo-b": "/canonical/tree", "/declared/moved": "/canonical/tree"}
    )
    machine = manifest.machine_map["laptop"]
    manager.mark_unresolved(
        session_record(
            manager,
            "earlier",
            repository_id="repo-b",
            path="/canonical/tree",
            declared_path="/declared/repo-b",
            declared_machine="laptop",
            declared_account=machine.os_account,
            execution_host=machine.host,
        )
    )
    remote_machine.failure = TerminalUnavailable("The execution machine is unreachable.")
    rename_alias(manifest, "repo-b", "repo-b-moved", path="/declared/moved")
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await manager.open(**arguments(manifest, "repo-b-moved"))


@pytest.mark.asyncio
async def test_a_renamed_alias_cannot_open_over_the_old_alias_blocker(
    manifest, unstarted, doubles, monkeypatch
):
    """Settings can drop an alias and register the same checkout under another
    name. A blocker filed under the old alias still names a unit on the very
    working tree the new alias is about to open.
    """
    checkout = Path(manifest.repository_map["repo-b"].path).resolve()
    write_record(unstarted, "stranded", repository_id="repo-b", path=str(checkout))
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    await unstarted.start()
    rename_alias(manifest, "repo-b", "repo-b-renamed")
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await unstarted.open(**arguments(manifest, "repo-b-renamed"))


# --- Records: startup reconciliation -----------------------------------------


@pytest.mark.asyncio
async def test_restart_cleans_only_own_unfinished_metadata(unstarted, doubles):
    session = session_record(unstarted, "test", repository_id="repo")
    save_metadata(unstarted.directory, session)
    await unstarted.start()
    assert doubles.stopped == [session.unit]
    recorded = read_record(unstarted, "test")
    assert recorded["termination_reason"] == "server_restart"
    assert recorded["ended_at"]


@pytest.mark.asyncio
async def test_startup_retires_a_record_belonging_to_another_data_directory(unstarted, monkeypatch):
    """The unit prefix hashes the data directory, so moving it makes every
    unfinished record foreign. That must not be a permanent startup failure.
    """
    save_metadata(
        unstarted.directory,
        session_record(unstarted, "foreign", unit="rcp-terminal-000000000000-foreign"),
    )
    monkeypatch.setattr(
        remote, "stop_remote_unit", lambda *args: pytest.fail("A foreign unit is not ours to stop")
    )
    monkeypatch.setattr(
        launch, "stop_unit", lambda *args: pytest.fail("A foreign unit is not ours to stop")
    )
    await unstarted.start()
    recorded = read_record(unstarted, "foreign")
    assert recorded["ended_at"]
    assert recorded["termination_reason"] == "unit_identity_mismatch"


@pytest.mark.asyncio
async def test_a_record_whose_unit_cannot_be_stopped_is_retained_and_blocks(
    manifest, unstarted, doubles, monkeypatch
):
    """Startup is the first owner to run, so a stop that fails must retain the
    record rather than refuse the boot; the retained record is the retry, and
    it refuses a reopen until the unit is confirmed gone.
    """
    write_record(unstarted, "stranded", execution_host="worker.invalid")

    def unreachable(*args):
        raise TerminalUnavailable("The execution machine is unreachable.")

    monkeypatch.setattr(remote, "stop_remote_unit", unreachable)
    await unstarted.start()
    assert read_record(unstarted, "stranded")["ended_at"] is None
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await unstarted.open(**arguments(manifest))


@pytest.mark.asyncio
async def test_a_retained_record_refuses_a_reopen_until_its_unit_is_gone(
    manifest, unstarted, doubles, monkeypatch
):
    """That unit is not in `sessions`, so nothing else stops a member opening a
    second shell on the checkout while the first may still be writing it.
    """
    write_record(unstarted, "stranded", path=manifest.repository_map["repo-a"].path)
    stoppable = False
    monkeypatch.setattr(launch, "stop_unit", lambda unit: None if stoppable else unstoppable())
    await unstarted.start()
    with pytest.raises(TerminalUnavailable, match="until that one is gone"):
        await unstarted.open(**arguments(manifest))
    assert unstarted.list("project") == []
    stoppable = True
    session = await unstarted.open(**arguments(manifest))
    assert session.repository_id == "repo-a"
    assert read_record(unstarted, "stranded")["ended_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retained_reason", "expected"),
    [(None, "server_restart"), ("launch_failed", "launch_failed")],
)
async def test_a_late_stop_finishes_its_record_with_the_same_reason(
    manifest, unstarted, doubles, monkeypatch, retained_reason, expected
):
    """A record retired on the first startup attempt says why. One whose unit
    could only be stopped later has to say the same thing, and one retained by
    a failed launch keeps the reason it already had.
    """
    write_record(unstarted, "deferred", termination_reason=retained_reason)
    reachable = False
    monkeypatch.setattr(launch, "stop_unit", lambda unit: None if reachable else unstoppable())
    await unstarted.start()
    assert read_record(unstarted, "deferred")["ended_at"] is None
    reachable = True
    await unstarted.open(**arguments(manifest))
    recorded = read_record(unstarted, "deferred")
    assert recorded["ended_at"]
    assert recorded["termination_reason"] == expected


@pytest.mark.asyncio
async def test_a_failed_mirrored_launch_keeps_its_record_when_the_unit_may_survive(
    manifest, manager, doubles, monkeypatch
):
    """A mirrored launch can create its unit and then fail before readiness.
    Finishing that record would hide the unit from startup reconciliation.
    """

    def fail(command, unit, **kwargs):
        raise TerminalUnavailable("systemd-run did not confirm the required terminal profile.")

    monkeypatch.setattr(launch, "launch", fail)
    monkeypatch.setattr(launch, "stop_unit", unstoppable)
    with pytest.raises(TerminalUnavailable, match="did not confirm"):
        await manager.open(**arguments(manifest))
    receipt = json.loads(next(manager.directory.glob("*.json")).read_text())
    assert receipt["ended_at"] is None
    assert receipt["termination_reason"] == "launch_failed"


@pytest.mark.asyncio
async def test_startup_reconciles_records_together(unstarted, monkeypatch):
    """Shutdown leaves every live remote session unfinished on purpose, so a
    machine that went away costs a remote stop timeout per record. A barrier
    proves the overlap without timing anything.
    """
    meeting = threading.Barrier(2)
    met = []

    def stop(host, unit, account=""):
        meeting.wait(timeout=5)
        met.append(unit)

    for name in ("first", "second"):
        write_record(
            unstarted,
            name,
            repository_id=f"repo-{name}",
            path=f"/checkout/{name}",
            execution_host="worker.invalid",
        )
    monkeypatch.setattr(remote, "stop_remote_unit", stop)
    try:
        await unstarted.start()
    finally:
        meeting.abort()
    assert sorted(met) == sorted(f"{unstarted._unit_prefix}-{name}" for name in ("first", "second"))
    for name in ("first", "second"):
        record = read_record(unstarted, name)
        assert record["termination_reason"] == "server_restart"
        assert record["ended_at"]


@pytest.mark.asyncio
async def test_startup_gives_up_on_a_record_that_outlasts_its_budget(
    manifest, unstarted, doubles, monkeypatch
):
    """The stops are blocking calls in a shared pool, so the budget is what
    bounds the boot. A record still running when it expires keeps blocking its
    repository, and keeps what it says, so a later open retries the very stop
    it names rather than blocking forever.
    """
    release = threading.Event()
    stops = []

    def stop(*args):
        stops.append(args)
        if len(stops) == 1:
            assert release.wait(10)

    monkeypatch.setattr(remote, "stop_remote_unit", stop)
    monkeypatch.setattr("rcp.terminals.manager.TERMINAL_STARTUP_RECONCILE_TIMEOUT_SECONDS", 0.25)
    write_record(unstarted, "wedged", execution_host="worker.invalid")
    try:
        await unstarted.start()
    finally:
        release.set()
    assert read_record(unstarted, "wedged")["ended_at"] is None
    # The retry names the same unit on the same machine, and releases the tree.
    await unstarted.open(**arguments(manifest))
    assert len(stops) == 2
    assert stops[1][:2] == ("worker.invalid", f"{unstarted._unit_prefix}-wedged")
    record = read_record(unstarted, "wedged")
    assert record["ended_at"]
    assert record["termination_reason"] == "server_restart"


UNREADABLE_RECORDS = {
    "unparsable": ("{ not json", False),
    "unknown_field": ({"a_field_from_another_version": True}, True),
    "wrong_typed_project": ({"project_id": []}, False),
    "missing_host": ({"execution_host": ...}, True),
    "wrong_typed_host": ({"execution_host": []}, True),
    "names_another_file": ({"session_id": "../rcp-server"}, True),
    "unknown_containment": ({"containment": "mirrored-with-something-new"}, True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", UNREADABLE_RECORDS, ids=UNREADABLE_RECORDS)
async def test_startup_leaves_a_record_it_cannot_read_and_blocks_what_it_names(
    manifest, unstarted, doubles, case
):
    """Being unable to read a record is not evidence that its shell is gone.

    Startup runs before every other owner, so no malformed record may refuse
    the boot. The file is left exactly as found, nothing else is written, and
    whatever repository it still names stays blocked: a record missing a field
    or holding one of the wrong kind, one named for another session or for a
    file outside the directory, and one whose containment this version does
    not know can none of them confirm what they left running. A missing or
    mistyped execution host in particular must not read as local, or a remote
    unit is cleaned up against a local one that was never there. A record
    naming no readable repository has nothing to block.
    """
    content, blocked = UNREADABLE_RECORDS[case]
    server_metadata = unstarted.data_dir / "rcp-server.json"
    server_metadata.write_text('{"instance": "original"}')
    if isinstance(content, str):
        path = unstarted.directory / "record.json"
        path.write_text(content)
    else:
        path = write_record(unstarted, "record", **content)
    written = path.read_bytes()
    await unstarted.start()
    assert path.read_bytes() == written
    assert sorted(item.name for item in unstarted.directory.iterdir()) == ["empty", "record.json"]
    assert server_metadata.read_text() == '{"instance": "original"}'
    assert doubles.stopped == []
    if blocked:
        with pytest.raises(TerminalUnavailable, match="until that one is gone"):
            await unstarted.open(**arguments(manifest))
    else:
        await unstarted.open(**arguments(manifest))


@pytest.mark.asyncio
async def test_every_unresolved_record_has_to_be_accounted_for(
    manifest, unstarted, doubles, monkeypatch
):
    """One repository can be spoken for by more than one retained record: a
    unit that outlived a restart, and a record this version cannot read. If
    only one were kept, confirming it gone would unblock the repository while
    the other may still be running.
    """
    write_record(unstarted, "stranded")
    write_record(unstarted, "newer", a_field_from_another_version=True)
    stoppable = False
    monkeypatch.setattr(launch, "stop_unit", lambda unit: None if stoppable else unstoppable())
    await unstarted.start()
    stoppable = True
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await unstarted.open(**arguments(manifest))
    # The stoppable one is confirmed gone and finished; the unreadable one
    # still speaks for the repository.
    assert read_record(unstarted, "stranded")["ended_at"]
    assert read_record(unstarted, "newer")["ended_at"] is None


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_reconciled_at_all_blocks_its_repository(
    manifest, unstarted, doubles
):
    """An `execution_host` can be a string and still be one `ssh` refuses, which
    raises where no branch expects it. Whatever the reason, the record names a
    unit this startup could not account for.
    """
    write_record(unstarted, "unreachable", execution_host="bad\nhost")
    await unstarted.start()
    assert read_record(unstarted, "unreachable")["ended_at"] is None
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await unstarted.open(**arguments(manifest))


# --- Remote sessions ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfirmed_remote_unit_blocks_a_second_shell(manager, remote_machine):
    """`run_session` writes the completion marker, then raises out of its own
    `stop_unit`, and reports that only through an exit status a dropped link
    produces too. Retiring the record on that evidence would hide a live unit.
    """
    manifest = remote_machine.manifest
    remote_machine.failure = TerminalUnavailable(
        "systemctl is unavailable on the execution machine."
    )
    session = await manager.open(**arguments(manifest))
    manager.sessions[session.session_id].process.returncode = 255
    await manager.sweep()
    assert session.ended_at is None
    assert read_record(manager, session.session_id)["ended_at"] is None
    assert manager.list("project") == []
    with pytest.raises(TerminalUnavailable, match="may still be running"):
        await manager.open(**arguments(manifest))


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_remote_255_confirms_the_unit_before_retiring(
    manager, remote_machine, doubles, monkeypatch, completed
):
    manifest = remote_machine.manifest
    session = await manager.open(**arguments(manifest))
    runtime = manager.sessions[session.session_id]
    queue = manager.attach("project", session.session_id)
    if completed:
        # Completion can sit behind multiple output chunks when SSH exits.
        monkeypatch.setattr("rcp.terminals.runtime.TERMINAL_IO_CHUNK_BYTES", 5)
        os.write(doubles.slaves[0], b"last output" + EXIT_PREFIX + b"255" + EXIT_SUFFIX)
    runtime.process.returncode = 255
    await manager.sweep()
    assert session.termination_reason == ("shell_exited" if completed else "link_dropped")
    # The supervisor writes its completion marker before the cleanup that can
    # fail, so a finished shell is not evidence the unit went with it.
    assert remote_machine.stops
    assert session.ended_at
    assert manager.list("project") == []
    visible = bytearray()
    while (chunk := await queue.get()) is not None:
        visible.extend(chunk)
    assert bytes(visible) == (b"last output" if completed else b"")
    assert (
        read_record(manager, session.session_id)["termination_reason"] == session.termination_reason
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_hanging_up_a_live_remote_shell_finishes_only_on_a_confirmed_stop(
    manager, remote_machine, confirmed
):
    """Hanging up the SSH PTY asks the remote supervisor to stop its unit, but
    nothing acknowledges that. An unconfirmed stop must leave the intent
    unfinished so the next startup reconciles the unit instead of skipping it.
    """
    manifest = remote_machine.manifest
    if not confirmed:
        remote_machine.failure = TerminalUnavailable("The execution machine is unreachable.")
    session = await manager.open(**arguments(manifest))
    await manager.end("project", session.session_id)
    if confirmed:
        # Cleanup carries the recorded account so it cannot run as another one.
        assert remote_machine.stops == [
            (session.execution_host, session.unit, session.declared_account)
        ]
    assert manager.list("project") == []
    receipt = read_record(manager, session.session_id)
    assert bool(receipt["ended_at"]) is confirmed
    assert receipt["termination_reason"] == "ended"
