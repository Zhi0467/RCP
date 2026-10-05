from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.agents.hidden_read import HIDDEN_READ_ENV_DENY_LIST, staged_hidden_read_source
from rcp.agents.provider_environment import prepare_hidden_read_launch
from rcp.agents.write_scope import ProjectWriteScope
from rcp.core.models import HiddenReadKeyEvidence, HiddenReadScope, HiddenReadStatus
from rcp.providers import profile_for
from rcp.providers.browser_grant import BrowserGrant

CAPABILITIES = ("discuss", "work_auto", "orchestrate", "scratch_patch", "paper_readonly")


def _scope(home: str = "/home/research") -> HiddenReadScope:
    return HiddenReadScope(
        execution_machine="local",
        execution_host="",
        os_account="research",
        account_home=home,
        hidden_directories=("/secret/folder",),
        hidden_files=("/secret/file",),
        hidden_globs=("/secret/db*",),
        env_deny_list=HIDDEN_READ_ENV_DENY_LIST,
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
    assert settings["env"]["CLAUDE_CODE_SHELL_PREFIX"] == scope.wrapper_path()
    wrapper_root = str(Path(scope.wrapper_path()).parent)
    assert f"Edit(/{wrapper_root}/**)" in settings["permissions"]["deny"]


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
        "env_deny_list",
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

    remote_evidence = (
        HiddenReadKeyEvidence(
            path="/home/research/.ssh/id_ed25519",
            kind="ssh_identity",
            public_key_fingerprint="SHA256:" + "A" * 43,
            agent_confirmed=True,
            visibility="hidden",
        ),
    )

    def confirm_remote(*, remote_stage, home):
        order.append({"remote": home})
        return remote_evidence

    def resolve(**kwargs):
        assert len(order) == (1 if remote else 2)
        if remote:
            assert kwargs["key_evidence"] == remote_evidence
        assert kwargs["machine_hidden_folders"] == ["/custom"]
        if failed:
            raise OSError("host unavailable")
        return _scope(str(tmp_path / "home"))

    monkeypatch.setattr(ssh_agent, "confirm_key_evidence", confirm)
    monkeypatch.setattr(ssh_agent, "confirm_remote_key_evidence", confirm_remote)
    monkeypatch.setattr(hidden_read, "resolve_hidden_read_scope", resolve)

    class Stage:
        def __init__(self):
            self.installed = {}

        def _ssh(self, command):
            output = "/home/research\n" if command[0] == "printenv" else ""
            return subprocess.CompletedProcess(command, 0, output, "")

        def _ssh_bytes(self, command, *, input_data, timeout_seconds):
            assert command[:2] == ["python3", "-c"] and command[3] == "--install"
            self.installed[command[4]] = json.loads(input_data)
            return subprocess.CompletedProcess(command, 0, b"", b"")

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
    assert order == (
        [{"remote": "/home/research"}]
        if remote
        else [
            {**order[0], "agent_socket": "/user/socket"},
            {**order[1], "agent_socket": "/deploy/socket"},
        ]
    )
    if failed:
        assert scope.enforcement.status == "unhidden"
        assert scope.enforcement.reasons
        return
    wrapper = Path(scope.wrapper_path())
    # Staged outside the writable workspace, inside a folder the scope hides.
    assert not wrapper.is_relative_to(tmp_path / "workspace")
    if remote:
        files = stage.installed[str(wrapper.parent)]
    else:
        files = {path.name: path.read_text() for path in wrapper.parent.iterdir()}
        assert all(path.stat().st_mode & 0o222 == 0 for path in wrapper.parent.iterdir())
        # OpenCode executes the wrapper directly as its SHELL.
        assert os.access(wrapper, os.X_OK)
    assert HiddenReadScope.model_validate_json(files[wrapper.name + ".policy.json"]) == scope
    assert files[wrapper.name] == staged_hidden_read_source()
    if provider == "opencode":
        assert "provider_native_tools_uncovered" in scope.enforcement.reasons


def test_claude_prefix_runs_as_claude_invokes_it(tmp_path):
    from rcp.agents.staged_hidden_read import install

    scope = _scope(str(tmp_path / "home"))
    wrapper = Path(scope.wrapper_path())
    install(
        str(wrapper.parent),
        {
            wrapper.name: staged_hidden_read_source(),
            wrapper.name + ".policy.json": scope.model_dump_json(),
        },
    )
    command = AgentLauncher._command(
        "claude",
        "task",
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[],
        capability="discuss",
        hidden_read_scope=scope,
        provider_version="2.1.288",
    )
    settings = json.loads(command[command.index("--settings") + 1])
    # Claude runs the prefix as one executable path with the command as its argument.
    prefix = settings["env"]["CLAUDE_CODE_SHELL_PREFIX"]
    assert subprocess.run([prefix, "exit 7"], capture_output=True).returncode == 7


def test_every_task_launch_resolves_or_carries_a_scope():
    from rcp import runs

    # A call with neither would silently launch with the empty unhidden scope.
    calls = [
        (path.stem, node.lineno, {keyword.arg for keyword in node.keywords})
        for path in sorted(Path(runs.__file__).parent.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_stream_agent_events"
    ]
    assert calls
    assert [call for call in calls if not {"service", "hidden_read_scope"} & call[2]] == []


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
