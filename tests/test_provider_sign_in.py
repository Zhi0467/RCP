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
from rcp.agents.provider_accounts import ProviderAccounts, reset_logins_without_credentials
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.provider_auth import CLAUDE_TOKEN_VARIABLE, CodexDeviceLogin
from rcp.runs import provider_sign_in
from rcp.runs.provider_sign_in import ProviderLoginRefused, ProviderSignInRunner
from rcp.storage import AppStore

from .helpers import wait_until

TOKEN = "sk-ant-oat01-a-pasted-setup-token"
VERIFICATION_URL = "https://auth.example/device"
USER_CODE = "ABCD-EFGH"


SCRIPT = """\
import json, pathlib, sys, threading, time
args = sys.argv[1:]
root = pathlib.Path(__ROOT__)
with (root / 'argv.jsonl').open('a') as f:
    f.write(json.dumps(args) + '\\n')


def send(value):
    sys.stdout.write(json.dumps(value) + '\\n')
    sys.stdout.flush()


def settle(login_id):
    for _ in range(400):
        if (root / 'signed-in').exists():
            send({'method': 'account/login/completed',
                  'params': {'loginId': login_id, 'success': True,
                             'error': None, 'onboardingEntrypoint': None}})
            return
        time.sleep(0.05)
    send({'method': 'account/login/completed',
          'params': {'loginId': login_id, 'success': False, 'error': 'timed out'}})


if args == ['app-server']:
    login_id = None
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get('method')
        if method == 'initialize':
            send({'id': message['id'],
                  'result': {'userAgent': 'fake/0 (test)', 'codexHome': str(root),
                             'platformFamily': 'unix', 'platformOs': 'macos'}})
        elif method == 'account/login/start':
            if (root / 'deny').exists():
                send({'id': message['id'],
                      'error': {'code': -32000, 'message': 'device authorization was denied'}})
                break
            login_id = 'login-1'
            send({'id': message['id'],
                  'result': {'type': 'chatgptDeviceCode', 'loginId': login_id,
                             'verificationUrl': __URL__, 'userCode': __CODE__}})
            threading.Thread(target=settle, args=(login_id,), daemon=True).start()
        elif method == 'account/login/cancel':
            send({'id': message['id'], 'result': {'status': 'canceled'}})
            send({'method': 'account/login/completed',
                  'params': {'loginId': login_id, 'success': False,
                             'error': 'Login was not completed'}})
    sys.exit(0)
if args[:1] == ['exec']:
    print('OK')
elif args == ['logout']:
    print('Successfully logged out')
elif args == ['--version']:
    print('codex-cli 0.154.0')
"""


def _fake_codex(tmp_path: Path) -> Path:
    """A `codex` whose app-server answers the frames the real binary answers.

    The frames mirror a transcript captured from codex-cli 0.154.0: the code and
    link arrive as `account/login/start` result fields and the outcome as the
    `success` flag on `account/login/completed`. Console prose is deliberately
    absent, so any dependence on provider wording fails here.
    """

    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        + SCRIPT.replace("__ROOT__", repr(str(tmp_path)))
        .replace("__URL__", repr(VERIFICATION_URL))
        .replace("__CODE__", repr(USER_CODE))
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
    accounts = ProviderAccounts(store, credentials)
    launcher = AgentLauncher(accounts=accounts, readiness_snapshots=store)
    return ProviderSignInRunner(store, launcher, accounts)


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

    started = runner.start_sign_in("codex", "", member_id="member")
    assert started.state == "pending" and started.started_by == "member"
    shown = _status_when(runner, started.login_id, lambda status: status.user_code is not None)
    assert shown.user_code == USER_CODE
    assert shown.verification_url == VERIFICATION_URL
    # One sign-in per account: a second member joins the running one.
    assert runner.start_sign_in("codex", "", member_id="other").login_id == started.login_id
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
    assert state.generation == 2 and state.changed_by == "member" and state.source == "verify"
    argv = _argv_log(tmp_path)
    assert argv[0] == ["app-server"]
    assert argv[1][:2] == ["exec", "--ignore-user-config"], "success was not proven by a request"
    assert wait_until(lambda: gate.acquire(False) or None), "the gate was never released"
    gate.release()
    assert runner.running_sign_in("codex", "") is None


