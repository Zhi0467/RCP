from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
from pathlib import Path

import pytest
from rcp_supervisor import checkpoint
from rcp_supervisor.errors import SupervisorError


class PowerLoss(BaseException):
    """Interrupt without turning a simulated process death into ordinary recovery."""


def _directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    return path


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    path.chmod(mode)


def _case(tmp_path: Path):
    live_data = _directory(tmp_path / "server" / "data")
    live_research = _directory(tmp_path / "repository" / ".research")
    database = live_data / "rcp.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")
        connection.execute("INSERT INTO observations VALUES ('before migration')")
    database.chmod(0o600)
    _write(live_data / "run-stage" / "retained" / "runner", b"retained stage\n", 0o700)
    (live_data / "run-stage" / "retained" / "current").symlink_to("runner")
    _write(live_research / "patches" / "000001.json", b'{"revision":1}\n')
    _directory(live_research / "facts")
    payload_data = tmp_path / "prepared" / "data"
    payload_research = tmp_path / "prepared" / "research"
    _directory(payload_data.parent)
    shutil.copytree(live_data, payload_data, symlinks=True)
    shutil.copytree(live_research, payload_research, symlinks=True)
    _directory(tmp_path / "checkpoints")
    roots = (
        checkpoint.SnapshotRoot(live=live_data, payload=payload_data),
        checkpoint.SnapshotRoot(live=live_research, payload=payload_research),
    )
    saved = checkpoint.create_checkpoint(
        tmp_path / "checkpoints" / "operation", roots, boundary_sha256="b" * 64
    )
    return saved, roots


def _mutate_live(roots) -> None:
    data, research = (root.live for root in roots)
    with sqlite3.connect(data / "rcp.sqlite3") as connection:
        connection.execute("ALTER TABLE observations ADD COLUMN migrated INTEGER DEFAULT 1")
        connection.execute("UPDATE observations SET value = 'candidate changed data'")
    _write(data / "candidate-only" / "diagnostic", b"candidate database side effect\n")
    _write(data / "run-stage" / "retained" / "runner", b"changed stage\n", 0o600)
    _write(research / "patches" / "000001.json", b'{"revision":2}\n')
    _write(research / "candidate-only", b"candidate graph side effect\n")


def _assert_restored(roots) -> None:
    for root in roots:
        live_paths = {path.relative_to(root.live) for path in root.live.rglob("*")}
        expected_paths = {path.relative_to(root.payload) for path in root.payload.rglob("*")}
        assert live_paths == expected_paths
        assert stat.S_IMODE(root.live.stat().st_mode) == 0o700
        for relative in expected_paths:
            actual = root.live / relative
            expected = root.payload / relative
            assert actual.stat().st_uid == os.geteuid()
            if expected.is_symlink():
                assert actual.is_symlink() and os.readlink(actual) == os.readlink(expected)
            elif expected.is_dir():
                assert actual.is_dir() and not actual.is_symlink()
                assert stat.S_IMODE(actual.stat().st_mode) == 0o700
            else:
                assert actual.read_bytes() == expected.read_bytes()
                assert stat.S_IMODE(actual.stat().st_mode) == stat.S_IMODE(expected.stat().st_mode)
    with sqlite3.connect(f"file:{roots[0].live / 'rcp.sqlite3'}?mode=ro", uri=True) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(observations)")] == [
            "value"
        ]
        assert connection.execute("SELECT value FROM observations").fetchall() == [
            ("before migration",)
        ]


def test_checkpoint_restores_forward_migration_and_research_without_overlay(tmp_path: Path) -> None:
    saved, roots = _case(tmp_path)
    unrelated = tmp_path / "unrelated" / "keep"
    _write(unrelated, b"outside selected roots\n")
    assert saved.boundary_sha256 == "b" * 64
    assert checkpoint.read_checkpoint(saved.directory, expected_sha256=saved.sha256) == saved
    _mutate_live(roots)

    checkpoint.restore_checkpoint(saved)

    _assert_restored(roots)
    assert unrelated.read_bytes() == b"outside selected roots\n"
    assert any(
        path.read_bytes() == b"candidate database side effect\n"
        for path in roots[0].live.parent.rglob("diagnostic")
    )
    assert any(
        path.is_file() and path.read_bytes() == b"candidate graph side effect\n"
        for path in roots[1].live.parent.rglob("candidate-only")
    )
    checkpoint.restore_checkpoint(saved)
    _assert_restored(roots)


