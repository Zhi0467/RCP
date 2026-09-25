from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor import checkpoint
from rcp_supervisor.errors import SupervisorError


class PowerLoss(BaseException):
    pass


def _case(tmp_path):
    roots = (tmp_path / "data", tmp_path / "project" / ".research")
    for root in roots:
        root.mkdir(parents=True)
        (root / "empty").mkdir()
        (root / "original").write_bytes(b"before")
        (root / "original").chmod(0o666)
        (root / "dangling").symlink_to("absent")
    with sqlite3.connect(roots[0] / "db") as db:
        db.execute("CREATE TABLE observations (value TEXT)")
        db.execute("INSERT INTO observations VALUES ('before')")
    saved = checkpoint.create_stopped_snapshot(
        tmp_path / "checkpoint", roots, boundary_sha256="b" * 64
    )
    for root in roots:
        (root / "original").write_bytes(b"after")
        (root / "candidate").write_bytes(b"new")
    return saved, roots


def _assert_restored(roots):
    for root in roots:
        assert (root / "original").read_bytes() == b"before"
        assert stat.S_IMODE((root / "original").stat().st_mode) == 0o666
        assert (root / "empty").is_dir()
        assert os.readlink(root / "dangling") == "absent"
        assert not (root / "candidate").exists()
        assert root.stat().st_uid == os.geteuid()
        assert root.stat().st_gid == os.getegid()
    with sqlite3.connect(roots[0] / "db") as db:
        assert db.execute("SELECT value FROM observations").fetchall() == [("before",)]


def test_whole_root_rollback_consumes_snapshot_and_never_replays(tmp_path):
    saved, roots = _case(tmp_path)
    assert checkpoint.read_checkpoint(saved.directory, expected_sha256=saved.sha256) == saved
    checkpoint.restore_checkpoint(saved)
    _assert_restored(roots)
    catalog = json.loads((saved.directory / "checkpoint.json").read_text())
    for root in catalog["roots"]:
        assert not Path(root["payload"]).exists()
        assert (Path(root["quarantine"]) / "candidate").read_bytes() == b"new"
    (roots[0] / "accepted-later").write_text("keep")
    checkpoint.restore_checkpoint(saved)
    assert (roots[0] / "accepted-later").read_text() == "keep"


@pytest.mark.parametrize("root_index", [0, 1])
@pytest.mark.parametrize("move", ["quarantine", "publication"])
def test_rollback_resumes_each_rename_after_crash(tmp_path, monkeypatch, root_index, move):
    saved, roots = _case(tmp_path)
    replace = os.replace

    def crash(source, target):
        replace(source, target)
        if Path(source if move == "quarantine" else target) == roots[root_index]:
            raise PowerLoss

    with monkeypatch.context() as fault:
        fault.setattr(checkpoint.os, "replace", crash)
        with pytest.raises(PowerLoss):
            checkpoint.restore_checkpoint(saved)
    checkpoint.restore_checkpoint(saved)
    _assert_restored(roots)


@pytest.mark.parametrize("effect", ["copy", "sync"])
def test_snapshot_is_not_ready_after_copy_or_sync_failure(tmp_path, monkeypatch, effect):
    root = tmp_path / "data"
    root.mkdir()
    (root / "keep").write_text("original")
    original = checkpoint._run

    def fail(argv):
        if argv[0] == ("cp" if effect == "copy" else "sync"):
            raise SupervisorError("injected I/O failure")
        return original(argv)

    monkeypatch.setattr(checkpoint, "_run", fail)
    with pytest.raises(SupervisorError):
        checkpoint.create_stopped_snapshot(
            tmp_path / "checkpoint", (root,), boundary_sha256="b" * 64
        )
    document = json.loads((tmp_path / "checkpoint" / "checkpoint.json").read_text())
    assert not document["ready"]
    assert (root / "keep").read_text() == "original"
    assert document["workspaces"]  # Partial copies remain reachable for retention.


