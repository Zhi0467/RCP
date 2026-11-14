from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import threading
import time

import pytest

from rcp.paper import folder_io
from rcp.paper.folder_io import UnsupportedPath, paper_root
from rcp.transport.run_stage import RemoteRunStage


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def root(tmp_path):
    return paper_root(tmp_path / "data", "project")


def test_root_layout_and_symlinked_project_refused(tmp_path):
    assert paper_root(tmp_path, "p1") == tmp_path / "paper/p1"
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "paper/p2").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        paper_root(tmp_path, "p2")
    with pytest.raises(ValueError):
        paper_root(tmp_path, "../p3")


def test_new_path_is_created_with_parents(root):
    result = folder_io.write(root, "sections/intro.md", b"hello", None)
    assert (root / "sections/intro.md").read_bytes() == b"hello"
    assert result.sha == sha(b"hello")
    assert result.displaced is None
    assert not result.conflict


def test_swap_returns_bytes_written_since_the_callers_read(root):
    folder_io.write(root, "main.tex", b"v1", None)
    (root / "main.tex").write_bytes(b"v2 from an agent")
    result = folder_io.write(root, "main.tex", b"v3", sha(b"v1"))
    assert (root / "main.tex").read_bytes() == b"v3"
    assert result.displaced == b"v2 from an agent"
    assert result.displaced_sha == sha(b"v2 from an agent")
    assert result.conflict
    again = folder_io.write(root, "main.tex", b"v4", sha(b"v3"))
    assert again.displaced == b"v3" and not again.conflict


def test_swap_keeps_existing_permissions(root):
    folder_io.write(root, "run.sh", b"#!/bin/sh\n", None)
    os.chmod(root / "run.sh", 0o751)
    folder_io.write(root, "run.sh", b"#!/bin/sh\necho\n", sha(b"#!/bin/sh\n"))
    assert (root / "run.sh").stat().st_mode & 0o777 == 0o751


def test_late_write_through_a_held_descriptor_is_returned(root):
    folder_io.write(root, "notes.md", b"base", None)
    held = os.open(root / "notes.md", os.O_WRONLY | os.O_APPEND)
    os.write(held, b"+early")

    def late():
        time.sleep(0.2)
        os.write(held, b"+late")

    writer = threading.Thread(target=late)
    writer.start()
    try:
        result = folder_io.write(
            root, "notes.md", b"mine", sha(b"base"), settle_seconds=0.5, settle_max_seconds=5
        )
    finally:
        writer.join()
        os.close(held)
    assert result.displaced == b"base+early+late"
    assert (root / "notes.md").read_bytes() == b"mine"


def test_new_path_never_clobbers_a_concurrent_create(root, monkeypatch):
    real_link = os.link

    def racing_link(source, destination, **kwargs):
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=kwargs["dst_dir_fd"]
        )
        os.write(descriptor, b"theirs")
        os.close(descriptor)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    result = folder_io.write(root, "fig.txt", b"ours", None)
    monkeypatch.setattr(os, "link", real_link)
    assert result.displaced == b"theirs"
    assert result.conflict
    assert (root / "fig.txt").read_bytes() == b"ours"


def test_links_and_special_files_are_refused_for_writes(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside)
    (root / "alias.md").symlink_to(outside / "target.md")
    (root / "real.md").write_bytes(b"x")
    os.link(root / "real.md", root / "hard.md")
    os.mkfifo(root / "pipe")
    for path in ("linked/a.md", "alias.md", "hard.md", "pipe"):
        with pytest.raises(UnsupportedPath):
            folder_io.write(root, path, b"data", None)
    assert list(outside.iterdir()) == []
    assert (root / "real.md").read_bytes() == b"x"


def test_scan_reports_stat_data_and_unsupported_entries(root, tmp_path):
    folder_io.write(root, "main.tex", b"\\documentclass{article}", None)
    folder_io.write(root, "figs/plot.png", b"\x89PNG", None)
    (root / "figs/.DS_Store").write_bytes(b"")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/x.pyc").write_bytes(b"")
    (root / "main.aux").write_bytes(b"")
    (root / ".main.tex.swp").write_bytes(b"")
    (root / ".rcp-paper-leftover").write_bytes(b"")
    (root / "linked").symlink_to(tmp_path)
    os.link(root / "main.tex", root / "hard.tex")
    os.mkfifo(root / "pipe")
    (root / "bad\\name.md").write_bytes(b"")
    (root / ".git").mkdir()
    (root / ".git/HEAD").write_bytes(b"")

    entries = {entry.path: entry for entry in folder_io.scan(root)}

    assert set(entries) == {
        "main.tex",
        "figs/plot.png",
        "linked",
        "hard.tex",
        "pipe",
        "bad\\name.md",
        ".git",
    }
    assert {path for path, entry in entries.items() if entry.kind == "file"} == {"figs/plot.png"}
    plot = entries["figs/plot.png"]
    status = (root / "figs/plot.png").lstat()
    assert (plot.size, plot.mtime_ns, plot.inode) == (4, status.st_mtime_ns, status.st_ino)