@pytest.mark.parametrize("moved_root", [0, 1])
@pytest.mark.parametrize("move", ["quarantine", "publication"])
def test_restore_reenters_after_each_live_root_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moved_root: int, move: str
) -> None:
    saved, roots = _case(tmp_path)
    _mutate_live(roots)
    replace = os.replace
    interrupted = False

    def die_after_rename(source, destination, *args, **kwargs):
        nonlocal interrupted
        result = replace(source, destination, *args, **kwargs)
        moved_path = source if move == "quarantine" else destination
        if Path(moved_path) == roots[moved_root].live and not interrupted:
            interrupted = True
            raise PowerLoss
        return result

    with monkeypatch.context() as fault:
        fault.setattr(checkpoint.os, "replace", die_after_rename)
        with pytest.raises(PowerLoss):
            checkpoint.restore_checkpoint(saved)
    assert interrupted

    reopened = checkpoint.read_checkpoint(saved.directory, expected_sha256=saved.sha256)
    checkpoint.restore_checkpoint(reopened)
    _assert_restored(roots)


def test_restore_reenters_after_staged_file_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved, roots = _case(tmp_path)
    _mutate_live(roots)
    fsync = os.fsync
    interrupted = False

    def die_after_staged_file(descriptor):
        nonlocal interrupted
        result = fsync(descriptor)
        info = os.fstat(descriptor)
        staged = [
            path
            for root in roots
            for path in root.live.parent.rglob("runner")
            if path != roots[0].live / "run-stage" / "retained" / "runner"
        ]
        if not interrupted and any(
            path.stat().st_ino == info.st_ino and path.read_bytes() == b"retained stage\n"
            for path in staged
        ):
            interrupted = True
            raise PowerLoss
        return result

    with monkeypatch.context() as fault:
        fault.setattr(checkpoint.os, "fsync", die_after_staged_file)
        with pytest.raises(PowerLoss):
            checkpoint.restore_checkpoint(saved)
    assert interrupted

    checkpoint.restore_checkpoint(
        checkpoint.read_checkpoint(saved.directory, expected_sha256=saved.sha256)
    )
    _assert_restored(roots)


def test_checkpoint_detects_changed_payload_and_wrong_manifest_hash(tmp_path: Path) -> None:
    saved, _ = _case(tmp_path)
    with pytest.raises(SupervisorError):
        checkpoint.read_checkpoint(saved.directory, expected_sha256="f" * 64)
    target = next(path for path in saved.directory.rglob("runner") if path.is_file())
    target.chmod(0o600)
    target.write_bytes(b"tampered checkpoint bytes\n")
    with pytest.raises(SupervisorError):
        checkpoint.read_checkpoint(saved.directory, expected_sha256=saved.sha256)
    with pytest.raises(SupervisorError):
        checkpoint.restore_checkpoint(saved)


@pytest.mark.parametrize(
    "invalid", ["unknown-field", "list-kind", "zero-mode", "link-size", "link-target"]
)
def test_checkpoint_refuses_invalid_manifest_even_with_matching_hash(
    tmp_path: Path, invalid: str
) -> None:
    saved, _ = _case(tmp_path)
    (manifest,) = saved.directory.glob("*.json")
    document = json.loads(manifest.read_bytes())
    if invalid == "unknown-field":
        document["unknown-contract-field"] = True
    elif invalid.startswith("link-"):
        entry = next(item for item in document["roots"][0]["entries"] if item["kind"] == "symlink")
        if invalid == "link-size":
            entry["size"] = 0
        else:
            entry["target"] = "bad\x00target"
    else:
        entry = next(item for item in document["roots"][0]["entries"] if item["kind"] == "file")
        entry["kind" if invalid == "list-kind" else "mode"] = [] if invalid == "list-kind" else 0
    data = json.dumps(document).encode()
    manifest.chmod(0o600)
    manifest.write_bytes(data)
    with pytest.raises(SupervisorError):
        checkpoint.read_checkpoint(
            saved.directory, expected_sha256=hashlib.sha256(data).hexdigest()
        )


@pytest.mark.parametrize("invalid", ["unknown-field", "malformed-json", "list-status"])
def test_restore_refuses_unknown_or_malformed_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    saved, roots = _case(tmp_path)
    _mutate_live(roots)
    before = set(saved.directory.rglob("*.json"))
    replace = os.replace

    def die_after_first_root(source, destination, *args, **kwargs):
        result = replace(source, destination, *args, **kwargs)
        if Path(source) == roots[0].live:
            raise PowerLoss
        return result

    with monkeypatch.context() as fault:
        fault.setattr(checkpoint.os, "replace", die_after_first_root)
        with pytest.raises(PowerLoss):
            checkpoint.restore_checkpoint(saved)
    (journal,) = set(saved.directory.rglob("*.json")) - before
    if invalid == "malformed-json":
        journal.write_bytes(b"{broken")
    else:
        document = json.loads(journal.read_bytes())
        if invalid == "unknown-field":
            document["unknown-contract-field"] = True
        else:
            document["status"] = []
        journal.write_text(json.dumps(document))
    with pytest.raises(SupervisorError):
        checkpoint.restore_checkpoint(saved)
    assert not roots[0].live.exists()
    assert (roots[1].live / "candidate-only").read_bytes() == b"candidate graph side effect\n"


