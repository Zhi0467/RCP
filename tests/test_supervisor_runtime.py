from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor.errors import ApplicationCommandError, SupervisorError
from rcp_supervisor.runtime import Paths, SystemRuntime


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    runtime = object.__new__(SystemRuntime)
    runtime.paths = Paths(service_home=tmp_path)
    runtime.uid, runtime.gid = os.getuid(), os.getgid()
    runtime.notify = lambda message: None
    runtime.startup = False
    runtime.restore_request = None
    runtime._deployment_lock_fd = None
    if sys.platform != "linux":
        # Production requires Linux. This local framing fixture explicitly
        # bypasses prctl only on hosts where its kernel proof cannot run.
        runtime.owned_argv = lambda argv: argv
    logs = tmp_path / "logs"
    logs.mkdir()
    counter = iter(range(20))

    @contextmanager
    def log(label):
        path = logs / f"{next(counter)}-{label}.log"
        with path.open("w+b") as output:
            yield output, str(path)

    runtime._log = log
    actual_popen = subprocess.Popen

    def popen(*args, **kwargs):
        # The Linux VM qualification proves the UID transition. These real local
        # subprocesses exercise framing, deadlines, output bounds and diagnostics.
        assert kwargs.pop("user") == os.getuid()
        assert kwargs.pop("group") == os.getgid()
        assert kwargs.pop("extra_groups") == []
        return actual_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return runtime


def test_application_stderr_does_not_corrupt_json_and_diagnostics_survive(runtime):
    result = runtime.service_json(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys,json; print('diagnostic',file=sys.stderr); print(json.dumps(json.load(sys.stdin)))",
        ],
        {"version": 1, "payload": "x" * 100_000},
    )
    assert result["payload"] == "x" * 100_000
    assert any(
        "diagnostic" in path.read_text() for path in (runtime.paths.service_home / "logs").iterdir()
    )


def test_subprocess_not_reading_stdin_cannot_defeat_deadline(runtime):
    start = time.monotonic()
    with pytest.raises(SupervisorError, match="time limit"):
        runtime.service_json(
            [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            {"payload": "x" * 1_000_000},
            timeout=0.2,
        )
    assert time.monotonic() - start < 5


def test_stderr_output_is_bounded_too(runtime, monkeypatch):
    monkeypatch.setattr("rcp_supervisor.runtime.MAX_APP_OUTPUT_BYTES", 8192)
    with pytest.raises(SupervisorError, match="output|Output"):
        runtime.service_json(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys,time; sys.stderr.write('x'*10000); sys.stderr.flush(); time.sleep(60)",
            ],
            timeout=5,
        )


def test_incomplete_backup_warns_instead_of_refusing(runtime, monkeypatch):
    def output(status, count, problems=""):
        return json.dumps(
            {
                "step": {
                    "fields": [
                        {"name": "backup_status", "value": status},
                        {"name": "uncaptured_projects", "value": count},
                        {"name": "problems", "value": problems},
                    ]
                }
            }
        ).encode()

    legacy = {"release_directory": "/tmp/release", "commit": "a" * 40, "version_string": "0.3.4"}
    monkeypatch.setattr(runtime, "_service_output", lambda *args, **kwargs: output("protected", 0))
    assert runtime.protected_backup(legacy) is None
    for status, count in [("partial", 1), ("complete", 0), ("protected", 1)]:
        monkeypatch.setattr(
            runtime,
            "_service_output",
            lambda *args, _status=status, _count=count, **kwargs: output(_status, _count),
        )
        assert runtime.protected_backup(legacy)

    def failed(*_args, **_kwargs):
        raise ApplicationCommandError(
            "inspect private-error.log",
            output(
                "partial",
                1,
                "project-id: local_state_missing: .research/branches token=secret-value",
            ),
        )

    monkeypatch.setattr(runtime, "_service_output", failed)
    warning = runtime.protected_backup(legacy)
    assert "local_state_missing: .research/branches" in warning
    assert "private-error.log" in warning
    assert "secret-value" not in warning

    def timed_out(*_args, **_kwargs):
        raise SupervisorError("Application subprocess exceeded its time limit.")

    monkeypatch.setattr(runtime, "_service_output", timed_out)
    assert runtime.protected_backup(legacy)


