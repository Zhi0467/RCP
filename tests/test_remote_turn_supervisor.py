"""The turn supervisor, run the way an execution host runs it.

`python -c <composed source>` with the same argv, against a scripted provider.
Nothing here needs a reachable machine, so the guards can be driven directly.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.transport.state import _remote_turn_supervisor_script

_PROVIDER = """\
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request.get("sleep"):
        time.sleep(float(request["sleep"]))
    for event in request.get("emit", []):
        sys.stdout.write(json.dumps(event) + "\\n")
        sys.stdout.flush()
    if request.get("stderr"):
        sys.stderr.write(request["stderr"])
        sys.stderr.flush()
    if request.get("exit") is not None:
        sys.exit(int(request["exit"]))
"""


def _supervise(
    tmp_path: Path,
    stdin: str,
    *,
    runtime_id: str = "codex.exec-json.v1",
    provider: str = _PROVIDER,
    patch_path: str | None = None,
    watch_path: str | None = None,
    experiment_watch_glob: str | None = None,
    experiment_watch_files: int = 64,
    experiment_watch_bytes: int = 100000,
    close_input_after_initial: bool = True,
    codex_start_marker: Path | None = None,
    codex_state_path: Path | None = None,
    delegation_wait_limit: float = 3600,
    stop_grace: float = 5,
    detached: bool = False,
) -> tuple[subprocess.CompletedProcess, Path]:
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    pid_file = stage / "agent.pid"
    argv = [
        sys.executable,
        "-c",
        _remote_turn_supervisor_script(),
        "--pid-file",
        str(pid_file),
        "--provider",
        "claude" if runtime_id.startswith("claude.") else "codex",
        "--runtime-id",
        runtime_id,
        "--patch-path",
        patch_path or str(stage / "workspace" / "patch.json"),
        "--watch-path",
        watch_path or str(stage / "workspace" / "watch.json"),
        "--experiment-watch-glob",
        experiment_watch_glob or str(stage / "workspace" / "experiment-watch-*.json"),
        "--provider-version",
        "0.153.4",
        "--max-journal-bytes",
        "1000000",
        "--max-event-bytes",
        "100000",
        "--max-stderr-bytes",
        "100000",
        "--max-patch-bytes",
        "100000",
        "--max-uplink-bytes",
        "1000000",
        "--max-control-messages",
        "500",
        "--max-experiment-watch-files",
        str(experiment_watch_files),
        "--max-experiment-watch-bytes",
        str(experiment_watch_bytes),
        "--stop-hold-seconds",
        "0",
        "--stop-grace-seconds",
        str(stop_grace),
        "--delegation-wait-limit-seconds",
        str(delegation_wait_limit),
        "--poll-seconds",
        "0.02",
    ]
    if codex_start_marker is not None:
        argv.extend(["--codex-start-marker", str(codex_start_marker)])
    if codex_state_path is not None:
        argv.extend(["--codex-state-path", str(codex_state_path)])
    if close_input_after_initial:
        argv.append("--close-input-after-initial")
    argv += ["--", sys.executable, "-c", provider]
    if detached:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        process.stdout.close()
        process.stdout = None
        _, stderr = process.communicate(stdin, timeout=60)
        completed = subprocess.CompletedProcess(argv, process.returncode, "", stderr)
    else:
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, start_new_session=True, timeout=60
        )
    return completed, Path(str(pid_file) + ".turn")


def _journal(directory: Path) -> dict:
    return json.loads((directory / "outcome.json").read_text(encoding="utf-8"))


def test_a_finished_turn_leaves_its_events_on_the_host(tmp_path) -> None:
    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 1}},
                ],
                "exit": 0,
            }
        )
        + "\n",
    )

    assert completed.returncode == 0, completed.stderr
    events = (journal / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(events[-1])["type"] == "turn.completed"
    outcome = _journal(journal)
    assert outcome["terminal_event"] is True
    assert outcome["journal_complete"] is True
    assert outcome["error"] is None


def test_the_supervisor_publishes_no_verdict(tmp_path) -> None:
    """A failed turn and a completed one are recorded the same way.

    The host says the turn ended and hands over its bytes. What the turn was
    worth is read off those bytes by the decoder that has always read them, so
    there is nothing here for that decoder to disagree with.
    """

    _completed, failed = _supervise(
        tmp_path / "failed",
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.failed", "error": {"message": "the model refused"}},
                ],
                "exit": 0,
            }
        )
        + "\n",
    )
    outcome = _journal(failed)

    assert outcome["terminal_event"] is True
    assert "protocol_complete" not in outcome
    assert not [key for key in outcome if "complete" in key and key != "journal_complete"]
    assert json.loads((failed / "events.jsonl").read_text().splitlines()[-1])["type"] == (
        "turn.failed"
    )


def test_a_pass_that_reached_the_provider_says_so_on_the_host(tmp_path) -> None:
    """Acceptance is the host's own record that work may have begun."""

    _completed, journal = _supervise(
        tmp_path,
        json.dumps({"emit": [{"type": "turn.completed"}], "exit": 0}) + "\n",
    )

    accepted = json.loads((journal / "accepted.json").read_text(encoding="utf-8"))
    assert accepted["version"] == 1
    assert accepted["at"] > 0
    assert _journal(journal)["accepted"] is True