def test_checkpoint_keeps_payload_links_by_text_and_never_follows_them(tmp_path: Path) -> None:
    live = _directory(tmp_path / "live")
    payload = _directory(tmp_path / "payload")
    outside = tmp_path / "outside"
    _write(outside, b"outside authority")
    _write(payload / "stage" / "log.txt", b"ran\n")
    links = {
        "stage/current": "log.txt",  # relative, inside the stage
        "stage/python": "/usr/bin/python3",  # absolute, an environment pointer
        "stage/gone": "missing-target",  # dangling
        "stage/escape": str(outside),  # points outside every root
        "stage/odd": "dir\\name",  # a backslash is an ordinary character on Linux
        "stage/pytest-current": "pytest-0",  # a link to a directory
    }
    (payload / "stage" / "pytest-0").mkdir(mode=0o700)
    for relative, target in links.items():
        (payload / relative).symlink_to(target)

    saved = checkpoint.create_checkpoint(
        tmp_path / "checkpoint",
        (checkpoint.SnapshotRoot(live, payload),),
        boundary_sha256="b" * 64,
    )
    (manifest,) = saved.directory.glob("*.json")
    entries = json.loads(manifest.read_bytes())["roots"][0]["entries"]
    assert {entry["path"]: entry["target"] for entry in entries if entry["kind"] == "symlink"} == (
        links
    )
    stored = saved.directory / "payload" / "0"
    assert {
        p.relative_to(stored).as_posix(): os.readlink(p)
        for p in stored.rglob("*")
        if p.is_symlink()
    } == links
    assert outside.read_bytes() == b"outside authority"

    _write(live / "stage" / "candidate.txt", b"candidate side effect\n")
    (live / "stage").mkdir(exist_ok=True)
    (live / "stage" / "current").symlink_to("candidate.txt")
    checkpoint.restore_checkpoint(saved)
    restored = {
        p.relative_to(live).as_posix(): os.readlink(p) for p in live.rglob("*") if p.is_symlink()
    }
    assert restored == links
    assert not (live / "stage" / "candidate.txt").exists()
    assert (live / "stage" / "log.txt").read_bytes() == b"ran\n"
    assert outside.read_bytes() == b"outside authority"


@pytest.mark.parametrize("unsafe", ["fifo", "socket", "hardlink"])
def test_checkpoint_refuses_unsafe_prepared_payload_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    live = _directory(tmp_path / "live")
    payload = _directory(tmp_path / "payload")
    target = payload / "unsafe"
    listener = None
    if unsafe == "fifo":
        os.mkfifo(target)
    elif unsafe == "socket":
        listener = socket.socket(socket.AF_UNIX)
        with monkeypatch.context() as directory:
            directory.chdir(payload)
            listener.bind(target.name)
    else:
        _write(payload / "original", b"hardlinked bytes")
        os.link(payload / "original", target)
    try:
        with pytest.raises(SupervisorError):
            checkpoint.create_checkpoint(
                tmp_path / "checkpoint",
                (checkpoint.SnapshotRoot(live, payload),),
                boundary_sha256="b" * 64,
            )
    finally:
        if listener is not None:
            listener.close()