def test_paths_follow_paper_rules(root):
    for path in ("", "../x", "/abs", "a//b", "a/./b", ".git/config", "a\\b", "a\x01", "x" * 256):
        with pytest.raises(ValueError):
            folder_io.write(root, path, b"data", None)
        with pytest.raises(ValueError):
            folder_io.read(root, [path])


def test_read_and_hash_skip_missing_and_unsupported(root, tmp_path):
    folder_io.write(root, "a.md", b"alpha", None)
    (tmp_path / "secret").write_bytes(b"secret")
    (root / "link.md").symlink_to(tmp_path / "secret")
    paths = ["a.md", "missing.md", "link.md", "nodir/x.md"]
    assert folder_io.read(root, paths) == {
        "a.md": b"alpha",
        "missing.md": None,
        "link.md": None,
        "nodir/x.md": None,
    }
    assert folder_io.hash(root, paths) == {
        "a.md": sha(b"alpha"),
        "missing.md": None,
        "link.md": None,
        "nodir/x.md": None,
    }


def test_delete_returns_displaced_bytes_and_is_idempotent(root):
    folder_io.write(root, "a.md", b"v1", None)
    (root / "a.md").write_bytes(b"v2")
    result = folder_io.delete(root, "a.md", sha(b"v1"))
    assert not (root / "a.md").exists()
    assert result.displaced == b"v2" and result.conflict and result.sha is None
    again = folder_io.delete(root, "a.md", sha(b"v1"))
    assert again.displaced is None
    assert [entry.path for entry in folder_io.scan(root)] == []


def test_rename_never_replaces_and_creates_parents(root):
    folder_io.write(root, "a.md", b"a", None)
    folder_io.write(root, "b.md", b"b", None)
    with pytest.raises(FileExistsError):
        folder_io.rename(root, "a.md", "b.md")
    folder_io.rename(root, "a.md", "drafts/a.md")
    assert (root / "drafts/a.md").read_bytes() == b"a"
    assert not (root / "a.md").exists()
    with pytest.raises(FileNotFoundError):
        folder_io.rename(root, "a.md", "c.md")


def test_remote_stage_ships_the_stdlib_only_module(tmp_path, monkeypatch):
    stage = RemoteRunStage("example.invalid")

    def remote(arguments, *, input_data=None, **kwargs):
        assert arguments[0] == "python3"
        return subprocess.run(
            [sys.executable, "-I", *arguments[1:]],
            input=input_data,
            capture_output=True,
            env={"HOME": str(tmp_path)},
        )

    monkeypatch.setattr(stage, "_ssh_bytes", remote)

    def call(request: dict) -> dict:
        return stage.paper_folder({"project_id": "project", **request})

    encoded = base64.b64encode(b"remote text").decode()
    assert call({"op": "root"}) == {"root": str(tmp_path / ".rcp/paper/project")}
    written = call({"op": "write", "path": "a.md", "data": encoded, "expect_sha": None})
    assert written == {
        "sha": sha(b"remote text"),
        "displaced": None,
        "displaced_sha": None,
        "conflict": False,
    }
    replaced = call({"op": "write", "path": "a.md", "data": "", "expect_sha": sha(b"other")})
    assert base64.b64decode(replaced["displaced"]) == b"remote text"
    assert replaced["conflict"]
    call({"op": "rename", "source": "a.md", "destination": "b/a.md"})
    [entry] = call({"op": "scan"})["entries"]
    assert (entry["path"], entry["kind"], entry["size"]) == ("b/a.md", "file", 0)
    assert call({"op": "read", "paths": ["b/a.md", "gone"]})["files"] == {
        "b/a.md": "",
        "gone": None,
    }
    assert call({"op": "hash", "paths": ["b/a.md"]})["hashes"] == {"b/a.md": sha(b"")}
    deleted = call({"op": "delete", "path": "b/a.md", "expect_sha": sha(b"")})
    assert deleted["displaced"] == "" and not deleted["conflict"]
    assert call({"op": "scan"})["entries"] == []