def test_probe_arms_parent_ownership_in_supervisor_code_after_service_uid_drop(
    runtime, monkeypatch
):
    from types import SimpleNamespace

    release = {"release_directory": "/isolated/selected"}
    operation = {"operation_id": "operation", "nonce": "boundary"}
    launched = []
    stopped = []
    runtime.owned_argv = SystemRuntime.owned_argv.__get__(runtime)
    runtime._deployment_lock_fd = 42
    monkeypatch.setattr(runtime, "require_capability", lambda _release: None)
    monkeypatch.setattr(runtime, "prepare_control_directory", lambda: None)
    monkeypatch.setattr(runtime, "environment", lambda _release: {})
    monkeypatch.setattr(runtime, "_stop_child", lambda process: stopped.append(process.pid))

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return SimpleNamespace(pid=123, poll=lambda: 1)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(SupervisorError, match="exited before verification"):
        runtime.probe(release, operation, None)
    ((argv, kwargs),) = launched
    assert argv[:5] == [
        sys.executable,
        "-I",
        str(Path(sys.modules[SystemRuntime.__module__].__file__).with_name("child_exec.py")),
        str(os.getpid()),
        "/isolated/selected/.venv/bin/python",
    ]
    assert argv[5:9] == ["-I", "-m", "rcp", "serve"]
    assert kwargs["pass_fds"] == (42,)
    assert kwargs["user"] == runtime.uid and kwargs["group"] == runtime.gid
    assert kwargs["extra_groups"] == [] and kwargs["start_new_session"] is True
    assert stopped == [123]


@pytest.fixture
def control_runtime(runtime, tmp_path, monkeypatch):
    parent = tmp_path / "runtime-parent"
    parent.mkdir()
    runtime.paths = Paths(control_socket=parent / "rcp/control.sock", service_home=tmp_path)
    ancestors = set(runtime.paths.control_socket.parent.parents)
    lstat = Path.lstat
    fstat = os.fstat
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)

    def protected_ancestor(path):
        info = lstat(path)
        if path in ancestors:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        return info

    def protected_parent_fd(descriptor):
        info = fstat(descriptor)
        if (info.st_dev, info.st_ino) == parent_identity:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        return info

    # The local test account cannot own /run. Only ancestry ownership is modeled;
    # mkdir/open/chown/chmod/fsync and runtime entry validation operate on real files.
    monkeypatch.setattr(Path, "lstat", protected_ancestor)
    monkeypatch.setattr(os, "fstat", protected_parent_fd)
    return runtime


def test_probe_recreates_runtime_directory_removed_by_systemd(control_runtime, monkeypatch):
    runtime = control_runtime
    directory = runtime.paths.control_socket.parent
    runtime.prepare_control_directory()
    directory.rmdir()  # RuntimeDirectoryPreserve=no removes this on systemctl stop.
    monkeypatch.setattr(runtime, "require_capability", lambda _release: None)
    monkeypatch.setattr(runtime, "environment", lambda _release: {})

    def launch(argv, **kwargs):
        assert directory.is_dir()
        info = directory.stat()
        assert (info.st_uid, info.st_gid) == (runtime.uid, runtime.gid)
        assert stat.S_IMODE(info.st_mode) == 0o700
        return SimpleNamespace(pid=123, poll=lambda: 1)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(SupervisorError, match="exited before verification"):
        runtime.probe(
            {"release_directory": "/prepared"},
            {"operation_id": "operation", "nonce": "boundary"},
            None,
        )


def test_control_directory_preparation_preserves_existing_entries(control_runtime):
    runtime = control_runtime
    runtime.prepare_control_directory()
    directory = runtime.paths.control_socket.parent
    entry = directory / "retained-entry"
    entry.write_bytes(b"do not alter")
    entry.chmod(0o400)
    before = entry.stat()
    runtime.prepare_control_directory()
    assert entry.read_bytes() == b"do not alter"
    after = entry.stat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
    )


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "regular_file", "owner"])
def test_control_directory_preparation_refuses_unsafe_existing_path(control_runtime, unsafe):
    directory = control_runtime.paths.control_socket.parent
    if unsafe == "symlink":
        directory.symlink_to(directory.parent, target_is_directory=True)
    elif unsafe == "permissions":
        directory.mkdir(mode=0o755)
    elif unsafe == "owner":
        directory.mkdir(mode=0o700)
        control_runtime.uid += 1
    else:
        directory.write_bytes(b"retain")
    with pytest.raises((SupervisorError, OSError)):
        control_runtime.prepare_control_directory()


@pytest.mark.parametrize("held", [False, True])
def test_service_worker_receives_parent_ownership_and_only_held_lock(runtime, monkeypatch, held):
    from types import SimpleNamespace

    wrapped = []

    def owned_argv(argv):
        wrapped.append(argv)
        return ["owned-child", *argv]

    monkeypatch.setattr(runtime, "owned_argv", owned_argv)
    runtime._deployment_lock_fd = 42 if held else None
    launched = []

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return SimpleNamespace(pid=123, poll=lambda: 0, returncode=0, stdin=None)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert runtime._service_output(["/installed/worker"], timeout=5) == b""
    assert wrapped == [["/installed/worker"]]
    ((argv, kwargs),) = launched
    assert argv == ["owned-child", "/installed/worker"]
    assert kwargs["pass_fds"] == ((42,) if held else ())