def test_a_pass_handed_nothing_never_claims_acceptance(tmp_path) -> None:
    """No prompt reached the provider, so no later reader may think one did."""

    _completed, journal = _supervise(tmp_path, "")

    assert not (journal / "accepted.json").exists()
    assert _journal(journal)["accepted"] is False


def test_app_server_setup_alone_never_claims_the_turn_prompt(tmp_path) -> None:
    """Opening the protocol is not starting the user's turn."""

    _completed, journal = _supervise(
        tmp_path,
        json.dumps({"id": 1, "method": "initialize", "params": {}}) + "\n",
        runtime_id="codex.app-server-stdio.v1",
    )

    assert not (journal / "accepted.json").exists()
    assert _journal(journal)["accepted"] is False


def test_the_journal_keeps_the_turn_when_nobody_is_listening(tmp_path) -> None:
    """The point of the whole thing: output survives a reader that went away."""

    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "kept"}},
                    {"type": "turn.completed"},
                ],
                "exit": 0,
            }
        )
        + "\n",
    )

    assert "kept" in (journal / "events.jsonl").read_text(encoding="utf-8")
    assert completed.returncode == 0


def test_a_second_pass_never_overwrites_the_first_ones_evidence(tmp_path) -> None:
    line = json.dumps({"emit": [{"type": "turn.completed"}], "exit": 0}) + "\n"
    _first, journal = _supervise(tmp_path, line)
    assert journal.exists()

    kept = (journal / "events.jsonl").read_text(encoding="utf-8")
    second, _ = _supervise(tmp_path, line)

    assert second.returncode != 0
    assert (journal / "events.jsonl").read_text(encoding="utf-8") == kept


@pytest.mark.parametrize("state", ["", "not json at all"])
def test_a_provider_that_says_nothing_records_no_terminal_event(tmp_path, state) -> None:
    _completed, journal = _supervise(
        tmp_path, json.dumps({"emit": [], "stderr": state, "exit": 3}) + "\n"
    )
    outcome = _journal(journal)

    assert outcome["terminal_event"] is False
    assert outcome["return_code"] == 3


def test_a_finished_turn_snapshots_both_of_its_deliverables(tmp_path) -> None:
    """A Patch and a watcher handoff are one admission, so both are evidence.

    Reading either back off the stage later reads whatever the stage holds by
    then, which is not necessarily what the pass produced.
    """

    workspace = tmp_path / "stage" / "workspace"
    workspace.mkdir(mode=0o700, parents=True)
    patch_text = '{"ops": []}'
    watch_text = '{"external": [], "graph": []}'
    workspace.joinpath("patch.json").write_text(patch_text, encoding="utf-8")
    workspace.joinpath("watch.json").write_text(watch_text, encoding="utf-8")

    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 1}},
                ],
                "exit": 0,
            }
        )
        + "\n",
    )

    assert completed.returncode == 0, completed.stderr
    outcome = _journal(journal)
    assert outcome["patch_present"] is True
    assert outcome["watch_present"] is True
    assert (journal / "patch.json").read_text(encoding="utf-8") == patch_text
    assert (journal / "watch.json").read_text(encoding="utf-8") == watch_text
    assert outcome["watch_sha256"] == hashlib.sha256(watch_text.encode("utf-8")).hexdigest()


def test_a_turn_that_wrote_no_watcher_handoff_says_so(tmp_path) -> None:
    """Saying "there was none" is different from saying nothing at all."""

    workspace = tmp_path / "stage" / "workspace"
    workspace.mkdir(mode=0o700, parents=True)

    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 1}},
                ],
                "exit": 0,
            }
        )
        + "\n",
    )

    assert completed.returncode == 0, completed.stderr
    outcome = _journal(journal)
    assert outcome["watch_present"] is False
    assert outcome["watch_sha256"] is None
    assert not (journal / "watch.json").exists()


