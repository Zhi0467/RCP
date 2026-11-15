from __future__ import annotations

import json
import os
import stat
import threading
from datetime import UTC, datetime, timedelta

import pytest

from rcp.providers.claude.remote import claude_project_directory
from rcp.runs.provider_session_sweep import sweep_provider_sessions
from rcp.storage import AppStore
from tests.test_provider_session_sweeps import _insert

OLD = (datetime.now(UTC) - timedelta(days=31)).isoformat()
RCP_CODEX = "019a0000-0000-7000-8000-00000000000a"
GONE_CODEX = "019a0000-0000-7000-8000-00000000000b"
FORKED_CODEX = "019a0000-0000-7000-8000-00000000000c"
HUMAN_CODEX = "019a0000-0000-7000-8000-00000000000d"
RCP_OPENCODE = "ses_rcpOld1"
GONE_OPENCODE = "ses_rcpGone2"
RCP_CLAUDE = "019a0000-0000-7000-8000-00000000000e"
OTHER_CLAUDE = "019a0000-0000-7000-8000-00000000000f"

FAKE = """#!/usr/bin/env python3
import json, os, re, sys
if sys.argv[1] == "--version":
    print(os.environ.get("FAKE_OPENCODE_VERSION", "1.0.150"))
    sys.exit(0)
if sys.argv[1] == "db":
    rows = json.loads(os.environ.get("FAKE_OPENCODE_ROWS", "{{}}"))
    session = re.search("id = '([^']+)'", sys.argv[2]).group(1)
    print(json.dumps(rows.get(session, []), indent=2))
    sys.exit(0)
with open(os.environ["FAKE_ARGV_LOG"], "a") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
session = sys.argv[-1]
home = os.environ["FAKE_PROVIDER_HOME"]
if os.path.basename(sys.argv[0]) == "opencode":
    if session == "{gone_opencode}":
        print("Error: Session not found: " + session, file=sys.stderr)
        sys.exit(1)
    os.unlink(os.path.join(home, session))
    sys.exit(0)
if session in ("{gone_codex}", "{forked_codex}"):
    if "--force" in sys.argv:
        print("Error: failed to delete session", file=sys.stderr)
    elif session == "{gone_codex}":
        print("Error: No active or archived session found matching '" + session + "'.", file=sys.stderr)
    else:
        print("Error: cannot confirm session deletion without an interactive terminal", file=sys.stderr)
    sys.exit(1)
os.unlink(os.path.join(home, session))
print("Deleted session " + session + ".")
"""


def _run(
    store,
    operation_id,
    session,
    provider,
    stage_root="/stage",
    kind="project_chat",
    machine="backend",
):
    _insert(
        store,
        "graph_runs",
        operation_id=operation_id,
        project_id="p",
        kind=kind,
        status="succeeded",
        request_json=json.dumps({"provider": provider, "chat_id": operation_id}),
        created_at=OLD,
        updated_at=OLD,
        status_message="",
        native_session_id=session,
        stage_machine_id=machine,
        stage_root=stage_root,
    )


def _sweep(store, stop=None):
    sweep_provider_sessions(store, stop=stop or threading.Event())


