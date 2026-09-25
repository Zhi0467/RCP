from __future__ import annotations

import subprocess
import sys
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.agents.launcher import work_like_launch_problem
from rcp.runs.auto_research_admission import _require_auto_research_retry_target_ready

_WORK_PROBE_REASON = "invalid --settings: unknown permission mode 'dontAsk'"


def _claude_binary(tmp_path: Path, *, authenticated: bool = True, work_ready: bool = False) -> Path:
    binary = tmp_path / "claude"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('2.1.266 (Claude Code)')\n"
        "elif args == ['auth', 'status']:\n"
        f"    print(json.dumps({{'loggedIn': {authenticated!r}}}))\n"
        "elif '--settings' in args:\n"
        "    settings = json.loads(args[args.index('--settings') + 1])\n"
        "    assert settings['sandbox'] == {'enabled': False}\n"
        "    assert settings['permissions']['defaultMode'] == 'dontAsk'\n"
        "    assert args[args.index('--input-format') + 1] == 'stream-json'\n"
        "    assert args[args.index('--permission-mode') + 1] == 'dontAsk'\n"
        "    assert sys.stdin.read() == ''\n"
        f"    with pathlib.Path({str(tmp_path / 'probes')!r}).open('a') as f: f.write('probe\\n')\n"
        f"    if not {work_ready!r}:\n"
        "        print('bash: no job control in this shell', file=sys.stderr)\n"
        f"        print({_WORK_PROBE_REASON!r}, file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "else:\n"
        "    command = json.loads(sys.stdin.readline())\n"
        "    print(json.dumps({'type':'result', 'result':'Discuss works',"
        "'user_message_uuids':[command['uuid']]}), flush=True)\n"
    )
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize("work_ready", [False, True])
def test_work_probe_is_empty_stdin_cached_and_refreshable(tmp_path: Path, work_ready: bool) -> None:
    binary = _claude_binary(tmp_path, work_ready=work_ready)
    launcher = AgentLauncher()

    readiness = launcher.readiness("claude", binary=str(binary))
    assert readiness.authenticated
    assert readiness.work_like_available is work_ready
    # A Work-only fault never becomes the general readiness reason: `reason` also
    # carries benign notes, so folding them together makes a usable Discuss
    # provider read as broken.
    assert readiness.reason is None
    if work_ready:
        assert readiness.work_like_reason is None
    else:
        assert _WORK_PROBE_REASON in readiness.work_like_reason
    assert launcher.readiness("claude", binary=str(binary)) == readiness
    assert (tmp_path / "probes").read_text().splitlines() == ["probe"]
    launcher.readiness("claude", binary=str(binary), refresh=True)
    assert (tmp_path / "probes").read_text().splitlines() == ["probe", "probe"]


def test_unauthenticated_provider_does_not_probe_work_readiness(tmp_path: Path) -> None:
    binary = _claude_binary(tmp_path, authenticated=False)
    readiness = AgentLauncher().readiness("claude", binary=str(binary))
    assert not readiness.authenticated
    assert readiness.work_like_available is None
    assert readiness.work_like_reason is None
    assert not (tmp_path / "probes").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["work_auto", "orchestrate"])
async def test_failed_work_readiness_refuses_before_launch_but_discuss_runs(
    tmp_path: Path, capability: str
) -> None:
    binary = _claude_binary(tmp_path)
    launcher = AgentLauncher()
    errors = [
        event
        async for event in launcher.stream(
            "claude", "Do work", binary=str(binary), cwd=tmp_path, capability=capability
        )
    ]
    assert len(errors) == 1
    assert errors[0].event == "error"
    assert _WORK_PROBE_REASON in errors[0].text

    events = [
        event
        async for event in launcher.stream(
            "claude", "Discuss", binary=str(binary), cwd=tmp_path, capability="discuss"
        )
    ]
    assert [event.text for event in events if event.event == "answer"] == ["Discuss works"]
    assert not any(event.event == "error" for event in events)
    assert (tmp_path / "probes").read_text().splitlines() == ["probe"]


@pytest.mark.parametrize("host", ["", "compute.example"])
def test_probe_transport_failure_is_not_a_rejected_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    binary = _claude_binary(tmp_path)
    launcher = AgentLauncher()

    def probe(_host, command, **_):
        if "--input-format" in command:
            return subprocess.CompletedProcess(command, 255, "", "connection failed")
        if command[-2:] == ["auth", "status"]:
            return subprocess.CompletedProcess(command, 0, '{"loggedIn":true}', "")
        return subprocess.CompletedProcess(command, 0, "2.1.266", "")

    monkeypatch.setattr(launcher, "_probe", probe)
    readiness = launcher.readiness("claude", binary=str(binary), host=host)
    assert readiness.work_like_available is None
    assert _WORK_PROBE_REASON not in (readiness.work_like_reason or "")
    if host:
        # ssh's own 255: the host is gone, the saved path is not wrong.
        assert readiness.path_state == "unreachable" and readiness.link_lost
        assert "unreachable" in (readiness.reason or "")
    else:
        assert "connection failed" in readiness.work_like_reason