def test_a_denied_device_sign_in_fails_with_the_provider_detail_and_stays_signed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    runner.store.mark_provider_login_verified(
        "codex", "", member_id="member", detail="Previously verified."
    )
    (tmp_path / "deny").write_text("")
    started = runner.start_sign_in("codex", "", member_id="member")
    failed = _status_when(runner, started.login_id, lambda status: status.state != "pending")
    assert failed.state == "failed"
    assert "denied" in (failed.detail or "")
    assert runner.store.provider_login_state("codex", "").state == "signed_out"
    assert _argv_log(tmp_path) == [["app-server"]]
    # The account is free for another attempt.
    assert runner.start_sign_in("codex", "", member_id="member").login_id != started.login_id


def test_claude_token_paste_stores_privately_verifies_and_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    runner = _runner(tmp_path, monkeypatch, _fake_claude(tmp_path))
    store = runner.store
    store.mark_provider_login_failed("claude", "", generation=0, detail="signed out", source="turn")

    with pytest.raises(ProviderLoginRefused) as refused:
        runner.save_token("claude", "", "not a token", member_id="member")
    assert refused.value.status_code == 422
    assert runner.credentials.token("claude", "") is None

    state = runner.save_token("claude", "", TOKEN, member_id="member")
    assert state.state == "signed_in" and state.changed_by == "member"
    path = runner.credentials.token_path("claude", "")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = runner.credentials.token_record("claude", "")
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

    state = runner.save_token("claude", host, TOKEN, member_id="member")
    assert state.state == "signed_in" and state.host == host
    assert stat.S_IMODE(runner.credentials.token_path("claude", host).stat().st_mode) == 0o600
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
    credentials.store_token("claude", "", TOKEN, member_id="member", now=store.now())
    monkeypatch.setattr(
        launcher_module, "_discover_local_provider", lambda _p: str(tmp_path / "claude")
    )
    claude = runner.sign_out("claude", "", member_id="member")
    assert claude.state == "signed_out"
    assert credentials.token("claude", "") is None
    assert credentials.token_record("claude", "") is None


