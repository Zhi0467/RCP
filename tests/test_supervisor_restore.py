from __future__ import annotations

import io
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor import restore
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.events import EventEmitter

from rcp.server_ops.models import ServerStepEvent


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    state = tmp_path / "supervisor"
    state.mkdir()
    archive = tmp_path / "protected.age"
    archive.write_bytes(b"encrypted disposable fixture")
    archive.chmod(0o600)
    paths = SimpleNamespace(
        supervisor=state, data_dir=tmp_path / "data", checkpoints_root=tmp_path / "checkpoints"
    )
    paths.data_dir.mkdir()
    calls = []

    def application(target, action, request):
        calls.append((action, request))
        return {
            "version": 1,
            "status": "operator_action_needed",
            "diagnostic": "Review exact archived authority.",
            "progress": [],
            "actions": [{"kind": "external", "instruction": "Review every archived authority."}],
            "fields": [
                {"name": "old_authority_boundary", "value": "a" * 64},
                {"name": "member_roster_boundary", "value": "b" * 64},
            ],
        }

    runtime = SimpleNamespace(paths=paths, gid=os.getegid(), application=application)
    monkeypatch.setattr(restore, "_directory", lambda path, gid: path.mkdir(exist_ok=True))
    monkeypatch.setattr(
        restore, "write_root_json", lambda path, value: path.write_text(json.dumps(value))
    )
    monkeypatch.setattr(restore, "_read_file", lambda path, **kwargs: path.read_bytes())
    real_hash = restore._hash
    monkeypatch.setattr(restore, "_hash", lambda path, **kwargs: real_hash(path))
    decrypts = []

    def decrypt(archive, identity, output, gid, expected):
        decrypts.append(output)
        output.write_bytes(b"decrypted disposable fixture")
        output.chmod(0o640)
        return real_hash(output), "c" * 64

    monkeypatch.setattr(restore, "_decrypt", decrypt)
    request = {
        "archive_path": str(archive),
        "identity_file": str(tmp_path / "recovery.key"),
        "confirmed_data_dir": str(paths.data_dir),
        "confirmed_by": "qualification",
    }
    return runtime, request, calls, decrypts


def test_protected_restore_pauses_in_sealed_wizard_envelope_and_reuses_preparation(runtime):
    runtime, request, calls, decrypts = runtime
    target = {"commit": "d" * 40, "manifest_sha256": "e" * 64}
    first = {"operation_id": str(uuid.uuid4()), "target": target}
    previous = {"roots": [{"live": str(runtime.paths.data_dir), "payload": "/private/previous"}]}
    with pytest.raises(restore.RestoreOperatorAction) as error:
        restore.prepare_restore(runtime, request, first, previous)
    stream = io.StringIO()
    emitter = EventEmitter("server restore", machine_readable=True, stream=stream)
    emitter.emit("operator_action_needed", str(error.value), **error.value.details)
    event = ServerStepEvent.model_validate_json(stream.getvalue().splitlines()[-1])
    actions = [a for a in event.step.actions if a.kind == "command"]
    assert len(actions) == 2
    assert {a.argv[a.argv.index("--old-authority-disposition") + 1] for a in actions} == {
        "old-machine-destroyed",
        "old-machine-fenced-and-credentials-revoked",
    }
    for action in actions:
        assert action.argv[:4] == ("sudo", "rcp", "server", "restore")
        assert action.argv[action.argv.index("--confirm-old-authority") + 1] == "a" * 64
        assert action.argv[action.argv.index("--confirm-member-roster") + 1] == "b" * 64
    with pytest.raises(restore.RestoreOperatorAction):
        restore.prepare_restore(
            runtime, request, {**first, "operation_id": str(uuid.uuid4())}, previous
        )
    assert len(decrypts) == 1
    assert calls[0][1]["preparation_id"] == calls[1][1]["preparation_id"]
    assert calls[0][1]["detached_at"] == calls[1][1]["detached_at"]
    assert calls[0][1]["output_dir"] != calls[1][1]["output_dir"]