def test_deployment_and_existing_backup_share_one_kernel_lock(runtime, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from rcp.server_ops.backup import BackupRunRefused, backup_run_lock

    runtime.paths = replace(runtime.paths, data_dir=runtime.paths.service_home / "data")
    layout = SimpleNamespace(server_root=runtime.paths.service_home)
    monkeypatch.setattr("rcp_supervisor.runtime.MAINTENANCE_TIMEOUT_SECONDS", 0.01)
    with (
        backup_run_lock(layout),
        pytest.raises(SupervisorError, match="running protected backup"),
        runtime.deployment_lock(),
    ):
        pytest.fail("deployment raced the backup")
    with runtime.deployment_lock():
        assert runtime._deployment_lock_fd is not None
        with pytest.raises(BackupRunRefused), backup_run_lock(layout):
            pytest.fail("backup raced deployment")
    with backup_run_lock(layout):
        assert runtime._deployment_lock_fd is None


@pytest.mark.parametrize("matches", [False, True])
def test_selected_release_requires_receipt_and_current_pointer_agreement(monkeypatch, matches):
    from rcp_supervisor import driver
    from rcp_supervisor import runtime as runtime_module

    selected = {"release_directory": "/releases/100"}
    pointer = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFLNK, st_uid=0))
    paths = SimpleNamespace(selected="receipt", releases_root="/releases", current=pointer)
    runtime = object.__new__(SystemRuntime)
    runtime.paths = paths
    monkeypatch.setattr(runtime_module, "read_selected_receipt", lambda *args, **kwargs: selected)
    monkeypatch.setattr(
        runtime_module.os, "readlink", lambda _: "/releases/100" if matches else "/releases/101"
    )
    for read in (runtime.selected_release, lambda: driver.selected_pointer(paths)):
        if matches:
            assert read() == selected
        else:
            with pytest.raises(SupervisorError, match="current pointer.*disagree"):
                read()


def test_stopped_lock_checks_current_inode_without_changing_bytes(runtime, tmp_path):
    import fcntl
    from dataclasses import replace

    runtime.paths = replace(runtime.paths, data_dir=tmp_path)
    lock = tmp_path / "rcp.lock"
    lock.write_bytes(b"stopped pid bytes\n")
    lock.chmod(0o600)
    before = lock.read_bytes()
    with lock.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(SupervisorError, match="writer still owns"),
            runtime.stopped_data_lock(),
        ):
            pytest.fail("admitted locked data")
    with runtime.stopped_data_lock():
        assert lock.read_bytes() == before
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"restored lock bytes\n")
    replacement.replace(lock)
    with lock.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(SupervisorError, match="writer still owns"),
            runtime.stopped_data_lock(),
        ):
            pytest.fail("checked stale inode")
    assert lock.read_bytes() == b"restored lock bytes\n"