def test_restore_resets_claude_logins_whose_token_did_not_come_back(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    credentials = ProviderCredentialStore(tmp_path / "providers")
    store.mark_provider_login_verified("claude", "", member_id="member", detail="verified")
    store.mark_provider_login_verified("claude", "gpu.example", member_id="member", detail="ok")
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")
    credentials.store_token("claude", "gpu.example", TOKEN, member_id="member", now=store.now())

    reset = reset_logins_without_credentials(store, credentials)

    assert [(state.provider, state.host) for state in reset] == [("claude", "")]
    local = store.provider_login_state("claude", "")
    assert local.state == "signed_out" and local.source == "restore" and local.generation == 2
    assert "setup token" in (local.detail or "")
    assert store.provider_login_state("claude", "gpu.example").state == "signed_in"
    assert store.provider_login_state("codex", "").state == "signed_in"
    assert reset_logins_without_credentials(store, credentials) == []


def test_third_provider_device_sign_in_completes_recovery_without_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration plus an existing interaction needs no shared provider-name branch."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rcp.providers import PROVIDERS, CodexProfile

    class TestProvider(CodexProfile):
        id = "test-device"
        label = "Test device provider"

    monkeypatch.setitem(PROVIDERS, TestProvider.id, TestProvider())
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    resumed = Event()
    calls = []

    def recover(provider, host):
        calls.append((provider, host))
        resumed.set()
        return {"checked": 1}

    runner.resume_account = recover
    with ThreadPoolExecutor(max_workers=8) as pool:
        starts = list(
            pool.map(
                lambda _: runner.start_sign_in(TestProvider.id, "", member_id="member"), range(8)
            )
        )
    assert len({s.login_id for s in starts}) == 1
    (tmp_path / "signed-in").write_text("")
    # No status read or HTTP GET drives completion.
    assert resumed.wait(10)
    assert runner.store.provider_login_state(TestProvider.id, "").state == "signed_in"
    runner.reconcile_recovery()
    assert calls == [(TestProvider.id, "")]
    assert len([argv for argv in _argv_log(tmp_path) if argv == ["app-server"]]) == 1


def test_verified_generation_replays_recovery_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    calls = []

    def interrupted(_provider, _host):
        raise RuntimeError("simulated interruption before recovery")

    runner.resume_account = interrupted
    state = runner.verify("codex", "", member_id="member")
    assert state.state == "signed_in"
    restarted = ProviderSignInRunner(runner.store, runner.launcher, runner.accounts)
    restarted.resume_account = lambda provider, host: (
        calls.append((provider, host)) or {"checked": 1}
    )
    restarted.reconcile_recovery()
    restarted.reconcile_recovery()
    assert calls == [("codex", "")]


def test_token_replacement_and_sign_out_wait_for_verification_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runner = _runner(tmp_path, monkeypatch, _fake_claude(tmp_path))
    entered, release = Event(), Event()
    observed = []

    def probe(_host, command, *, environment, **_kwargs):
        observed.append(environment.local_env[CLAUDE_TOKEN_VARIABLE])
        entered.set()
        assert release.wait(10)
        return subprocess.CompletedProcess(command, 0, "OK", "")

    monkeypatch.setattr(runner.launcher, "_probe", probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(runner.save_token, "claude", "", TOKEN, member_id="member")
        assert entered.wait(10)
        signed_out = pool.submit(runner.sign_out, "claude", "", member_id="other")
        assert not signed_out.done()
        assert runner.credentials.token("claude", "") == TOKEN
        release.set()
        verified = saved.result(10)
        final = signed_out.result(10)
    assert observed == [TOKEN]
    assert verified.state == "signed_in"
    assert final.state == "signed_out" and final.generation > verified.generation
    assert runner.credentials.token("claude", "") is None
    assert runner.store.provider_login_state("claude", "").state == "signed_out"


def test_failed_replacement_cannot_retain_old_verified_state_or_leak_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_claude(tmp_path))
    original = runner.save_token("claude", "", TOKEN, member_id="member")
    monkeypatch.setattr(
        runner.launcher,
        "_probe",
        lambda _host, command, **_: subprocess.CompletedProcess(
            command, 1, "", "server echoed secret-replacement-token"
        ),
    )
    with pytest.raises(ProviderLoginRefused) as refusal:
        runner.save_token("claude", "", "secret-replacement-token", member_id="member")
    state = runner.store.provider_login_state("claude", "")
    assert state.state == "signed_out" and state.generation > original.generation
    assert "secret-replacement-token" not in str(refusal.value)
    assert "secret-replacement-token" not in state.model_dump_json()


def test_late_failure_does_not_invalidate_repaired_account_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    old = runner.store.provider_login_state("codex", "")
    repaired = runner.verify("codex", "", member_id="member")
    invalidated = []
    monkeypatch.setattr(runner, "_forget_readiness", lambda *args: invalidated.append(args))
    assert runner.observe_failure(
        "codex", "", generation=old.generation, evidence="refresh_token_reused", source="turn"
    )
    assert runner.store.provider_login_state("codex", "") == repaired
    assert invalidated == []


def test_verification_waiting_for_token_replacement_reads_the_new_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runner = _runner(tmp_path, monkeypatch, _fake_claude(tmp_path))
    runner.credentials.store_token(
        "claude", "", "old-token", member_id="member", now=runner.store.now()
    )
    entered, release = Event(), Event()
    observed = []

    def probe(_host, command, *, environment, **_kwargs):
        observed.append(environment.local_env[CLAUDE_TOKEN_VARIABLE])
        if len(observed) == 1:
            entered.set()
            assert release.wait(10)
        return subprocess.CompletedProcess(command, 0, "OK", "")

    monkeypatch.setattr(runner.launcher, "_probe", probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replacement = pool.submit(runner.save_token, "claude", "", TOKEN, member_id="member")
        assert entered.wait(10)
        verify = pool.submit(runner.verify, "claude", "", member_id="other")
        assert not verify.done()
        release.set()
        saved = replacement.result(10)
        verified = verify.result(10)
    assert observed == [TOKEN, TOKEN]
    assert verified.generation > saved.generation
    assert runner.store.provider_login_state("claude", "") == verified


def test_verification_classifies_auth_error_even_when_process_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    monkeypatch.setattr(
        runner.launcher,
        "_probe",
        lambda _host, command, **_: subprocess.CompletedProcess(
            command, 0, '{"error":"refresh_token_reused"}', ""
        ),
    )
    with pytest.raises(ProviderLoginRefused, match="authentication failed"):
        runner.verify("codex", "", member_id="member")
    state = runner.store.provider_login_state("codex", "")
    assert state.state == "signed_out" and state.generation == 0


def test_credential_write_interruption_leaves_account_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_claude(tmp_path))
    old = runner.save_token("claude", "", TOKEN, member_id="member")
    with pytest.raises(ProviderLoginRefused):
        runner.save_token("claude", "", "invalid token", member_id="member")
    assert runner.store.provider_login_state("claude", "") == old
    write = runner.credentials.store_token

    def interrupted(*args, **kwargs):
        write(*args, **kwargs)
        raise OSError("simulated interrupted persistence")

    monkeypatch.setattr(runner.credentials, "store_token", interrupted)
    with pytest.raises(ProviderLoginRefused, match="persist"):
        runner.save_token("claude", "", "replacement-token", member_id="member")
    state = runner.store.provider_login_state("claude", "")
    assert state.state == "signed_out" and state.generation > old.generation
    assert runner.credentials.token("claude", "") == "replacement-token"


def test_registered_provider_without_sign_out_explicitly_refuses_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rcp.provider_auth import ProviderAuthentication
    from rcp.providers import PROVIDERS, CodexProfile

    class Unsupported(CodexProfile):
        id = "no-sign-out"
        authentication = ProviderAuthentication()

    monkeypatch.setitem(PROVIDERS, Unsupported.id, Unsupported())
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    with pytest.raises(ProviderLoginRefused) as refusal:
        runner.sign_out(Unsupported.id, "", member_id="member")
    assert refusal.value.status_code == 422
    assert runner.store.provider_login_states() == []


def test_device_credential_change_interrupted_before_verification_stays_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    previous = runner.store.mark_provider_login_verified(
        "codex", "", member_id="member", detail="Previously verified."
    )
    (tmp_path / "signed-in").write_text("")

    def interrupted(*_args):
        raise OSError("simulated interruption after native credential replacement")

    monkeypatch.setattr(runner, "_verify_locked", interrupted)
    started = runner.start_sign_in("codex", "", member_id="other")
    failed = _status_when(runner, started.login_id, lambda status: status.state != "pending")
    assert failed.state == "failed"
    state = runner.store.provider_login_state("codex", "")
    assert state.state == "signed_out" and state.generation > previous.generation
    assert state.changed_by == "other"
    assert runner.refusal("codex", "") is not None
    assert _argv_log(tmp_path) == [["app-server"]]


@pytest.mark.parametrize(
    ("provider", "output"),
    [
        ("codex", '{"error":{"message":"unsupported model"}}'),
        ("claude", '{"type":"result","is_error":true,"result":"unsupported model"}'),
    ],
)
def test_verification_rejects_zero_exit_provider_errors_without_revoking_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, output: str
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    if provider == "claude":
        runner.credentials.store_token(
            "claude", "", TOKEN, member_id="member", now=runner.store.now()
        )
    original = runner.store.mark_provider_login_verified(
        provider, "", member_id="member", detail="verified"
    )
    monkeypatch.setattr(
        runner.launcher,
        "_probe",
        lambda _host, command, **_: subprocess.CompletedProcess(command, 0, output, ""),
    )
    with pytest.raises(ProviderLoginRefused, match="could not complete"):
        runner.verify(provider, "", member_id="member")
    assert runner.store.provider_login_state(provider, "") == original


@pytest.mark.parametrize(
    ("provider", "output"),
    [
        ("codex", '{"error":{"message":"unsupported model"}}'),
        ("claude", '{"type":"result","is_error":true,"result":"unsupported model"}'),
    ],
)
def test_zero_exit_provider_errors_cannot_verify_replacement_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, output: str
) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    original = runner.store.mark_provider_login_verified(
        provider, "", member_id="member", detail="verified"
    )
    monkeypatch.setattr(
        runner.launcher,
        "_probe",
        lambda _host, command, **_: subprocess.CompletedProcess(command, 0, output, ""),
    )
    if provider == "claude":
        with pytest.raises(ProviderLoginRefused, match="could not complete"):
            runner.save_token(provider, "", TOKEN, member_id="member")
        assert runner.credentials.token_record(provider, "").verified_at is None
    else:
        (tmp_path / "signed-in").write_text("")
        started = runner.start_sign_in(provider, "", member_id="member")
        done = _status_when(runner, started.login_id, lambda status: status.state != "pending")
        assert done.state == "failed"
    state = runner.store.provider_login_state(provider, "")
    assert state.state == "signed_out" and state.generation > original.generation