def test_archive_fifo_and_oversized_regular_file_fail_promptly(tmp_path, monkeypatch):
    fifo = tmp_path / "archive.fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(SupervisorError, match="metadata"):
        restore._hash(fifo)
    archive = tmp_path / "oversized.age"
    archive.write_bytes(b"12345")
    archive.chmod(0o600)
    monkeypatch.setattr(restore, "MAX_RESTORE_ARCHIVE_BYTES", 4)
    with pytest.raises(SupervisorError, match="byte bound"):
        restore._hash(archive)


def test_unchanged_application_progress_cannot_loop(runtime):
    runtime, request, _, _ = runtime
    runtime.application = lambda *args: {"status": "continue", "progress": []}
    with pytest.raises(SupervisorError, match="did not advance"):
        restore.prepare_restore(
            runtime,
            request,
            {"operation_id": str(uuid.uuid4()), "target": {"commit": "a" * 40}},
            {"roots": []},
        )


def test_unsealed_decryption_is_quarantined_then_rebuilt_after_interruption(runtime, monkeypatch):
    runtime, request, _, decrypts = runtime
    target = {"commit": "a" * 40}
    operation = {"operation_id": str(uuid.uuid4()), "target": target}
    original_write = restore.write_root_json
    monkeypatch.setattr(
        restore,
        "write_root_json",
        lambda *a: (_ for _ in ()).throw(OSError("interrupted before receipt")),
    )
    with pytest.raises(OSError, match="interrupted"):
        restore.prepare_restore(runtime, request, operation, {"roots": []})
    monkeypatch.setattr(restore, "write_root_json", original_write)
    real_lstat = Path.lstat

    def root_owned_orphan(path):
        info = real_lstat(path)
        if path.name == "archive.tar":
            return SimpleNamespace(
                st_mode=info.st_mode, st_uid=0, st_gid=info.st_gid, st_nlink=info.st_nlink
            )
        return info

    monkeypatch.setattr(Path, "lstat", root_owned_orphan)
    with pytest.raises(restore.RestoreOperatorAction):
        restore.prepare_restore(runtime, request, operation, {"roots": []})
    assert len(decrypts) == 2
    assert len(list(runtime.paths.supervisor.rglob("unsealed-*-archive.tar"))) == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_decryption_kills_child_when_output_exceeds_bound(tmp_path, monkeypatch, stream):
    import hashlib
    import stat
    import subprocess
    import sys

    archive = tmp_path / "input.age"
    archive.write_bytes(b"encrypted test")
    archive.chmod(0o600)
    identity = tmp_path / "identity.key"
    identity.write_bytes(b"synthetic private identity")
    identity.chmod(0o600)
    real_lstat = Path.lstat

    def protected_identity_paths(path):
        info = real_lstat(path)
        if path == identity or path in identity.parents:
            return SimpleNamespace(
                st_mode=(stat.S_IFDIR | 0o700) if path != identity else (stat.S_IFREG | 0o600),
                st_uid=0,
                st_nlink=1,
            )
        return info

    monkeypatch.setattr(Path, "lstat", protected_identity_paths)
    monkeypatch.setattr(restore, "root_executable", lambda name: name)
    monkeypatch.setattr(
        restore,
        "_tool_output",
        lambda executable, arguments: b"1.2.0\n" if executable == "age" else b"age1" + b"q" * 58,
    )
    real_popen = subprocess.Popen
    processes = []

    def launch(argv, **kwargs):
        process = real_popen(
            [
                sys.executable,
                "-c",
                f"import sys; sys.{stream}.buffer.write(b'x' * 4096); sys.{stream}.flush()",
            ],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(restore.subprocess, "Popen", launch)
    monkeypatch.setattr(restore, "MAX_RESTORE_ARCHIVE_BYTES", 128)
    monkeypatch.setattr(restore, "MAX_APP_OUTPUT_BYTES", 128)
    with pytest.raises(SupervisorError, match="output bound"):
        restore._decrypt(
            archive,
            identity,
            tmp_path / "archive.tar",
            os.getegid(),
            hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
    assert processes[0].poll() is not None
    assert (tmp_path / "archive.tar").stat().st_size <= 128
    assert (tmp_path / "archive.stderr").stat().st_size <= 128
