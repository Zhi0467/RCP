from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.agents.hidden_read import HIDDEN_READ_ENV_ALLOW_LIST
from rcp.agents.provider_environment import prepare_hidden_read_launch
from rcp.agents.write_scope import ProjectWriteScope
from rcp.core.models import HiddenReadScope, HiddenReadStatus
from rcp.providers import profile_for
from rcp.providers.browser_grant import BrowserGrant

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
    browser_status = HiddenReadStatus(status="unhidden", reasons=("browser_unwrapped_macos",))
    events = []
    with pytest.raises(Captured):
        async for _ in launcher.stream(
            provider,
            "task",
            cwd=tmp_path,
            capability=capability,
            runtime_id=profile.legacy_runtime_id,
            hidden_read_scope=scope,
            browser_grant=BrowserGrant(
                requested=True, status="granted", hidden_read_enforcement=browser_status
            ),
        ):
            events.append(_)
    assert events == []
    actual = captured[0].hidden_read_scope
    assert actual is not None
    prompt = captured[0].prompt
    policy = json.loads(prompt[prompt.index("{") :])
    for field in (
        "enforcement",
        "hidden_directories",
        "hidden_files",
        "hidden_globs",
        "env_allow_list",
        "fingerprint",
    ):
        assert policy[field] == actual.model_dump(mode="json")[field]
    assert policy["browser_enforcement"] == browser_status.model_dump(mode="json")
    assert policy["effective_enforcement"] == {
        "status": "unhidden",
        "reasons": sorted(set(actual.enforcement.reasons) | set(browser_status.reasons)),
    }
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


def _browser_admission_calls():
    from rcp import runs

    # Discover every call, including future task owners, rather than maintaining
    # a second inventory that could silently miss a new launch path.
    for path in sorted(Path(runs.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "browser_turn"
            ):
                yield pytest.param(path, node, id=f"{path.stem}:{node.lineno}")


@pytest.mark.parametrize(("path", "call"), list(_browser_admission_calls()))
@pytest.mark.asyncio
async def test_task_browser_admission_receives_resolved_scope(monkeypatch, tmp_path, path, call):
    from rcp.providers.browser_grant import BrowserGrant
    from rcp.runs import browser_lifecycle

    scope = _scope()
    captured = []

    def acquire(**kwargs):
        captured.append(kwargs["hidden_read_scope"])
        return BrowserGrant()

    monkeypatch.setattr(browser_lifecycle, "acquire_turn_browser", acquire)
    request = SimpleNamespace()
    turn = SimpleNamespace(
        request=request,
        workspace=tmp_path,
        execution_host="",
        execution=None,
        remote_stage=None,
        hidden_read_scope=scope,
    )
    stage = SimpleNamespace(workspace=tmp_path, execution_host="", remote=None)
    # Execute the actual task's admission expression through browser_turn. The
    # task's unrelated staging/settlement machinery is outside this invariant.
    admission = eval(
        compile(ast.Expression(call), str(path), "eval"),
        {"browser_turn": browser_lifecycle.browser_turn},
        dict(
            request=request,
            turn=turn,
            stage=stage,
            workspace=tmp_path,
            execution_host="",
            execution=None,
            remote_stage=None,
            hidden_read_scope=scope,
        ),
    )
    async with admission:
        assert captured == [scope]
        assert captured[0] is scope