def test_a_finished_turn_snapshots_every_experiment_watcher_output(tmp_path) -> None:
    """Watcher maintenance writes one file per resource, and all of them are evidence.

    The settling turn discovers this set off the stage rather than naming it, so
    a file replaced, deleted or added after the pass would change which
    observers get armed. Pinning the bytes is what makes that impossible.
    """

    workspace = tmp_path / "stage" / "workspace"
    workspace.mkdir(mode=0o700, parents=True)
    first = '{"observers": [{"check_command": "true"}], "stops": []}'
    second = '{"observers": [], "stops": ["obs-1"]}'
    workspace.joinpath("experiment-watch-aaaa.json").write_text(first, encoding="utf-8")
    workspace.joinpath("experiment-watch-bbbb.json").write_text(second, encoding="utf-8")
    # Neither a differently named file nor a directory is part of the set.
    workspace.joinpath("notes.json").write_text("{}", encoding="utf-8")

    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 1}},
                ],
                "exit": 0,
            }
        )
        + "\n",
    )

    assert completed.returncode == 0, completed.stderr
    outcome = _journal(journal)
    assert outcome["experiment_watch_snapshotted"] is True
    assert sorted(outcome["experiment_watch_sha256"]) == [
        "experiment-watch-aaaa.json",
        "experiment-watch-bbbb.json",
    ]
    snapshots = journal / "experiment-watch"
    assert snapshots.joinpath("experiment-watch-aaaa.json").read_text(encoding="utf-8") == first
    assert snapshots.joinpath("experiment-watch-bbbb.json").read_text(encoding="utf-8") == second


def test_a_turn_that_maintained_no_experiment_watcher_says_so(tmp_path) -> None:
    """Snapshotting nothing is an answer; saying nothing at all is not."""

    workspace = tmp_path / "stage" / "workspace"
    workspace.mkdir(mode=0o700, parents=True)

    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 1}},
                ],
                "exit": 0,
            }
        )
        + "\n",
    )

    assert completed.returncode == 0, completed.stderr
    outcome = _journal(journal)
    assert outcome["experiment_watch_snapshotted"] is True
    assert outcome["experiment_watch_sha256"] == {}
    assert not (journal / "experiment-watch").exists()


@pytest.mark.parametrize("bound", ["files", "bytes"])
def test_a_flood_of_watcher_outputs_is_an_incomplete_turn(tmp_path, bound) -> None:
    """The provider chose this set's size, so the set is bounded as a whole.

    Copying every match would let one pass fill the host's disk, and a digest
    map large enough to exceed the journal reader's own bound would make an
    otherwise complete turn unreadable. Overflow is the same answer any other
    journal overflow gives.
    """

    workspace = tmp_path / "stage" / "workspace"
    workspace.mkdir(mode=0o700, parents=True)
    for index in range(4):
        workspace.joinpath(f"experiment-watch-{index}.json").write_text("x" * 64, encoding="utf-8")

    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {
                "emit": [
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 1}},
                ],
                "exit": 0,
            }
        )
        + "\n",
        experiment_watch_files=3 if bound == "files" else 64,
        experiment_watch_bytes=100000 if bound == "files" else 100,
    )

    assert completed.returncode != 0
    outcome = _journal(journal)
    assert outcome["error"]
    # No partial set is left claiming to be what the pass produced.
    assert outcome["experiment_watch_snapshotted"] is False
    assert outcome["experiment_watch_sha256"] == {}


def test_supervised_launch_ships_the_supervisor_as_a_staged_file():
    """The supervisor travels as a path, never as a command-line argument.

    An SSH multiplexing master cannot carry an argument this large: the client
    fills the control socket with the command and then fails passing the
    process's own file descriptors, so the launch dies before the execution
    host runs anything at all.
    """

    from rcp.agents.launcher import _supervised_remote_turn_command
    from rcp.transport.state import _remote_turn_supervisor_script

    source = _remote_turn_supervisor_script()
    command = _supervised_remote_turn_command(
        ["codex", "app-server", "--stdio"],
        supervisor_path="/stage/inputs/rcp-turn-supervisor-0123456789abcdef.py",
        pid_file="/stage/agent.pid",
        provider="codex",
        runtime_id="codex.app-server-stdio.v1",
        provider_version=None,
        patch_path="/stage/workspace/patch.json",
        watch_path="/stage/workspace/watch.json",
        experiment_watch_glob="/stage/workspace/experiment-watch-*.json",
        close_input_after_initial=False,
    )

    assert "/stage/inputs/rcp-turn-supervisor-0123456789abcdef.py" in command
    assert not any(source in argument for argument in command)
    assert len(" ".join(command)) < len(source)