def test_a_member_can_cancel_a_running_device_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling ends the attempt, stops the process, and says so on the account."""

    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    started = runner.start_sign_in("codex", "", member_id="member")
    _status_when(runner, started.login_id, lambda status: status.user_code is not None)

    runner.cancel_sign_in(started.login_id)

    ended = _status_when(runner, started.login_id, lambda status: status.state != "pending")
    assert ended.state == "failed"
    assert ended.detail == provider_sign_in.SIGN_IN_CANCELED_DETAIL
    state = runner.store.provider_login_state("codex", "")
    # The account must stop describing an attempt that is over.
    assert state.state == "signed_out"
    assert state.detail == provider_sign_in.SIGN_IN_CANCELED_DETAIL
    assert runner.running_sign_in("codex", "") is None


def test_the_device_code_is_read_from_protocol_fields_not_console_prose() -> None:
    """Codex may reword or restyle everything it prints; RCP reads only protocol fields.

    The prose lines below are the real console output of codex-cli 0.154.0,
    colour escapes included.
    """

    login = CodexDeviceLogin()
    prose = [
        "Follow these steps to sign in with ChatGPT using device code authorization:",
        "1. Open this link in your browser and sign in to your account",
        "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m",
        "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m",
        "   \x1b[94mE03Y-3H5MN\x1b[0m",
    ]
    for line in prose:
        step = login.receive_line(line)
        assert step.fields == {} and not step.finished

    started = login.receive_line(
        json.dumps(
            {
                "id": CodexDeviceLogin.START_ID,
                "result": {
                    "type": "chatgptDeviceCode",
                    "loginId": "login-1",
                    "verificationUrl": VERIFICATION_URL,
                    "userCode": USER_CODE,
                },
            }
        )
    )
    assert started.fields == {"user_code": USER_CODE, "verification_url": VERIFICATION_URL}

    # Only the protocol's own completion ends the wait, whatever it says.
    assert not login.receive_line("Successfully logged in").finished
    done = login.receive_line(
        json.dumps(
            {
                "method": "account/login/completed",
                "params": {"loginId": "login-1", "success": True, "error": None},
            }
        )
    )
    assert done.finished and done.failure is None
