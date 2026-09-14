"""Every RCP-started Claude process runs on the stored setup token, and nothing else."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher
from rcp.agents.provider_environment import (
    CLAUDE_CONFLICTING_VARIABLES,
    CLAUDE_TOKEN_VARIABLE,
    ProviderCredentialStore,
    remote_claude_token_placement_command,
    remote_claude_token_prefix,
)
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.storage import AppStore

TOKEN = "sk-ant-oat01-test-token-value"


def _fake_claude(tmp_path: Path) -> Path:
    """A `claude` that records the environment of every start and answers each probe."""

    binary = tmp_path / "claude"
    log = tmp_path / "starts.jsonl"
    version_file = tmp_path / "version.txt"
    version_file.write_text("2.1.270 (Claude Code)\n")
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "watched = ['CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN']\n"
        "record = {'argv': args, 'env': {k: os.environ[k] for k in watched if k in os.environ}}\n"
        f"with pathlib.Path({str(log)!r}).open('a') as f: f.write(json.dumps(record) + '\\n')\n"
        "if args == ['--version']:\n"
        f"    print(pathlib.Path({str(version_file)!r}).read_text().strip())\n"
        "elif args == ['auth', 'status']:\n"
        "    print(json.dumps({'loggedIn': True}))\n"
        "elif args == ['--help']:\n"
        "    print('usage')\n"
        "elif '/context' in args:\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'skills': ['review']}))\n"
        "elif 'Reply with OK only.' in args:\n"
        "    print('OK')\n"
        "elif '--settings' in args:\n"
        "    assert sys.stdin.read() == ''\n"
        "else:\n"
        "    command = json.loads(sys.stdin.readline())\n"
        "    print(json.dumps({'type': 'result', 'result': 'Discuss works',"
        " 'user_message_uuids': [command['uuid']]}), flush=True)\n"
    )
    binary.chmod(0o755)
    return binary


def _starts(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "starts.jsonl").read_text().splitlines()]


def _assert_token_only(starts: list[dict]) -> None:
    assert starts, "the fake provider never started"
    for start in starts:
        assert start["env"].get(CLAUDE_TOKEN_VARIABLE) == TOKEN, start["argv"]
        for name in CLAUDE_CONFLICTING_VARIABLES:
            assert name not in start["env"], start["argv"]


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProviderCredentialStore:
    for name in CLAUDE_CONFLICTING_VARIABLES:
        monkeypatch.setenv(name, "a-member-shell-value")
    store = ProviderCredentialStore(tmp_path / "providers")
    store.store_claude_token("", TOKEN, member_id="member", now="2026-09-14T10:00:00+00:00")
    return store


def test_token_is_stored_privately_and_the_record_is_secret_free(
    credentials: ProviderCredentialStore,
) -> None:
    path = credentials.claude_token_path("")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert credentials.claude_token("") == TOKEN
    record = credentials.claude_token_record("")
    assert record is not None and record.pasted_by == "member" and record.verified_at is None
    assert TOKEN not in (path.parent / "setup-token.json").read_text()
    with pytest.raises(ValueError):
        credentials.store_claude_token("", "two words", member_id="member", now="now")
    with pytest.raises(ValueError):
        credentials.store_claude_token("", "x" * 5000, member_id="member", now="now")


def test_local_environment_carries_the_token_and_drops_conflicting_variables(
    credentials: ProviderCredentialStore,
) -> None:
    environment = credentials.process_environment("claude", "")
    assert environment.local_env is not None
    assert environment.local_env[CLAUDE_TOKEN_VARIABLE] == TOKEN
    assert not set(CLAUDE_CONFLICTING_VARIABLES) & set(environment.local_env)
    assert environment.remote_prefix is None
    # Codex has no RCP-managed credential; it inherits the service environment.
    assert credentials.process_environment("codex", "").local_env is None

    credentials.delete_claude_token("")
    without = credentials.process_environment("claude", "").local_env
    assert without is not None
    assert CLAUDE_TOKEN_VARIABLE not in without
    assert not set(CLAUDE_CONFLICTING_VARIABLES) & set(without)


def test_remote_prefix_reads_the_account_file_and_never_carries_the_token(
    credentials: ProviderCredentialStore,
) -> None:
    host = "gpu.example"
    assert credentials.process_environment("claude", host).remote_prefix is None
    credentials.store_claude_token(host, TOKEN, member_id="member", now="now")
    prefix = credentials.process_environment("claude", host).remote_prefix
    assert prefix == remote_claude_token_prefix()
    assert TOKEN not in prefix
    assert "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN" in prefix
    assert (
        'export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$HOME"/.config/rcp/claude-setup-token)"' in prefix
    )
    placement = remote_claude_token_placement_command()
    assert placement.startswith("umask 077 && mkdir -p ")
    assert placement.endswith('cat > "$HOME"/.config/rcp/claude-setup-token')
    assert TOKEN not in placement


@pytest.mark.asyncio
async def test_every_local_claude_start_receives_the_stored_token(
    tmp_path: Path, credentials: ProviderCredentialStore
) -> None:
    binary = _fake_claude(tmp_path)
    store = AppStore(tmp_path / "app.sqlite3")
    launcher = AgentLauncher(credentials=credentials, readiness_snapshots=store)

    readiness = launcher.readiness("claude", binary=str(binary))
    assert readiness.authenticated and readiness.work_like_available

    skills = ProviderSkillInventoryManager(
        store,
        credential_gate=launcher.credential_gate,
        process_environment=launcher.process_environment,
    )
    skills.mark_refreshing("claude", "", str(binary))
    inventory = skills.refresh("claude", "", str(binary), readiness)
    assert [skill.name for skill in inventory.skills] == ["review"]

    events = [
        event
        async for event in launcher.stream(
            "claude", "Discuss", binary=str(binary), cwd=tmp_path, capability="discuss"
        )
    ]
    assert [event.text for event in events if event.event == "answer"] == ["Discuss works"]

    starts = _starts(tmp_path)
    argvs = [start["argv"] for start in starts]
    assert ["--version"] in argvs and ["auth", "status"] in argvs
    assert any("/context" in argv for argv in argvs), "the skill probe never ran"
    # The prompt travels on stdin; the turn is the stream-json start that is
    # neither the Work probe (`--settings`) nor the skill probe.
    assert any(
        "--input-format" in argv and "--settings" not in argv and "/context" not in argv
        for argv in argvs
    ), "the turn never ran"
    _assert_token_only(starts)


def test_readiness_reuses_the_stored_answer_until_the_version_changes(
    tmp_path: Path, credentials: ProviderCredentialStore
) -> None:
    binary = _fake_claude(tmp_path)
    store = AppStore(tmp_path / "app.sqlite3")
    first = AgentLauncher(credentials=credentials, readiness_snapshots=store).readiness(
        "claude", binary=str(binary)
    )
    probed = [start["argv"] for start in _starts(tmp_path)]
    assert ["auth", "status"] in probed
    (tmp_path / "starts.jsonl").unlink()

    # A new process has no memory; the durable snapshot answers for it.
    fresh = AgentLauncher(credentials=credentials, readiness_snapshots=store)
    reused = fresh.readiness("claude", binary=str(binary))
    assert reused == first
    assert [start["argv"] for start in _starts(tmp_path)] == [["--version"]]
    (tmp_path / "starts.jsonl").unlink()

    # An explicit Refresh is the human asking for a real probe.
    fresh.readiness("claude", binary=str(binary), refresh=True)
    assert ["auth", "status"] in [start["argv"] for start in _starts(tmp_path)]
    (tmp_path / "starts.jsonl").unlink()

    # An upgraded executable is probed again, and the snapshot follows it.
    (tmp_path / "version.txt").write_text("2.1.271 (Claude Code)\n")
    upgraded = AgentLauncher(credentials=credentials, readiness_snapshots=store).readiness(
        "claude", binary=str(binary)
    )
    assert upgraded.version == "2.1.271 (Claude Code)"
    assert ["auth", "status"] in [start["argv"] for start in _starts(tmp_path)]
    snapshot = store.provider_readiness_snapshot("claude", "", str(binary))
    assert snapshot is not None and snapshot.version == "2.1.271 (Claude Code)"
    assert TOKEN not in snapshot.readiness_json


@pytest.mark.asyncio
async def test_before_start_runs_once_under_the_credential_gate(
    tmp_path: Path, credentials: ProviderCredentialStore
) -> None:
    """The login generation a turn runs under is read while nothing can change it."""

    binary = _fake_claude(tmp_path)
    launcher = AgentLauncher(credentials=credentials)
    lock = launcher.credential_gate._lock_for("claude", "")
    observed: list[bool] = []

    async def before_start() -> None:
        observed.append(not lock.acquire(False))

    events = [
        event
        async for event in launcher.stream(
            "claude",
            "Discuss",
            binary=str(binary),
            cwd=tmp_path,
            capability="discuss",
            before_start=before_start,
        )
    ]
    assert [event.text for event in events if event.event == "answer"] == ["Discuss works"]
    assert observed == [True], "before_start did not run exactly once inside the hold"
