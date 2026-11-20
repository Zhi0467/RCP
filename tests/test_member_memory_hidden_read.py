from __future__ import annotations

from pathlib import Path


def test_hidden_paths_never_nest_inside_a_hidden_folder():
    """A Linux sandbox cannot mask a path inside a folder it already masked read-only."""
    from rcp.agents.hidden_read import hidden_read_defaults

    directories, files, globs = hidden_read_defaults(
        home="/home/member",
        app_data_dir="/data",
        credential_roots=(),
        provider_login_files=(),
        control_socket_dir=None,
    )
    for path in (*directories, *files, *globs):
        parents = [folder for folder in directories if folder != path]
        assert not any(Path(path).is_relative_to(folder) for folder in parents), path
    assert not any("agent-threads" in path or "/memory/" in path for path in (*directories, *globs))
