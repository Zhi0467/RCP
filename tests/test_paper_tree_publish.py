from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.transport import LocalStateWorkspace, SSHStateWorkspace
from rcp.transport.state import _paper_tree_digest


@pytest.fixture(params=[False, True], ids=["local", "ssh"])
def publication(request, tmp_path, monkeypatch):
    root = tmp_path / "canonical" / ".research"
    if request.param:
        workspace = SSHStateWorkspace(tmp_path / "mirror", "research.example", str(root.parent))

        def ssh(arguments, **kwargs):
            if arguments[0] == "python3":
                arguments = [sys.executable, *arguments[1:]]
            return subprocess.run(arguments, capture_output=True, text=True, check=False)

        def push(stage, paths, *, source_root=None):
            shutil.copytree(source_root, stage, dirs_exist_ok=True)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(workspace, "_ssh", ssh)
        monkeypatch.setattr(workspace, "_push_state_files", push)
    else:
        workspace = LocalStateWorkspace(root, str(root))
    return workspace, root


def test_tree_publication_replaces_the_whole_tree(publication, tmp_path):
    workspace, root = publication
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.md").write_text("old")
    (source / "remove.md").write_text("removed")
    workspace.publish_paper_tree(source, 1)
    (source / "old.md").rename(source / "renamed.md")
    (source / "remove.md").unlink()
    (source / "figures").mkdir()
    (source / "figures" / "data.bin").write_bytes(b"\x00\xff")
    workspace.publish_paper_tree(source, 2)
    assert _paper_tree_digest(root / "paper") == _paper_tree_digest(source)
    assert not (root / "paper" / "old.md").exists()
    assert not (root / "paper" / "remove.md").exists()
    assert json.loads((root / ".publish" / "paper-current.json").read_text())["revision"] == 2
    workspace.publish_paper_tree(source, 2)
    with workspace.read_snapshot() as snapshot:
        assert _paper_tree_digest(snapshot / "paper") == _paper_tree_digest(source)
    shutil.rmtree(source)
    source.mkdir()
    workspace.publish_paper_tree(source, 3)
    assert list((root / "paper").iterdir()) == []


def test_publication_reconciles_a_lost_commit_response(publication, tmp_path, monkeypatch):
    workspace, root = publication
    source = tmp_path / "source"
    source.mkdir()
    (source / "paper.md").write_text("durable")
    if isinstance(workspace, SSHStateWorkspace):
        ssh = workspace._ssh
        lost = False

        def lose_response(arguments, **kwargs):
            nonlocal lost
            result = ssh(arguments, **kwargs)
            if arguments[-1] == "select" and not lost:
                assert result.returncode == 0, result.stderr
                lost = True
                return subprocess.CompletedProcess(arguments, 255, "", "connection lost")
            return result

        monkeypatch.setattr(workspace, "_ssh", lose_response)
        workspace.publish_paper_tree(source, 1)
        assert lost
    else:
        import os

        replace = os.replace

        def fail_marker(src, dest):
            if Path(dest).name == "paper-current.json":
                raise OSError("interrupted after tree selection")
            replace(src, dest)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", fail_marker)
            with pytest.raises(OSError):
                workspace.publish_paper_tree(source, 1)
        assert (root / "paper" / "paper.md").read_text() == "durable"
        workspace.publish_paper_tree(source, 1)
    assert json.loads((root / ".publish" / "paper-current.json").read_text())["revision"] == 1
    assert (root / "paper" / "paper.md").read_text() == "durable"


def test_publication_refuses_symlinks_without_changing_selected_tree(publication, tmp_path):
    workspace, root = publication
    source = tmp_path / "source"
    source.mkdir()
    (source / "paper.md").write_text("original")
    workspace.publish_paper_tree(source, 1)
    (source / "link.md").symlink_to(source / "paper.md")
    with pytest.raises(ValueError):
        workspace.publish_paper_tree(source, 2)
    assert [p.name for p in (root / "paper").iterdir()] == ["paper.md"]