@pytest.fixture
def fake_providers(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    source = FAKE.format(
        gone_opencode=GONE_OPENCODE, gone_codex=GONE_CODEX, forked_codex=FORKED_CODEX
    )
    for name in ("codex", "opencode"):
        path = bin_dir / name
        path.write_text(source)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "argv.jsonl"
    log.touch()
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
    home = tmp_path / "provider-home"
    home.mkdir()
    for session in (RCP_CODEX, HUMAN_CODEX, RCP_OPENCODE):
        (home / session).write_text("transcript")
    monkeypatch.setenv("FAKE_PROVIDER_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return log


@pytest.mark.real_provider_discovery
def test_a_pass_deletes_only_recorded_sessions_and_keeps_failures_marked(tmp_path, fake_providers):
    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "codex-turn", RCP_CODEX, "codex")
    _run(store, "codex-gone", GONE_CODEX, "codex")
    _run(store, "codex-forked", FORKED_CODEX, "codex")
    _run(store, "opencode-turn", RCP_OPENCODE, "opencode")
    _run(store, "opencode-gone", GONE_OPENCODE, "opencode")
    _run(store, "agy-turn", "agy-session", "agy")
    _sweep(store)
    calls = [json.loads(line) for line in fake_providers.read_text().splitlines()]
    named = {call[-1] for call in calls}
    assert HUMAN_CODEX not in named
    assert named == {RCP_CODEX, GONE_CODEX, FORKED_CODEX, RCP_OPENCODE, GONE_OPENCODE}
    assert ["codex", "delete", "--force", RCP_CODEX] in calls
    assert ["opencode", "session", "delete", RCP_OPENCODE] in calls
    with store.connection() as connection:
        rows = {
            row["native_session_id"]: (row["deleted_at"] is not None, row["last_error"])
            for row in connection.execute("SELECT * FROM provider_session_sweeps")
        }
    assert {session: deleted for session, (deleted, _error) in rows.items()} == {
        RCP_CODEX: True,
        GONE_CODEX: True,
        FORKED_CODEX: False,
        RCP_OPENCODE: True,
        GONE_OPENCODE: True,
    }
    assert rows[FORKED_CODEX][1]
    assert sorted(path.name for path in (tmp_path / "provider-home").iterdir()) == [HUMAN_CODEX]
    fake_providers.write_text("")
    _sweep(store)
    retried = {json.loads(line)[-1] for line in fake_providers.read_text().splitlines()}
    assert retried == {FORKED_CODEX}


@pytest.mark.real_provider_discovery
def test_sessions_used_outside_rcp_are_kept_unmarked_and_stay_eligible(
    tmp_path, monkeypatch, fake_providers, caplog
):
    import logging

    stale = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    rollouts = tmp_path / "codex-home" / "sessions" / "2026" / "11" / "01"
    rollouts.mkdir(parents=True)
    (rollouts / f"rollout-2026-11-01T00-00-00-{RCP_CODEX}.jsonl").write_text("{}")
    old_rollout = rollouts / f"rollout-2026-10-01T00-00-00-{HUMAN_CODEX}.jsonl"
    old_rollout.write_text("{}")
    os.utime(old_rollout, (stale, stale))
    monkeypatch.setenv(
        "FAKE_OPENCODE_ROWS",
        json.dumps(
            {
                RCP_OPENCODE: [
                    {"time_updated": int(datetime.now(UTC).timestamp() * 1000), "children": 0}
                ],
                GONE_OPENCODE: [{"time_updated": int(stale * 1000), "children": 1}],
            }
        ),
    )
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    stage = tmp_path / "stage"
    stage.mkdir()
    project = config / "projects" / claude_project_directory(stage.resolve())
    project.mkdir(parents=True)
    (project / f"{RCP_CLAUDE}.jsonl").write_text("{}")
    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "codex-resumed", RCP_CODEX, "codex")
    _run(store, "codex-idle", HUMAN_CODEX, "codex")
    _run(store, "opencode-resumed", RCP_OPENCODE, "opencode")
    _run(store, "opencode-parent", GONE_OPENCODE, "opencode")
    _run(store, "claude-resumed", RCP_CLAUDE, "claude", stage_root=str(stage))
    with caplog.at_level(logging.INFO):
        _sweep(store)
    deleted = [json.loads(line)[-1] for line in fake_providers.read_text().splitlines()]
    assert deleted == [HUMAN_CODEX]
    assert (project / f"{RCP_CLAUDE}.jsonl").exists()
    assert "in use outside RCP claude 1, codex 1, opencode 2" in caplog.text
    in_use = {RCP_CODEX, RCP_OPENCODE, GONE_OPENCODE, RCP_CLAUDE}
    assert not any(store.provider_session_swept(session) for session in in_use)
    assert {
        target.session_id
        for target in store.provider_session_sweep_targets(store.now())
        if not target.marked
    } == in_use