@pytest.mark.parametrize(
    "unsafe",
    [
        "live-file",
        "payload-file",
        "missing-live",
        "missing-payload",
        "live-link",
        "payload-link",
        "parent-link",
        "writable-live",
        "writable-parent",
        "writable-payload",
    ],
)
def test_checkpoint_refuses_unsafe_roots(tmp_path: Path, unsafe: str) -> None:
    parent = _directory(tmp_path / "parent")
    live = _directory(parent / "live")
    payload = _directory(tmp_path / "payload")
    if unsafe in {"missing-live", "missing-payload"}:
        (live if unsafe == "missing-live" else payload).rmdir()
    elif unsafe in {"live-file", "payload-file"}:
        target = live if unsafe == "live-file" else payload
        target.rmdir()
        _write(target, b"not a directory")
    elif unsafe in {"live-link", "payload-link"}:
        target = live if unsafe == "live-link" else payload
        target.rename(tmp_path / "original")
        target.symlink_to(tmp_path / "original", target_is_directory=True)
    elif unsafe == "parent-link":
        link = tmp_path / "link"
        link.symlink_to(parent, target_is_directory=True)
        live = link / "live"
    else:
        target = {"writable-live": live, "writable-parent": parent, "writable-payload": payload}[
            unsafe
        ]
        target.chmod(0o777)
    with pytest.raises(SupervisorError):
        checkpoint.create_checkpoint(
            tmp_path / "checkpoint",
            (checkpoint.SnapshotRoot(live, payload),),
            boundary_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "overlap",
    [
        "live-nested",
        "payload-in-live",
        "checkpoint-in-live",
        "checkpoint-in-payload",
        "duplicate-live",
    ],
)
def test_checkpoint_refuses_overlapping_roots(tmp_path: Path, overlap: str) -> None:
    live = _directory(tmp_path / "live")
    payload = _directory(tmp_path / "payload")
    destination = tmp_path / "checkpoint"
    roots = (checkpoint.SnapshotRoot(live, payload),)
    if overlap == "live-nested":
        roots += (
            checkpoint.SnapshotRoot(
                _directory(live / "nested"), _directory(tmp_path / "second-payload")
            ),
        )
    elif overlap == "payload-in-live":
        roots = (checkpoint.SnapshotRoot(live, _directory(live / "payload")),)
    elif overlap == "checkpoint-in-live":
        destination = live / "checkpoint"
    elif overlap == "checkpoint-in-payload":
        destination = payload / "checkpoint"
    else:
        roots += (checkpoint.SnapshotRoot(live, _directory(tmp_path / "second-payload")),)
    with pytest.raises(SupervisorError):
        checkpoint.create_checkpoint(destination, roots, boundary_sha256="b" * 64)


def test_checkpoint_never_overwrites_an_existing_destination(tmp_path: Path) -> None:
    saved, roots = _case(tmp_path)
    before = {
        path.relative_to(saved.directory): path.read_bytes()
        for path in saved.directory.rglob("*")
        if path.is_file()
    }
    with pytest.raises(SupervisorError):
        checkpoint.create_checkpoint(saved.directory, roots, boundary_sha256="c" * 64)
    after = {
        path.relative_to(saved.directory): path.read_bytes()
        for path in saved.directory.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert checkpoint.read_checkpoint(saved.directory, expected_sha256=saved.sha256) == saved


def test_completed_restore_refuses_to_replace_subsequent_data(tmp_path: Path) -> None:
    saved, roots = _case(tmp_path)
    _mutate_live(roots)
    checkpoint.restore_checkpoint(saved)
    _assert_restored(roots)
    database = roots[0].live / "rcp.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO observations VALUES ('accepted after recovery')")
    _write(roots[1].live / "facts" / "new-evidence.json", b'{"new":true}\n')
    preserved_database = database.read_bytes()

    with pytest.raises(SupervisorError):
        checkpoint.restore_checkpoint(
            checkpoint.read_checkpoint(saved.directory, expected_sha256=saved.sha256)
        )

    assert database.read_bytes() == preserved_database
    assert (roots[1].live / "facts" / "new-evidence.json").read_bytes() == b'{"new":true}\n'


def test_checkpoint_refuses_unreadable_prepared_file(tmp_path: Path) -> None:
    live = _directory(tmp_path / "live")
    payload = _directory(tmp_path / "payload")
    unreadable = payload / "unreadable"
    _write(unreadable, b"cannot verify after restoration", 0)
    try:
        with pytest.raises(SupervisorError):
            checkpoint.create_checkpoint(
                tmp_path / "checkpoint",
                (checkpoint.SnapshotRoot(live, payload),),
                boundary_sha256="b" * 64,
            )
    finally:
        unreadable.chmod(0o600)


def test_offline_adoption_snapshot_retains_original_sqlite_before_new_code(tmp_path):
    import sqlite3

    from rcp_supervisor.checkpoint import create_offline_snapshot, restore_checkpoint

    live = tmp_path / "legacy"
    live.mkdir(mode=0o700)
    database = live / "rcp.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE old_records (value TEXT)")
        connection.execute("INSERT INTO old_records VALUES ('retained original')")
    original = database.read_bytes()
    checkpoint = create_offline_snapshot(
        tmp_path / "legacy-checkpoint", live, boundary_sha256="b" * 64
    )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE old_records ADD COLUMN new_value TEXT")
    restore_checkpoint(checkpoint)
    assert database.read_bytes() == original
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM old_records").fetchall() == [
            ("retained original",)
        ]

    # An opaque legacy root keeps the fail-closed rule: a link anywhere refuses.
    (live / "stray").symlink_to("rcp.sqlite3")
    with pytest.raises(SupervisorError):
        create_offline_snapshot(tmp_path / "legacy-checkpoint-2", live, boundary_sha256="b" * 64)