def test_absence_nested_coverage_and_prepared_source_mapping(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    nested = live / "nested"
    nested.mkdir()
    absent = tmp_path / "absent"
    saved = checkpoint.create_stopped_snapshot(
        tmp_path / "checkpoint", (live, nested, absent), boundary_sha256="b" * 64
    )
    checkpoint.check_checkpoint_roots(saved, (nested, absent))
    with pytest.raises(SupervisorError):
        checkpoint.check_checkpoint_roots(saved, (tmp_path / "uncovered",))
    absent.mkdir()
    (absent / "new").write_text("candidate")
    checkpoint.restore_checkpoint(saved)
    assert not absent.exists()
    assert nested.exists()
    source = tmp_path / "prepared"
    source.mkdir()
    (source / "candidate").write_text("prepared")
    candidate = checkpoint.create_checkpoint(
        tmp_path / "candidate-checkpoint",
        (checkpoint.SnapshotRoot(live, source),),
        boundary_sha256="c" * 64,
    )
    checkpoint.restore_checkpoint(candidate)
    assert (live / "candidate").read_text() == "prepared"
    assert not nested.exists()


def test_catalog_identity_and_legacy_subset_cannot_authorize_rollback(tmp_path):
    saved, roots = _case(tmp_path)
    with pytest.raises(SupervisorError):
        checkpoint.read_checkpoint(saved.directory, expected_sha256="f" * 64)
    path = saved.directory / "checkpoint.json"
    document = json.loads(path.read_text())
    document["version"] = 2
    path.write_text(json.dumps(document))
    with pytest.raises(SupervisorError):
        checkpoint.restore_checkpoint(saved)
    assert (roots[0] / "candidate").exists()


def test_staging_is_fresh_and_outside_every_root(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    with pytest.raises(SupervisorError):
        checkpoint.create_stopped_snapshot(live / "checkpoint", (live,), boundary_sha256="b" * 64)
    destination = tmp_path / "checkpoint"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        checkpoint.create_stopped_snapshot(destination, (live,), boundary_sha256="b" * 64)


def test_space_estimate_is_advisory_uses_allocated_bytes_and_one_copy(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    with (live / "sparse").open("wb") as stream:
        stream.seek(1024**3)
        stream.write(b"x")
    monkeypatch.setattr(
        os, "statvfs", lambda _: SimpleNamespace(f_bavail=0, f_frsize=4096, f_favail=0)
    )
    with pytest.warns(UserWarning):
        estimates = checkpoint.check_snapshot_space(tmp_path, (live,))
    assert 0 < estimates[0]["bytes"] < 1024**2


@pytest.mark.skipif(sys.platform != "linux", reason="GNU copy metadata qualification")
def test_gnu_copy_preserves_special_entries_metadata_and_cross_root_hardlinks(tmp_path):
    roots = (tmp_path / "data", tmp_path / "project")
    for root in roots:
        root.mkdir()
    file = roots[0] / "odd\nname"
    file.write_bytes(b"linked")
    file.chmod(0o666)
    os.link(file, roots[1] / "link")
    os.mkfifo(roots[0] / "fifo")
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(roots[0] / "socket"))
    os.setxattr(file, "user.checkpoint", b"kept")
    saved = checkpoint.create_stopped_snapshot(
        tmp_path / "checkpoint", roots, boundary_sha256="b" * 64
    )
    checkpoint.restore_checkpoint(saved)
    assert file.stat().st_ino == (roots[1] / "link").stat().st_ino
    assert file.stat().st_nlink == 2
    assert stat.S_IMODE(file.stat().st_mode) == 0o666
    assert os.getxattr(file, "user.checkpoint") == b"kept"
    assert stat.S_ISFIFO((roots[0] / "fifo").stat().st_mode)
    assert stat.S_ISSOCK((roots[0] / "socket").stat().st_mode)


@pytest.mark.skipif(sys.platform != "linux", reason="GNU cp overlay semantics")
def test_overlay_replaces_destination_symlinks_instead_of_writing_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    source, destination = tmp_path / "candidate", tmp_path / "live"
    source.mkdir()
    destination.mkdir()
    (source / "view").write_bytes(b"candidate")
    (destination / "view").symlink_to(outside)
    checkpoint.copy_contents(source, destination)
    assert outside.read_bytes() == b"untouched"
    assert not (destination / "view").is_symlink()
    assert (destination / "view").read_bytes() == b"candidate"


def test_privileged_copy_refuses_root_owned_paths(tmp_path: Path) -> None:
    system = Path("/usr/share")
    assert system.stat().st_uid == 0
    with pytest.raises(SupervisorError, match="owned by root"):
        checkpoint.create_stopped_snapshot(
            tmp_path / "checkpoint", (system,), boundary_sha256="b" * 64
        )


def test_privileged_copy_refuses_roots_below_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(SupervisorError, match="symlink"):
        checkpoint.create_stopped_snapshot(
            tmp_path / "checkpoint", (tmp_path / "link" / "new",), boundary_sha256="b" * 64
        )


def test_overlay_lets_candidate_entries_replace_a_different_type(tmp_path: Path) -> None:
    source, destination = tmp_path / "candidate", tmp_path / "live"
    (source / "was-file").mkdir(parents=True)
    (source / "was-file" / "inside").write_bytes(b"candidate")
    (source / "was-dir").write_bytes(b"candidate")
    (destination / "was-dir").mkdir(parents=True)
    (destination / "was-dir" / "old").write_bytes(b"old")
    (destination / "was-file").write_bytes(b"old")
    (destination / "kept").write_bytes(b"unrelated")
    checkpoint.copy_contents(source, destination)
    assert (destination / "was-file" / "inside").read_bytes() == b"candidate"
    assert (destination / "was-dir").read_bytes() == b"candidate"
    assert (destination / "kept").read_bytes() == b"unrelated"
