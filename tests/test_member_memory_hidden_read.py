from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from rcp.agents.hidden_read import HiddenFolderRejected


@pytest.mark.parametrize("owner", [None, "alice", "bob"])
def test_thread_stages_are_hidden_except_from_their_owner(owner):
    from rcp.agents.hidden_read import hidden_read_defaults

    directories, _, globs = hidden_read_defaults(
        home="/home/member",
        app_data_dir="/data",
        credential_roots=(),
        provider_login_files=(),
        control_socket_dir=None,
        thread_owner=owner,
        member_ids=("alice", "bob"),
    )
    for root in ("/data/agent-threads", "/home/member/.rcp/agent-threads"):
        for member in ("alice", "bob"):
            stage = Path(root) / member / "stages/turn"
            assert any(stage.is_relative_to(path) for path in directories) == (member != owner)
            # Transcripts stay hidden from every run, the owner's included.
            transcript = Path(root) / member / "thread.jsonl"
            assert any(transcript.is_relative_to(path) for path in directories) or any(
                fnmatch.fnmatch(str(transcript), pattern) for pattern in globs
            )


@pytest.mark.asyncio
async def test_thread_policy_budget_rejection_reaches_launch_owner(manifest, tmp_path, monkeypatch):
    from rcp.agents import hidden_read
    from rcp.runs.shared import _prepare_hidden_read_scope
    from rcp.service import RunRequest
    from tests.helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    monkeypatch.setattr(hidden_read, "HIDDEN_READ_PATH_MAX_COUNT", 1)
    with pytest.raises(HiddenFolderRejected):
        await _prepare_hidden_read_scope(
            app.state.service,
            RunRequest(provider="codex", run_on="laptop"),
            workspace=tmp_path,
            remote_stage=None,
            execution=None,
            capability="work_auto",
            data_dir=tmp_path / "data",
        )
