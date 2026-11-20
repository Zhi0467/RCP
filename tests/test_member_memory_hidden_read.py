from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents.hidden_read import HiddenFolderRejected


@pytest.mark.parametrize("owner", [None, "alice"])
def test_hidden_paths_never_nest_inside_a_hidden_folder(owner):
    """A Linux sandbox cannot mask a path inside a folder it already masked read-only."""
    from rcp.agents.hidden_read import hidden_read_defaults

    directories, files, globs = hidden_read_defaults(
        home="/home/member",
        app_data_dir="/data",
        credential_roots=(),
        provider_login_files=(),
        control_socket_dir=None,
        thread_owner=owner,
    )
    for path in (*directories, *files, *globs):
        parents = [folder for folder in directories if folder != path]
        assert not any(Path(path).is_relative_to(folder) for folder in parents), path
    threads = [path for path in (*directories, *globs) if "agent-threads" in path]
    assert bool(threads) == (owner is None)


@pytest.mark.asyncio
async def test_thread_policy_budget_rejection_reaches_launch_owner(manifest, tmp_path, monkeypatch):
    from rcp.agents import AgentProcessControl, hidden_read
    from rcp.background import AgentTaskExecution
    from rcp.machine_cards import BACKEND_MACHINE_ID
    from rcp.runs.shared import _prepare_hidden_read_scope
    from rcp.service import RunRequest
    from tests.helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    monkeypatch.setattr(hidden_read, "HIDDEN_READ_PATH_MAX_COUNT", 1)
    execution = AgentTaskExecution(
        operation_id="unbound", store=app.state.catalog.store, control=AgentProcessControl()
    )
    with pytest.raises(HiddenFolderRejected):
        await _prepare_hidden_read_scope(
            app.state.service,
            RunRequest(provider="codex", run_on=BACKEND_MACHINE_ID),
            workspace=tmp_path,
            remote_stage=None,
            execution=execution,
            capability="work_auto",
            data_dir=tmp_path / "data",
        )