@pytest.mark.real_provider_discovery
def test_an_unverified_opencode_store_version_is_never_deleted(
    tmp_path, monkeypatch, fake_providers
):
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", "2.0.0")
    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "opencode-turn", RCP_OPENCODE, "opencode")
    _sweep(store)
    assert fake_providers.read_text() == ""
    assert (tmp_path / "provider-home" / RCP_OPENCODE).exists()
    with store.connection() as connection:
        row = connection.execute("SELECT * FROM provider_session_sweeps").fetchone()
    assert row["deleted_at"] is None
    assert row["last_error"]


def test_claude_deletes_the_exact_transcript_and_its_folder(tmp_path, monkeypatch):
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    stage = tmp_path / "stage"
    stage.mkdir()
    project = config / "projects" / claude_project_directory(stage.resolve())
    (project / RCP_CLAUDE / "subagents").mkdir(parents=True)
    (project / RCP_CLAUDE / "subagents" / "agent.jsonl").write_text("{}")
    (project / f"{RCP_CLAUDE}.jsonl").write_text("{}")
    stale = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(project / f"{RCP_CLAUDE}.jsonl", (stale, stale))
    (project / f"{OTHER_CLAUDE}.jsonl").write_text("{}")
    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "claude-turn", RCP_CLAUDE, "claude", stage_root=str(stage))
    _run(store, "claude-coach", OTHER_CLAUDE, "claude", kind="paper_coach")
    _sweep(store)
    assert sorted(path.name for path in project.iterdir()) == [f"{OTHER_CLAUDE}.jsonl"]
    assert store.provider_session_swept(RCP_CLAUDE)
    assert not store.provider_session_swept(OTHER_CLAUDE)


def test_an_unreachable_machine_keeps_its_marks(tmp_path, monkeypatch):
    from rcp.runs import provider_session_sweep

    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "codex-turn", RCP_CODEX, "codex")

    def unreachable(*_args, **_kwargs):
        raise RuntimeError("ssh: connect to host: Connection refused")

    monkeypatch.setattr(provider_session_sweep, "_delete_on_machine", unreachable)
    _sweep(store)
    assert store.provider_session_swept(RCP_CODEX)
    pending = store.provider_session_sweep_targets(store.now())
    assert [(target.session_id, target.marked) for target in pending] == [(RCP_CODEX, True)]


def _codex(index):
    return f"019a0000-0000-7000-8000-0000000001{index:02d}"


def test_stop_ends_the_pass_between_chunks(tmp_path, monkeypatch):
    from rcp.runs import provider_session_sweep

    store = AppStore(tmp_path / "app.sqlite3")
    for index in range(3):
        _run(store, f"turn-{index}", _codex(index), "codex")
    stop = threading.Event()
    calls = []

    def delete(_machine, targets):
        calls.append([target.session_id for target in targets])
        stop.set()
        return [("deleted", None)] * len(targets)

    monkeypatch.setattr(provider_session_sweep, "PROVIDER_SESSION_SWEEP_CHUNK", 1)
    monkeypatch.setattr(provider_session_sweep, "_delete_on_machine", delete)
    _sweep(store, stop)
    assert len(calls) == 1
    pending = store.provider_session_sweep_targets(store.now())
    assert len([target for target in pending if target.marked]) == 2


def test_failing_retries_cannot_starve_fresh_sessions(tmp_path, monkeypatch):
    from rcp.runs import provider_session_sweep

    store = AppStore(tmp_path / "app.sqlite3")
    for index in range(3):
        _run(store, f"turn-{index}", _codex(index), "codex")
    monkeypatch.setattr(provider_session_sweep, "PROVIDER_SESSION_SWEEP_BATCH", 2)
    monkeypatch.setattr(provider_session_sweep, "PROVIDER_SESSION_SWEEP_RETRY_BATCH", 1)
    attempted = []

    def fail(_machine, targets):
        attempted.extend(target.session_id for target in targets)
        return [("failed", "refused")] * len(targets)

    monkeypatch.setattr(provider_session_sweep, "_delete_on_machine", fail)
    _sweep(store)
    attempted.clear()
    _sweep(store)
    assert _codex(2) in attempted
    assert len(attempted) == 2