@pytest.mark.asyncio
async def test_first_provider_error_contains_stderr_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "failing-claude"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "sys.stdin.readline()\n"
        "print('bash: no job control in this shell', file=sys.stderr, flush=True)\n"
        f"print({_WORK_PROBE_REASON!r}, file=sys.stderr, flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'error_during_execution',"
        "'is_error':True,'result':None,'error':None,'message':None}),flush=True)\n"
        "time.sleep(30)\n"
    )
    binary.chmod(0o755)
    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda provider, **kwargs: ProviderReadiness(
            provider=provider, installed=True, authenticated=True, binary_path=str(binary)
        ),
    )
    # The task engine stops consuming on the first error. Exercise that exact edge.
    async with aclosing(
        launcher.stream("claude", "Run", cwd=tmp_path, capability="discuss")
    ) as stream:
        async for event in stream:
            if event.event == "error":
                assert _WORK_PROBE_REASON in event.text
                break
        else:
            pytest.fail("provider failure was not reported")


def test_remote_probe_closes_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {}

    def run(arguments, **kwargs):
        observed.update(kwargs)
        assert arguments[0] == "ssh"
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    AgentLauncher._probe("compute.example", ["claude", "--input-format", "stream-json"])
    assert observed["input"] == ""


def test_work_like_launch_problem_is_the_one_readiness_refusal() -> None:
    ready = ProviderReadiness(provider="claude", installed=True, authenticated=True)
    assert work_like_launch_problem(ready) is None
    unchecked = ready.model_copy(update={"work_like_available": None})
    assert work_like_launch_problem(unchecked) is None
    blocked = ready.model_copy(
        update={"work_like_available": False, "work_like_reason": _WORK_PROBE_REASON}
    )
    assert work_like_launch_problem(blocked) == _WORK_PROBE_REASON
    inconclusive = ready.model_copy(
        update={"work_like_available": None, "work_like_reason": "could not be checked"}
    )
    # A probe that could not run is still a refusal: Work must not start blind.
    assert work_like_launch_problem(inconclusive) == "could not be checked"


def test_auto_research_retry_is_refused_before_allocation_when_the_work_probe_failed() -> None:
    """Retry admission consults the same policy as launch instead of installed+authenticated."""
    blocked = ProviderReadiness(
        provider="claude",
        installed=True,
        authenticated=True,
        work_like_available=False,
        work_like_reason=_WORK_PROBE_REASON,
    )
    machine = SimpleNamespace(host="gpu.example", provider_paths={"claude": "/bin/claude"})
    service = SimpleNamespace(
        manifest=SimpleNamespace(machine_map={"remote-1": machine}),
        launcher=SimpleNamespace(readiness=lambda *_args, **_kwargs: blocked),
    )
    request = SimpleNamespace(provider="claude", run_on="remote-1")
    with pytest.raises(ValueError, match="unknown permission mode"):
        _require_auto_research_retry_target_ready(service, request)

    service.launcher = SimpleNamespace(
        readiness=lambda *_args, **_kwargs: blocked.model_copy(
            update={"work_like_available": True, "work_like_reason": None}
        )
    )
    _require_auto_research_retry_target_ready(service, request)


def test_durable_signed_out_overrides_cached_readiness_without_probe(tmp_path):
    from rcp.storage import AppStore

    store = AppStore(tmp_path / "login.sqlite3")
    launcher = AgentLauncher(login_state=store.provider_login_state)
    binary = _claude_binary(tmp_path, work_ready=True)
    assert launcher.readiness("claude", binary=str(binary)).authenticated
    store.mark_provider_login_failed(
        "claude", "", generation=0, detail="observed provider failure", source="turn"
    )
    readiness = launcher.readiness("claude", binary=str(binary))
    assert not readiness.authenticated
    assert (tmp_path / "probes").read_text().splitlines() == ["probe"]
    assert not launcher.cached_readiness("claude", binary=str(binary)).authenticated


@pytest.mark.parametrize("path", ["auth", "catalog", "work"])
@pytest.mark.parametrize(
    "diagnostic,blocked",
    [("refresh_token_reused", True), ("connection refused", False), ("unsupported model", False)],
)
def test_probe_failure_updates_account_only_for_provider_auth(
    tmp_path, monkeypatch, path, diagnostic, blocked
):
    from rcp.agents.provider_accounts import ProviderAccounts
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.providers import profile_for
    from rcp.runs.provider_sign_in import ProviderSignInRunner
    from rcp.storage import AppStore

    store = AppStore(tmp_path / "app.sqlite3")
    accounts = ProviderAccounts(store, ProviderCredentialStore(tmp_path / "providers"))
    launcher = AgentLauncher(accounts=accounts, readiness_snapshots=store)
    ProviderSignInRunner(store, launcher, accounts)
    profile = profile_for("codex")
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(profile, "auth_command", lambda binary: [binary, "auth"])
    monkeypatch.setattr(profile, "catalog_command", lambda binary: [binary, "catalog"])
    monkeypatch.setattr(profile, "work_like_probe_command", lambda binary: [binary, "work"])
    monkeypatch.setattr(profile, "is_authenticated", lambda result: result.returncode == 0)
    monkeypatch.setattr(profile, "models", lambda result: [])

    def probe(host, command, **kwargs):
        assert not launcher.credential_gate._lock_for("codex", "").acquire(False)
        return subprocess.CompletedProcess(
            command,
            1 if command[-1] == path else 0,
            "1.0" if command[-1] == "--version" else "OK",
            diagnostic if command[-1] == path else "",
        )

    monkeypatch.setattr(launcher, "_probe", probe)
    readiness = launcher.readiness("codex", binary=str(binary), refresh=True)
    assert (store.provider_login_state("codex", "").state == "signed_out") is blocked
    if blocked:
        assert not readiness.authenticated
        assert store.provider_readiness_snapshot("codex", "", str(binary)) is None
