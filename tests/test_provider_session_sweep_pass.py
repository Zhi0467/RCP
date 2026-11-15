from __future__ import annotations

import json
import stat
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
import json, os, sys
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


def _run(store, operation_id, session, provider, stage_root="/stage", kind="project_chat"):
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
        stage_machine_id="backend",
        stage_root=stage_root,
    )


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
    return log


def test_a_pass_deletes_only_recorded_sessions_and_keeps_failures_marked(tmp_path, fake_providers):
    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "codex-turn", RCP_CODEX, "codex")
    _run(store, "codex-gone", GONE_CODEX, "codex")
    _run(store, "codex-forked", FORKED_CODEX, "codex")
    _run(store, "opencode-turn", RCP_OPENCODE, "opencode")
    _run(store, "opencode-gone", GONE_OPENCODE, "opencode")
    _run(store, "agy-turn", "agy-session", "agy")
    sweep_provider_sessions(store)
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
    sweep_provider_sessions(store)
    retried = {json.loads(line)[-1] for line in fake_providers.read_text().splitlines()}
    assert retried == {FORKED_CODEX}


def test_claude_deletes_the_exact_transcript_and_its_folder(tmp_path, monkeypatch):
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    stage = tmp_path / "stage"
    stage.mkdir()
    project = config / "projects" / claude_project_directory(stage.resolve())
    (project / RCP_CLAUDE / "subagents").mkdir(parents=True)
    (project / RCP_CLAUDE / "subagents" / "agent.jsonl").write_text("{}")
    (project / f"{RCP_CLAUDE}.jsonl").write_text("{}")
    (project / f"{OTHER_CLAUDE}.jsonl").write_text("{}")
    store = AppStore(tmp_path / "app.sqlite3")
    _run(store, "claude-turn", RCP_CLAUDE, "claude", stage_root=str(stage))
    _run(store, "claude-coach", OTHER_CLAUDE, "claude", kind="paper_coach")
    sweep_provider_sessions(store)
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
    sweep_provider_sessions(store)
    assert store.provider_session_swept(RCP_CODEX)
    pending = store.provider_session_sweep_targets(store.now())
    assert [(target.session_id, target.marked) for target in pending] == [(RCP_CODEX, True)]


def test_the_shipped_script_deletes_over_stdin_with_discovery(tmp_path, fake_providers):
    import subprocess
    import sys

    from rcp.transport.state import _remote_script

    items = [
        {"provider": "codex", "session_id": RCP_CODEX, "binary": None, "install_paths": []},
        {"provider": "codex", "session_id": "not-a-uuid", "binary": None, "install_paths": []},
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _remote_script("provider_session_delete.py"),
            _remote_script("provider_discovery.py"),
        ],
        input=json.dumps({"items": items, "timeout": 10}),
        capture_output=True,
        text=True,
        check=True,
    )
    deleted, refused = json.loads(result.stdout)
    assert deleted is None
    assert refused
    assert [json.loads(line)[-1] for line in fake_providers.read_text().splitlines()] == [RCP_CODEX]
