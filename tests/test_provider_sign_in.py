"""Sign in, verify, and sign out an execution account from RCP, without ever showing a secret."""

from __future__ import annotations

import json
import logging
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher
from rcp.agents import launcher as launcher_module
from rcp.agents.provider_environment import CLAUDE_TOKEN_VARIABLE, ProviderCredentialStore
from rcp.runs import provider_sign_in
from rcp.runs.provider_sign_in import (
    ProviderLoginRefused,
    ProviderSignInRunner,
    reset_claude_logins_without_tokens,
)
from rcp.storage import AppStore

from .helpers import wait_until

TOKEN = "sk-ant-oat01-a-pasted-setup-token"
VERIFICATION_URL = "https://auth.example/device"
USER_CODE = "ABCD-EFGH"


def _fake_codex(tmp_path: Path) -> Path:
    """A `codex` whose device-code login waits for a marker file before it finishes."""

    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        f"root = pathlib.Path({str(tmp_path)!r})\n"
        "with (root / 'argv.jsonl').open('a') as f: f.write(json.dumps(args) + '\\n')\n"
        "if args == ['login', '--device-auth']:\n"
        "    if (root / 'deny').exists():\n"
        "        print('Error: device authorization was denied', flush=True)\n"
        "        sys.exit(1)\n"
        "    print('Follow these steps to sign in with ChatGPT using device code"
        " authorization:', flush=True)\n"
        "    print('1. Open this link in your browser and sign in', flush=True)\n"
        f"    print('   {VERIFICATION_URL}', flush=True)\n"
        "    print('2. Enter this one-time code after you are signed in"
        " (expires in 15 minutes)', flush=True)\n"
        f"    print('   {USER_CODE}', flush=True)\n"
        "    for _ in range(400):\n"
        "        if (root / 'signed-in').exists():\n"
        "            print('Successfully logged in', flush=True)\n"
        "            sys.exit(0)\n"
        "        time.sleep(0.05)\n"
        "    sys.exit(2)\n"
        "if args[:1] == ['exec']:\n"
        "    print('OK')\n"
        "elif args == ['logout']:\n"
        "    print('Successfully logged out')\n"
        "elif args == ['--version']:\n"
        "    print('codex-cli 0.154.0')\n"
    )
    binary.chmod(0o755)
    return binary


def _fake_claude(tmp_path: Path) -> Path:
    """A `claude` that answers the login probe only when the setup token is in its environment."""

    binary = tmp_path / "claude"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        f"root = pathlib.Path({str(tmp_path)!r})\n"
        "with (root / 'argv.jsonl').open('a') as f: f.write(json.dumps(args) + '\\n')\n"
        f"if os.environ.get({CLAUDE_TOKEN_VARIABLE!r}) != {TOKEN!r}:\n"
        "    print('Not logged in', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print('OK')\n"
    )
    binary.chmod(0o755)
    return binary


def _argv_log(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "argv.jsonl").read_text().splitlines()]


def _runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: Path) -> ProviderSignInRunner:
    monkeypatch.setattr(launcher_module, "_discover_local_provider", lambda _provider: str(binary))
    store = AppStore(tmp_path / "app.sqlite3")
    credentials = ProviderCredentialStore(tmp_path / "providers")
    launcher = AgentLauncher(
        login_state=store.provider_login_state,
        credentials=credentials,
        readiness_snapshots=store,
    )
    return ProviderSignInRunner(store, launcher, credentials)


def _status_when(runner: ProviderSignInRunner, login_id: str, ready):
    return wait_until(
        lambda: (
            status
            if (status := runner.sign_in_status(login_id)) is not None and ready(status)
            else None
        ),
        timeout=10,
    )


def test_codex_device_sign_in_shows_the_code_holds_the_gate_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    store = runner.store
    store.mark_provider_login_failed(
        "codex", "", generation=0, detail="refresh_token_reused", source="turn"
    )

    started = runner.start_codex_sign_in("", member_id="member")
    assert started.state == "pending" and started.started_by == "member"
    shown = _status_when(runner, started.login_id, lambda status: status.user_code is not None)
    assert shown.user_code == USER_CODE
    assert shown.verification_url == VERIFICATION_URL
    # One sign-in per account: a second member joins the running one.
    assert runner.start_codex_sign_in("", member_id="other").login_id == started.login_id
    assert runner.running_sign_in("codex", "") is not None
    # The login rewrites the credential file when it completes; no turn may
    # start on that file meanwhile.
    gate = runner.launcher.credential_gate._lock_for("codex", "")
    assert not gate.acquire(False), "the credential gate was free during the sign-in"
    assert store.provider_login_state("codex", "").state == "signed_out"

    (tmp_path / "signed-in").write_text("")
    done = _status_when(runner, started.login_id, lambda status: status.state != "pending")
    assert done.state == "succeeded", done.detail
    assert done.finished_at is not None and done.resumed is None
    state = store.provider_login_state("codex", "")
    assert state.state == "signed_in"
    assert state.generation == 1 and state.changed_by == "member" and state.source == "verify"
    argv = _argv_log(tmp_path)
    assert argv[0] == ["login", "--device-auth"]
    assert argv[1][:2] == ["exec", "--ignore-user-config"], "success was not proven by a request"
    assert wait_until(lambda: gate.acquire(False) or None), "the gate was never released"
    gate.release()
    assert runner.running_sign_in("codex", "") is None
    assert runner.record_resumed(started.login_id, {"recoveries": 1}).resumed == {"recoveries": 1}