def test_missing_codex_start_marker_refuses_deliverable_snapshot(tmp_path: Path) -> None:
    result, journal = _supervise(
        tmp_path,
        json.dumps({"emit": [{"type": "turn.completed"}], "exit": 0}) + "\n",
        codex_start_marker=tmp_path / "missing-marker",
    )
    assert result.returncode != 0
    outcome = json.loads((journal / "outcome.json").read_text())
    assert json.loads(outcome["error"])["code"] == "codex_hook_start_missing"
    assert outcome["patch_present"] is False


@pytest.mark.parametrize(
    "runtime", ["codex.exec-json.v1", "claude.stream-json.v1", "codex.app-server-stdio.v1"]
)
def test_delegation_deadline_stops_a_silent_provider_and_seals_patch(tmp_path, runtime):
    import time

    state_path = None
    if runtime == "codex.exec-json.v1":
        from rcp.agents.codex_turn_hooks import apply_hook, prepare_control

        control = tmp_path / "control"
        prepare_control(control, [])
        state_path = control / "state.json"
        apply_hook("SubagentStart", {"agent_id": "child"}, state_path, control / "started")
        apply_hook("Stop", {}, state_path, control / "started")
        apply_hook("Stop", {}, state_path, control / "started")
        events = [{"type": "thread.started", "thread_id": "root"}]
        request = {"prompt": "work"}
    elif runtime == "claude.stream-json.v1":
        events = [
            {"type": "system", "subtype": "task_started", "task_id": "child"},
            {"type": "result", "subtype": "success", "result": "parent answer"},
        ]
        request = {"type": "user", "uuid": "input", "message": {"role": "user", "content": "work"}}
    else:
        events = [
            {"id": 1, "result": {"thread": {"id": "root"}}},
            {
                "method": "item/started",
                "params": {
                    "threadId": "root",
                    "item": {
                        "type": "subAgentActivity",
                        "agentThreadId": "child",
                        "kind": "started",
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {"threadId": "root", "turn": {"id": "turn", "status": "completed"}},
            },
        ]
        request = {"id": 1, "method": "thread/start", "params": {}}
    patch = tmp_path / "patch.json"
    patch.write_text('{"ops": []}')
    provider = (
        "import sys, json, time, signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdin.readline()\n"
        f"events = {events!r}\n"
        "for event in events: print(json.dumps(event), flush=True)\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    completed, journal = _supervise(
        tmp_path,
        json.dumps(request) + "\n",
        runtime_id=runtime,
        provider=provider,
        patch_path=str(patch),
        codex_state_path=state_path,
        delegation_wait_limit=0.15,
        stop_grace=0.05,
    )
    assert time.monotonic() - started < 10
    assert completed.returncode != 0
    assert (journal / "outcome.json").exists(), completed.stderr
    outcome = _journal(journal)
    assert outcome["verdict"] == outcome["failure_kind"] == "delegation_unfinished"
    assert outcome["error"] is None
    assert (journal / "patch.json").read_text() == patch.read_text()
    receipts = (journal / "delegation.jsonl").read_text().splitlines()
    assert len(receipts) == (2 if state_path else 1)
    assert "RCP_TURN_RECEIPT:" in completed.stdout


def test_timeout_fences_shutdown_output_and_kills_a_child_that_closed_its_pipes(tmp_path):
    provider = (
        "import json,os,signal,sys,time\n"
        "sys.stdin.readline()\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "    os.close(0); os.close(1); os.close(2)\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        f"open({str(tmp_path / 'child.pid')!r},'w').write(str(child))\n"
        "def stop(sig,frame):\n"
        "    print(json.dumps({'type':'result','subtype':'error','is_error':True}),flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "print(json.dumps({'type':'system','subtype':'task_started','task_id':'child'}),flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success'}),flush=True)\n"
        "sys.stdout.write('{'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    completed, journal = _supervise(
        tmp_path,
        json.dumps(
            {"type": "user", "uuid": "input", "message": {"role": "user", "content": "work"}}
        )
        + "\n",
        runtime_id="claude.stream-json.v1",
        provider=provider,
        delegation_wait_limit=0.15,
        stop_grace=0.05,
    )
    assert _journal(journal)["verdict"] == "delegation_unfinished"
    assert '"is_error"' not in completed.stdout
    assert "\nRCP_TURN_RECEIPT:" in completed.stdout
    assert '"is_error"' not in (journal / "events.jsonl").read_text()
    child_status = subprocess.run(
        ["ps", "-o", "stat=", "-p", (tmp_path / "child.pid").read_text()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child_status.returncode in {0, 1} and not child_status.stderr
    child_state = child_status.stdout.strip()
    # Some execution hosts leave an orphan zombie until their init reaps it.
    assert not child_state or child_state.startswith("Z")
