"""A pass the host recorded, judged here by the decoder that judges live ones.

Each case runs the real supervisor as a subprocess, reads the journal it wrote,
and asks RCP what the turn was worth. The host never says; that is the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcp.providers import ProviderTurnRequest
from rcp.runs.recorded_turn import decode_recorded_turn, recorded_provider_turn

from .test_remote_turn_supervisor import _supervise


def _request(tmp_path: Path) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        prompt="recorded",
        binary="codex",
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[],
        write_dirs=[],
        write_scope=None,
        capability="paper_readonly",
        provider_version="0.153.4",
    )


def _journal_from_disk(directory: Path) -> dict[str, object]:
    outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
    return {
        "outcome": outcome,
        "events": (directory / "events.jsonl").read_text(encoding="utf-8"),
        "stderr": (directory / "stderr.txt").read_text(encoding="utf-8"),
        "patch": (
            (directory / "patch.json").read_text(encoding="utf-8")
            if outcome.get("patch_present")
            else None
        ),
    }


def _recorded(tmp_path: Path, events: list[dict]):
    # What the pass exited with is the supervisor's own contract, tested there.
    # Here the journal is the subject.
    _completed, directory = _supervise(tmp_path, json.dumps({"emit": events, "exit": 0}) + "\n")
    pid_file = str(directory)[: -len(".turn")]
    return recorded_provider_turn(pid_file, _journal_from_disk(directory)), directory


def test_a_completed_turn_reads_back_as_complete(tmp_path) -> None:
    recorded, _ = _recorded(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "the answer"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
        ],
    )
    verdict = decode_recorded_turn(recorded, _request(tmp_path))

    assert verdict.complete is True
    assert any(event.text == "the answer" for event in verdict.events)
    assert any(event.usage is not None for event in verdict.events)
    assert not [event for event in verdict.events if event.event == "error"]


def test_a_failed_turn_reads_back_in_the_provider_s_own_words(tmp_path) -> None:
    """The host recorded this exactly as it recorded a success; only RCP parts them.

    And it parts them the way the live pipe does -- an `error` event among the
    decoded ones -- rather than through a verdict flag that exists only for
    recovered turns.
    """

    recorded, _ = _recorded(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.failed", "error": {"message": "the model refused"}},
        ],
    )
    verdict = decode_recorded_turn(recorded, _request(tmp_path))

    assert recorded.outcome["terminal_event"] is True
    assert "protocol_complete" not in recorded.outcome
    assert [event.text for event in verdict.events if event.event == "error"] == [
        "the model refused"
    ]


def test_a_turn_that_never_ended_reads_back_as_incomplete(tmp_path) -> None:
    recorded, _ = _recorded(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "half"}},
        ],
    )
    verdict = decode_recorded_turn(recorded, _request(tmp_path))

    assert recorded.outcome["terminal_event"] is False
    assert verdict.complete is False


def test_the_host_record_carries_its_own_acceptance(tmp_path) -> None:
    recorded, _ = _recorded(tmp_path, [{"type": "turn.completed"}])

    assert recorded.accepted is True
    assert recorded.intact is True


def test_events_that_do_not_match_their_digest_are_refused(tmp_path) -> None:
    """A record nothing verified is a record anything could have written."""

    recorded, directory = _recorded(tmp_path, [{"type": "turn.completed"}])
    journal = _journal_from_disk(directory)
    journal["events"] = journal["events"] + '{"type":"item.completed"}\n'

    with pytest.raises(ValueError, match="do not match the digest"):
        recorded_provider_turn(recorded.pid_file, journal)


def test_a_record_naming_another_pass_is_refused(tmp_path) -> None:
    _recorded_turn, directory = _recorded(tmp_path, [{"type": "turn.completed"}])

    with pytest.raises(ValueError, match="different pass"):
        recorded_provider_turn("/stage/someone-else.pid", _journal_from_disk(directory))


@pytest.mark.parametrize("field", ["journal_complete", "error", "stopped"])
def test_a_record_that_lost_evidence_is_never_called_complete(tmp_path, field) -> None:
    """Whole enough to read is a separate question from whether the turn worked."""

    recorded, directory = _recorded(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed"},
        ],
    )
    assert decode_recorded_turn(recorded, _request(tmp_path)).complete is True

    journal = _journal_from_disk(directory)
    journal["outcome"][field] = False if field == "journal_complete" else "something went wrong"
    damaged = recorded_provider_turn(recorded.pid_file, journal)

    assert damaged.intact is False
    assert decode_recorded_turn(damaged, _request(tmp_path)).complete is False
