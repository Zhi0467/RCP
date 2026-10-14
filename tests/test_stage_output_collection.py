"""Invariant 4b: local collection reads only regular files inside the exact stage."""

from __future__ import annotations

from pathlib import Path

import pytest

from rcp.runs.chat import _read_chat_patch, _read_watch_request
from rcp.runs.shared import AgentOutputProblem, _collect_patch_text, _existing_exact_patch_digest


def test_local_collection_never_follows_a_link_out_of_the_stage(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"ops": []}', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("patch.json", "watch.json"):
        (workspace / name).symlink_to(outside)

    assert _read_chat_patch(workspace, None) is None
    assert _read_watch_request(workspace, None) is None
    assert _existing_exact_patch_digest(workspace, None) is None
    with pytest.raises(AgentOutputProblem):
        _collect_patch_text(workspace, None)
