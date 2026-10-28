"""Publication scratch is retained only while the remote outcome is unknown."""

import subprocess

import pytest

from rcp.transport import BatchPublishFailed, SSHStateWorkspace
from tests import test_paper_tree_publish

publication = test_paper_tree_publish.publication


def test_failed_selection_removes_stage(publication, tmp_path, monkeypatch):
    workspace, root = publication
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("pending")
    if isinstance(workspace, SSHStateWorkspace):
        ssh = workspace._ssh

        def fail_select(arguments, **kwargs):
            if arguments[-1] == "select":
                return subprocess.CompletedProcess(arguments, 255, "", "lost")
            return ssh(arguments, **kwargs)

        monkeypatch.setattr(workspace, "_ssh", fail_select)
        error = BatchPublishFailed
    else:

        def fail_select(*args):
            raise OSError("selection failed")

        monkeypatch.setattr("rcp.transport.state._select_paper_tree", fail_select)
        error = OSError
    with pytest.raises(error):
        workspace.publish_paper_tree(source, 1)
    assert not list((root / ".publish").glob("paper-stage-*"))