def test_candidate_requires_inventory_command(runtime):
    runtime.application = lambda *args: {
        "version": 1,
        "maintenance_protocol": 10,
        "commands": ["prepare", "validate"],
    }
    runtime.require_capability({})  # The source release needs no new command.
    with pytest.raises(
        SupervisorError, match="application_maintenance_commands_missing: inventory"
    ):
        runtime.require_capability({}, extra_commands=("inventory",))


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "peer",
        "request_id",
        "instance_id",
        "protocol_version",
        "ok",
        "result",
        "code",
        "message",
        "control",
    ],
)
def test_only_authenticated_refusal_causes_reach_cli_and_journal(
    runtime, tmp_path, monkeypatch, capsys, invalid
):
    import socket
    import struct

    from rcp_supervisor import cli, driver

    from tests.test_supervisor_operations import _case

    metadata = {"instance_id": "instance", "pid": 123, "data_dir_id": "data"}
    monkeypatch.setattr(runtime, "metadata", lambda: metadata)
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def getsockopt(self, *_args):
            return struct.pack("3i", 999 if invalid == "peer" else 123, runtime.uid, runtime.gid)

        def sendall(self, data):
            request = json.loads(data[4:])
            response = {
                "protocol_version": 10,
                "request_id": request["request_id"],
                "instance_id": "instance",
                "ok": False,
                "error": {
                    "code": "operation_refused",
                    "message": "maintenance_busy token=private-value",
                },
            }
            if invalid in {"request_id", "instance_id", "protocol_version", "ok"}:
                response[invalid] = "wrong"
            elif invalid == "result":
                response["result"] = {}
            elif invalid == "code":
                response["error"]["code"] = "x" * 65
            elif invalid == "message":
                response["error"]["message"] = "maintenance_busy " + "x" * 240
            elif invalid == "control":
                response["error"]["message"] += "\x1b[31m"
            payload = json.dumps(response).encode()
            self.data = struct.pack("!I", len(payload)) + payload

        def recv(self, count):
            data, self.data = self.data[:count], self.data[count:]
            return data

    monkeypatch.setattr(socket, "socket", lambda *_args: Connection())
    coordinator, adapter, previous, target = _case(tmp_path / "deployment")
    adapter.enter_maintenance = runtime.enter_maintenance
    monkeypatch.setattr(driver, "update", lambda *_args: coordinator.deploy(previous, target))
    monkeypatch.setattr(driver, "safe_state_fields", lambda: [])
    assert cli.main(["server", "update", "--machine-readable"]) == 1
    step = json.loads(capsys.readouterr().out.splitlines()[-1])["step"]
    assert step["state"] == "failed"
    journal = json.loads(next((tmp_path / "deployment/operations").glob("*.json")).read_text())
    for diagnostic in (step["message"], journal["error"]):
        assert "private-value" not in diagnostic
        assert "\x1b" not in diagnostic
        assert ("maintenance_busy" in diagnostic) is (invalid is None)
        assert ("operation_refused" in diagnostic) is (invalid is None)
    assert journal["phase"] == "aborted"


@pytest.mark.parametrize("exit_early", [False, True])
def test_probe_failure_retains_last_readiness_cause_and_private_log(
    runtime, monkeypatch, exit_early
):
    monkeypatch.setattr(runtime, "require_capability", lambda _release: None)
    monkeypatch.setattr(runtime, "prepare_control_directory", lambda: None)
    monkeypatch.setattr(runtime, "environment", lambda _release: {})
    monkeypatch.setattr(runtime, "_stop_child", lambda _process: None)
    polls = iter([None, 1 if exit_early else None])
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: SimpleNamespace(pid=123, poll=lambda: next(polls)),
    )
    clock = iter([0, 0, 1000])
    monkeypatch.setattr("rcp_supervisor.runtime.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("rcp_supervisor.runtime.time.sleep", lambda _seconds: None)

    def unavailable():
        raise SupervisorError("readiness_unavailable token=private-value")

    monkeypatch.setattr(runtime, "metadata", unavailable)
    with pytest.raises(SupervisorError) as failure:
        runtime.probe(
            {"release_directory": "/isolated"},
            {"operation_id": "operation", "nonce": "boundary"},
            None,
        )
    diagnostic = str(failure.value)
    assert "readiness_unavailable" in diagnostic
    assert "private-value" not in diagnostic
    assert str(next((runtime.paths.service_home / "logs").glob("*-probe.log"))) in diagnostic


def test_filesystem_copy_keeps_supervisor_credentials_without_total_deadline(runtime, monkeypatch):
    calls = []

    def command(argv, request, **kwargs):
        calls.append((argv[-1], kwargs))
        return {"status": "ready"}

    monkeypatch.setattr(runtime, "service_json", command)
    for action in ("checkpoint", "snapshot-roots", "restore", "preserve-candidate"):
        runtime.filesystem(action, {})
    assert all(options["privileged"] and options["timeout"] is None for _, options in calls)


def test_update_preparation_uses_candidate_and_keeps_stopped_checkpoint(runtime, tmp_path):
    runtime.paths = Paths(data_dir=tmp_path / "data", checkpoints_root=tmp_path)
    prepared = {
        "roots": [{"live": str(runtime.paths.data_dir), "payload": str(tmp_path / "prepared")}],
        "proof_path": str(tmp_path / "proof"),
        "proof_sha256": "c" * 64,
    }
    target = {"commit": "candidate"}
    calls = []

    def application(release, action, request):
        assert release == target
        calls.append(action)
        return prepared

    runtime.application = application
    runtime.filesystem = lambda action, request: calls.append(action)
    checkpoint = {"directory": str(tmp_path / "checkpoint"), "sha256": "a" * 64}
    result = runtime.prepare(
        {
            "operation_id": "operation",
            "target": target,
            "previous": {},
            "kind": "update",
            "checkpoint": checkpoint,
        },
        {"receipt_path": "receipt", "receipt_sha256": "b" * 64},
    )
    assert result[0] is checkpoint
    assert result[1] is None  # Old startup probes never consume a new-release proof.
    assert calls == ["prepare", "check-roots", "validate"]