def test_a_denied_device_sign_in_fails_with_the_provider_detail_and_stays_signed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    runner.store.mark_provider_login_failed(
        "codex", "", generation=0, detail="refresh_token_reused", source="turn"
    )
    (tmp_path / "deny").write_text("")
    started = runner.start_codex_sign_in("", member_id="member")
    failed = _status_when(runner, started.login_id, lambda status: status.state != "pending")
    assert failed.state == "failed"
    assert "denied" in (failed.detail or "")
    assert runner.store.provider_login_state("codex", "").state == "signed_out"
    assert _argv_log(tmp_path) == [["login", "--device-auth"]]
    # The account is free for another attempt.
    assert runner.start_codex_sign_in("", member_id="member").login_id != started.login_id


def test_claude_token_paste_stores_privately_verifies_and_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    runner = _runner(tmp_path, monkeypatch, _fake_claude(tmp_path))
    store = runner.store
    store.mark_provider_login_failed("claude", "", generation=0, detail="signed out", source="turn")

    with pytest.raises(ProviderLoginRefused) as refused:
        runner.save_claude_token("", "not a token", member_id="member")
    assert refused.value.status_code == 422
    assert runner.credentials.claude_token("") is None

    state = runner.save_claude_token("", TOKEN, member_id="member")
    assert state.state == "signed_in" and state.changed_by == "member"
    path = runner.credentials.claude_token_path("")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = runner.credentials.claude_token_record("")
    assert record is not None and record.verified_at is not None
    assert _argv_log(tmp_path)[-1][0] == "--print", "success was not proven by a request"

    # The token reached the provider only through its environment.
    for argv in _argv_log(tmp_path):
        assert TOKEN not in " ".join(argv)
    assert TOKEN not in caplog.text
    assert TOKEN not in state.model_dump_json()
    assert TOKEN not in (path.parent / "setup-token.json").read_text()
    with store.connection() as connection:
        for table in ("provider_login_states", "provider_readiness_snapshots"):
            for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                assert TOKEN not in json.dumps(list(row), default=str)


def test_remote_token_travels_on_stdin_of_the_placement_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_claude(tmp_path))
    host = "gpu.example"
    monkeypatch.setattr(runner, "provider_binary", lambda _provider, _host: ("claude", set()))
    transports: list[tuple[list[str], str]] = []

    def run(arguments, *, input, **_kwargs):
        transports.append((arguments, input))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(provider_sign_in.subprocess, "run", run)
    probes = []

    def probe(_host, command, *, environment, **_kwargs):
        probes.append((command, environment))
        return subprocess.CompletedProcess(command, 0, "OK", "")

    monkeypatch.setattr(runner.launcher, "_probe", probe)

    state = runner.save_claude_token(host, TOKEN, member_id="member")
    assert state.state == "signed_in" and state.host == host
    assert stat.S_IMODE(runner.credentials.claude_token_path(host).stat().st_mode) == 0o600
    [(arguments, fed)] = transports
    assert arguments[0] == "ssh" and arguments[-2] == host
    assert "umask 077" in arguments[-1] and TOKEN not in " ".join(arguments)
    assert fed == TOKEN + "\n"
    [(command, environment)] = probes
    assert command[1] == "--print"
    assert environment.remote_prefix is not None and TOKEN not in environment.remote_prefix


def test_sign_out_fences_launches_like_a_failed_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    store = runner.store
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")

    state = runner.sign_out("codex", "", member_id="other")
    assert state.state == "signed_out" and state.source == "sign_out"
    assert state.changed_by == "other" and state.generation == 2
    assert _argv_log(tmp_path) == [["logout"]]
    refusal = runner.launcher._login_refusal("codex", "")
    assert refusal is not None and "signed out" in refusal
    assert not runner.launcher.readiness("codex", binary=str(tmp_path / "codex")).authenticated

    credentials = runner.credentials
    credentials.store_claude_token("", TOKEN, member_id="member", now=store.now())
    monkeypatch.setattr(
        launcher_module, "_discover_local_provider", lambda _p: str(tmp_path / "claude")
    )
    claude = runner.sign_out("claude", "", member_id="member")
    assert claude.state == "signed_out"
    assert credentials.claude_token("") is None
    assert credentials.claude_token_record("") is None


def test_restore_resets_claude_logins_whose_token_did_not_come_back(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    credentials = ProviderCredentialStore(tmp_path / "providers")
    store.mark_provider_login_verified("claude", "", member_id="member", detail="verified")
    store.mark_provider_login_verified("claude", "gpu.example", member_id="member", detail="ok")
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")
    credentials.store_claude_token("gpu.example", TOKEN, member_id="member", now=store.now())

    reset = reset_claude_logins_without_tokens(store, credentials)

    assert [(state.provider, state.host) for state in reset] == [("claude", "")]
    local = store.provider_login_state("claude", "")
    assert local.state == "signed_out" and local.source == "restore" and local.generation == 2
    assert "setup token" in (local.detail or "")
    assert store.provider_login_state("claude", "gpu.example").state == "signed_in"
    assert store.provider_login_state("codex", "").state == "signed_in"
    assert reset_claude_logins_without_tokens(store, credentials) == []
