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
    close_input_after_initial: bool = True,
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
        "codex",
        "--runtime-id",
        runtime_id,
        "--patch-path",
        patch_path or str(stage / "workspace" / "patch.json"),
        "--watch-path",
        watch_path or str(stage / "workspace" / "watch.json"),
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
        "--stop-hold-seconds",
        "0",
        "--stop-grace-seconds",
        "5",
        "--poll-seconds",
        "0.02",
    ]
    if close_input_after_initial:
        argv.append("--close-input-after-initial")
    argv += ["--", sys.executable, "-c", provider]
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
