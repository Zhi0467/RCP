from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.agents.hidden_read import HIDDEN_READ_ENV_ALLOW_LIST
from rcp.agents.provider_environment import prepare_hidden_read_launch
from rcp.agents.write_scope import ProjectWriteScope
from rcp.core.models import HiddenReadScope, HiddenReadStatus
from rcp.providers import profile_for

CAPABILITIES = ("discuss", "work_auto", "orchestrate", "scratch_patch", "paper_readonly")


def _scope() -> HiddenReadScope:
    return HiddenReadScope(
        execution_machine="local",
        execution_host="",
        os_account="research",
        hidden_directories=("/secret/folder",),
        hidden_files=("/secret/file",),
        hidden_globs=("/secret/db*",),
        env_allow_list=HIDDEN_READ_ENV_ALLOW_LIST,
        enforcement=HiddenReadStatus(status="enforced"),
    )


def _write_scope(cwd, capability):
    if capability not in {"work_auto", "orchestrate"}:
        return None
    return ProjectWriteScope.create(
        project_id="project",
        execution_machine="local",
        execution_host="",
        capability=capability,
        stage_root=str(cwd),
        workspace_root=str(cwd),
        repositories=[],
        protected_write_paths=[],
    )


@pytest.mark.parametrize("capability", CAPABILITIES)
def test_claude_all_capabilities_render_scope(capability, tmp_path):
    scope = _scope()
    command = AgentLauncher._command(
        "claude",
        "task",
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[],
        capability=capability,
        hidden_read_scope=scope,
        provider_version="2.1.288",
        write_scope=_write_scope(tmp_path, capability),
    )
    settings = json.loads(command[command.index("--settings") + 1])
    assert {"Read(//secret/folder/**)", "Read(//secret/file)", "Read(//secret/db*)"} <= set(
        settings["permissions"]["deny"]
    )
    assert str(tmp_path / "rcp-hidden-read.py") in settings["env"]["CLAUDE_CODE_SHELL_PREFIX"]


@pytest.mark.parametrize("provider", ("claude", "codex", "opencode"))
@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("resolved", (True, False))
@pytest.mark.asyncio
async def test_launcher_always_passes_scope(monkeypatch, tmp_path, provider, capability, resolved):
    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *a, **kw: ProviderReadiness(
            provider=provider,
            installed=True,
            authenticated=True,
            binary_path=provider,
            path_state="resolved",
        ),
    )
    monkeypatch.setattr("rcp.agents.launcher.work_like_launch_problem", lambda _: None)
    monkeypatch.setattr(launcher, "_command", lambda *a, **kw: [provider])
    captured = []

    class Captured(Exception):
        pass

    def turn(request):
        captured.append(request)
        raise Captured

    profile = profile_for(provider)
    monkeypatch.setattr(
        type(profile.runtime(profile.legacy_runtime_id)), "turn", lambda _, r: turn(r)
    )
    scope = _scope() if resolved else None
    with pytest.raises(Captured):
        async for _ in launcher.stream(
            provider,
            "task",
            cwd=tmp_path,
            capability=capability,
            runtime_id=profile.legacy_runtime_id,
            hidden_read_scope=scope,
        ):
            pass
    actual = captured[0].hidden_read_scope
    assert actual is not None
    if resolved:
        assert actual is scope
    else:
        assert actual.enforcement.status == "unhidden"
        assert actual.enforcement.reasons


@pytest.mark.parametrize("provider", ("claude", "opencode"))
@pytest.mark.parametrize("failed", (False, True))
@pytest.mark.parametrize("remote", (False, True))
def test_preparation_confirms_before_resolving_and_stages_same_policy(
    monkeypatch, tmp_path, manifest, provider, failed, remote
):
    from rcp import ssh_agent
    from rcp.agents import hidden_read

    order = []
    monkeypatch.setattr(ssh_agent, "user_ssh_identity_candidates", lambda _: ("/user/key",))
    monkeypatch.setattr(ssh_agent, "running_agent_socket", lambda: "/deploy/socket")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/user/socket")

    def confirm(**kwargs):
        order.append(kwargs)
        return ()

    def resolve(**kwargs):
        assert len(order) == (0 if remote else 2)
        if remote:
            assert {(key.path, key.kind, key.visibility) for key in kwargs["key_evidence"]} == {
                ("/home/research/.ssh/id_ed25519", "ssh_identity", "readable"),
                ("/deploy/key", "deploy_key", "readable"),
            }
        assert kwargs["machine_hidden_folders"] == ["/custom"]
        if failed:
            raise OSError("host unavailable")
        return _scope()

    monkeypatch.setattr(ssh_agent, "confirm_key_evidence", confirm)
    monkeypatch.setattr(hidden_read, "resolve_hidden_read_scope", resolve)

    class Stage:
        workspace = tmp_path

        def __init__(self):
            self.files = {}

        def _ssh(self, command):
            output = {
                "printenv": "/home/research\n",
                "find": "/home/research/.ssh/id_ed25519\n",
            }.get(command[0], "")
            return subprocess.CompletedProcess(command, 0, output, "")

        def write_workspace_text(self, name, content):
            self.files[name] = content

    stage = Stage() if remote else None
    scope = prepare_hidden_read_launch(
        manifest=manifest,
        execution_machine="laptop",
        provider=provider,
        capability="discuss",
        stage_root=str(tmp_path),
        workspace_root=str(tmp_path),
        app_data_dir=tmp_path,
        remote_stage=stage,
        machine_hidden_folders=["/custom"],
        git_access=SimpleNamespace(checkouts=(("/repo", "/deploy/key"),)),
    )
    assert [item["agent_socket"] for item in order] == (
        [] if remote else ["/user/socket", "/deploy/socket"]
    )
    if failed:
        assert scope.enforcement.status == "unhidden"
        assert scope.enforcement.reasons
    else:
        policy_name = "rcp-hidden-read.py.policy.json"
        policy = stage.files[policy_name] if stage else (tmp_path / policy_name).read_text()
        assert HiddenReadScope.model_validate_json(policy) == scope
        if not remote:
            assert (tmp_path / "rcp-hidden-read.py").stat().st_mode & 0o100
        if provider == "opencode":
            assert "provider_native_tools_uncovered" in scope.enforcement.reasons
