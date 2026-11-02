from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents.write_scope import ProjectWriteScope, WritableRepositoryRoot


def _project(stage, project_id, *, memory_roots=None):
    return ProjectWriteScope.create(
        project_id=project_id,
        execution_machine="local",
        execution_host="",
        capability="work_auto",
        stage_root=str(stage / "projects" / project_id),
        workspace_root=str(stage / "projects" / project_id),
        repositories=[
            WritableRepositoryRoot(alias="repo", machine="local", path=f"/repos/{project_id}")
        ],
        protected_write_paths=[
            f"/repos/{project_id}/.research",
            *(["/memory"] if memory_roots else []),
        ],
        memory_roots=memory_roots,
    )


def test_composite_provider_scope_preserves_project_boundaries(tmp_path):
    from rcp.agents.write_scope import CompositeWriteScope
    from rcp.providers.base import _require_project_write_scope

    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    composite = CompositeWriteScope(stage_root=str(tmp_path), projects=(b, a))
    assert (
        composite.fingerprint
        == CompositeWriteScope(stage_root=str(tmp_path), projects=(a, b)).fingerprint
    )
    assert composite.repository_roots == ["/repos/a", "/repos/b"]
    assert composite.protected_write_paths == ["/repos/a/.research", "/repos/b/.research"]
    assert str(tmp_path) in composite.writable_roots
    assert (
        _require_project_write_scope(
            composite,
            capability="work_auto",
            write_dirs=[Path(p) for p in composite.repository_roots],
        )
        is composite
    )
    with pytest.raises(ValueError):
        CompositeWriteScope(stage_root=str(tmp_path / "other"), projects=(a,))
    with pytest.raises(ValueError):
        CompositeWriteScope(stage_root=str(tmp_path), projects=(a, a))


def test_zero_project_scope_only_writes_its_stage(tmp_path):
    from rcp.agents.write_scope import CompositeWriteScope

    scope = CompositeWriteScope(stage_root=str(tmp_path))
    assert scope.fingerprint is None
    assert scope.writable_roots == [str(tmp_path)]
    assert scope.repository_roots == []


def test_hidden_union_refuses_over_budget(tmp_path, monkeypatch):
    from rcp.core.models import HiddenReadScope, HiddenReadStatus
    from rcp.runs.space_chat import merge_space_hidden_read_scopes

    def scope(path):
        return HiddenReadScope(
            execution_machine="local",
            execution_host="",
            os_account="user",
            hidden_directories=(path,),
            enforcement=HiddenReadStatus(status="enforced"),
        )

    monkeypatch.setattr("rcp.runs.space_chat.HIDDEN_READ_PATH_MAX_COUNT", 1)
    with pytest.raises(ValueError, match="too_many_hidden_folders"):
        merge_space_hidden_read_scopes([scope("/private/a"), scope("/private/b")])


def test_composite_invocation_gate_keeps_each_mailbox_secret_out_of_argv():
    import json

    from rcp.agents.invocation_broker import ProviderInvocationGate

    child = ProviderInvocationGate(
        mailbox_id="b" * 32,
        broker_path="/child.py",
        socket_path="/child.sock",
        workspace="/stage/projects/p",
        response_timeout_seconds=5,
        _token="child-secret",
    )
    gate = ProviderInvocationGate(
        mailbox_id="a" * 32,
        broker_path="/top.py",
        socket_path="/top.sock",
        workspace="/stage",
        response_timeout_seconds=5,
        _token="top-secret",
        children=(child,),
    )
    command = gate.wrap_command(["provider"])
    assert command.index("/top.py") < command.index("/child.py") < command.index("provider")
    assert "child-secret" not in command and "top-secret" not in command
    top, nested, prompt = gate.bootstrap(b"prompt").split(b"\n", 2)
    assert json.loads(top)["token"] == "top-secret"
    assert json.loads(nested)["token"] == "child-secret"
    assert prompt == b"prompt"
    assert gate.ready_lines == {gate.ready_line, child.ready_line}


def test_merged_hidden_policy_installs_its_own_wrapper(tmp_path):
    import json

    from rcp.core.models import HiddenReadScope, HiddenReadStatus
    from rcp.runs.space_chat import install_space_hidden_read_scope, merge_space_hidden_read_scopes

    scopes = [
        HiddenReadScope(
            execution_machine="local",
            execution_host="",
            os_account="fixture",
            account_home=str(tmp_path),
            hidden_directories=(f"/private/{name}",),
            enforcement=HiddenReadStatus(status="enforced"),
        )
        for name in ("a", "b")
    ]
    merged = merge_space_hidden_read_scopes(scopes)
    assert merged.fingerprint not in {scope.fingerprint for scope in scopes}
    install_space_hidden_read_scope(merged)
    wrapper = Path(merged.wrapper_path())
    assert wrapper.is_file()
    policy = json.loads(wrapper.with_name(wrapper.name + ".policy.json").read_text())
    assert policy["fingerprint"] == merged.fingerprint
    assert policy["hidden_directories"] == ["/private/a", "/private/b"]


def test_nonregular_space_output_is_refused_without_blocking(tmp_path):
    import os

    from rcp.runs.space_projects import read_space_output

    fifo = tmp_path / "watch.json"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        read_space_output(fifo)
    link = tmp_path / "patch.json"
    link.symlink_to(fifo)
    with pytest.raises(OSError):
        read_space_output(link)


@pytest.mark.parametrize("project_ids", [(), ("b", "a")])
def test_composite_memory_roots_reach_claude_launch_permissions(tmp_path, project_ids):
    import json

    from rcp.agents.write_scope import CompositeWriteScope
    from rcp.providers.claude.profile import ClaudeProfile

    projects = tuple(
        _project(tmp_path, project_id, memory_roots=[f"/memory/{project_id}/shared"])
        for project_id in project_ids
    )
    scope = CompositeWriteScope(stage_root=str(tmp_path), projects=projects)
    memory_roots = sorted(f"/memory/{project_id}/shared" for project_id in project_ids)
    command = ClaudeProfile().command(
        "Member request",
        binary="claude",
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[],
        write_dirs=[Path(root) for root in scope.repository_roots],
        write_scope=scope,
        capability="work_auto",
        provider_version="2.1.296",
    )
    assert scope.memory_roots == memory_roots
    added = [command[index + 1] for index, value in enumerate(command) if value == "--add-dir"]
    assert added == [*scope.repository_roots, *memory_roots]
    permissions = json.loads(command[command.index("--settings") + 1])["permissions"]
    for root in memory_roots:
        assert f"Edit(/{root}/**)" in permissions["allow"]
    assert "Edit(//memory/**)" not in permissions["deny"]
    for root in scope.repository_roots:
        assert f"Edit(/{root}/.research/**)" in permissions["deny"]