def test_a_removed_machine_settles_its_marks_and_marks_nothing_new(tmp_path, caplog):
    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "marked", _codex(0), "codex", machine="removed")
    assert store.mark_provider_sessions_swept(
        store.provider_session_sweep_targets(store.now()), store.now()
    )
    _run(store, "fresh", _codex(1), "codex", machine="removed")
    _sweep(store)
    assert store.provider_session_sweep_targets(store.now()) == [
        target for target in store.provider_session_sweep_targets(store.now()) if not target.marked
    ]
    assert store.provider_session_swept(_codex(0))
    assert not store.provider_session_swept(_codex(1))
    assert "removed" in caplog.text


@pytest.mark.real_provider_discovery
def test_the_remote_program_runs_in_the_login_shell_and_resolves_the_workspace(
    tmp_path, monkeypatch, fake_providers
):
    import sys
    from types import SimpleNamespace

    from rcp.runs import provider_session_sweep
    from rcp.storage.provider_session_sweeps import ProviderSessionSweepTarget

    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").write_text(f'#!/bin/sh\nexec {sys.executable} -S "$@"\n')
    (shim / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    config = tmp_path / "login-claude"
    home = tmp_path / "remote-home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        f"echo 'welcome to the host'\nexport CLAUDE_CONFIG_DIR={config}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(
        provider_session_sweep, "ssh_arguments", lambda _host, command: ["bash", "-c", command]
    )
    stage = (tmp_path / "stage").resolve()
    stage.mkdir()
    (tmp_path / "linked").symlink_to(stage)
    project = config / "projects" / claude_project_directory(stage)
    project.mkdir(parents=True)
    (project / f"{RCP_CLAUDE}.jsonl").write_text("{}")
    stale = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(project / f"{RCP_CLAUDE}.jsonl", (stale, stale))

    def target(provider, session):
        return ProviderSessionSweepTarget(
            provider=provider,
            machine_id="worker",
            session_id=session,
            marked=True,
            cwd=str(tmp_path / "linked"),
        )

    outcomes = provider_session_sweep._delete_on_machine(
        SimpleNamespace(
            host="worker-host", provider_paths={"codex": str(tmp_path / "bin" / "codex")}
        ),
        [
            target("codex", RCP_CODEX),
            target("claude", RCP_CLAUDE),
            target("claude", OTHER_CLAUDE),
            target("codex", "not-a-uuid"),
        ],
    )
    assert [state for state, _error in outcomes] == ["deleted", "deleted", "absent", "failed"]
    assert list(project.iterdir()) == []
    assert [json.loads(line)[-1] for line in fake_providers.read_text().splitlines()] == [RCP_CODEX]


@pytest.mark.parametrize("canonical", [True, False])
def test_only_the_canonical_data_dir_sweeps(manifest, tmp_path, monkeypatch, caplog, canonical):
    import logging

    from fastapi.testclient import TestClient

    from rcp.api import app as app_module
    from tests.helpers import create_named_app, wait_until

    data = (tmp_path / "data").resolve()
    if canonical:
        monkeypatch.setattr(app_module, "platform_data_dir", lambda: data)
    passes = []
    monkeypatch.setattr(
        app_module, "sweep_provider_sessions", lambda _store, **_kwargs: passes.append(True)
    )
    app = create_named_app(str(manifest.path), data_dir=data)
    with caplog.at_level(logging.INFO, logger="rcp.api.app"), TestClient(app):
        if canonical:
            wait_until(lambda: passes)
        else:
            wait_until(
                lambda: any(
                    record.name == "rcp.api.app" and record.args and record.args[0] == data
                    for record in caplog.records
                )
            )
            assert passes == []
