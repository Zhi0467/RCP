"""The table that decides what an unwatched remote pass is owed.

Every row of it, driven without a host: what the host would have said is the
input, not the thing under test.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from rcp.runs.remote_reconciliation import JournalUnavailable, reconcile_remote_pass

from .test_remote_provider_receipts import _store


def _journal(*, accepted=True, terminal=True, intact=True, events=""):
    outcome = {
        "version": 1,
        "pid_file": "/stage/one.pid",
        "provider": "codex",
        "runtime_id": "codex.exec-json.v1",
        "provider_version": "0.153.4",
        "accepted": accepted,
        "terminal_event": terminal,
        "journal_complete": intact,
        "error": None,
        "stopped": False,
        "events_sha256": hashlib.sha256(events.encode("utf-8", "surrogateescape")).hexdigest(),
        "patch_present": False,
        "patch_sha256": None,
    }
    return {"outcome": outcome, "events": events, "stderr": "", "patch": None}


@pytest.fixture
def waiting(tmp_path):
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    record = store.agent_task("first").model_copy(
        update={"stage_host": "remote", "stage_root": "/stage"}
    )
    return store, record


def _reconcile(waiting, *, stopped, journal=None, raises=None):
    store, record = waiting

    def read(_host, _pid_file):
        if raises is not None:
            raise raises
        return journal

    return reconcile_remote_pass(store, record, stopped=lambda *_: stopped, read_journal=read)


def test_an_unreachable_host_is_waited_on(waiting) -> None:
    """No answer is not an answer, and guessing here replaces a live provider."""

    result = _reconcile(waiting, stopped=None)

    assert result.action == "wait"
    assert "could not be reached" in result.reason


def test_a_running_provider_is_waited_on(waiting) -> None:
    result = _reconcile(waiting, stopped=False)

    assert result.action == "wait"


def test_a_finished_turn_is_finalized_on_the_task_that_started_it(waiting) -> None:
    events = json.dumps({"type": "turn.completed"}) + "\n"
    result = _reconcile(waiting, stopped=True, journal=_journal(events=events))

    assert result.action == "finalize"
    assert result.recorded is not None
    assert result.pid_file == "/stage/one.pid"


def test_a_provider_that_stopped_mid_turn_fails_that_same_task(waiting) -> None:
    result = _reconcile(waiting, stopped=True, journal=_journal(terminal=False))

    assert result.action == "fail"
    assert "before its turn reached an end" in result.reason


def test_a_pass_that_never_took_the_prompt_says_so(waiting) -> None:
    """The one state where nothing ran, and so the one where a retry is safe."""

    result = _reconcile(waiting, stopped=True, journal=_journal(accepted=False))

    assert result.action == "fail"
    assert "never took this turn's prompt" in result.reason


def test_a_stopped_provider_that_wrote_nothing_fails(waiting) -> None:
    result = _reconcile(waiting, stopped=True, journal=None)

    assert result.action == "fail"
    assert "without writing a journal" in result.reason


def test_a_journal_that_cannot_be_trusted_fails_rather_than_applies(waiting) -> None:
    journal = _journal(events=json.dumps({"type": "turn.completed"}) + "\n")
    journal["events"] = journal["events"] + "tampered\n"

    result = _reconcile(waiting, stopped=True, journal=journal)

    assert result.action == "fail"
    assert "could not be trusted" in result.reason


def test_a_journal_that_could_not_be_read_is_waited_on_not_failed(waiting) -> None:
    """A link that dropped mid-read says nothing about the turn."""

    result = _reconcile(waiting, stopped=True, raises=JournalUnavailable("connection reset"))

    assert result.action == "wait"


def test_a_settled_pass_is_left_alone(waiting) -> None:
    store, record = waiting
    store.finish_remote_provider_pass("first", "/stage/one.pid")

    result = reconcile_remote_pass(
        store, record, stopped=lambda *_: True, read_journal=lambda *_: None
    )

    assert result.action == "settled"


def _plan(store, **probes):
    from rcp.runs.remote_finalization import plan_remote_reconciliation

    return plan_remote_reconciliation(store, **probes)


def test_a_chat_turn_is_routed_to_the_work_owner() -> None:
    """The routing is a table, not a branch anyone can forget to extend."""

    from rcp.runs.remote_finalization import RECORDED_FINALIZERS, recorded_finalizer
    from rcp.runs.tasks.work import finalize_recorded_work_result

    assert recorded_finalizer("project_chat") is finalize_recorded_work_result
    assert recorded_finalizer("node_chat") is finalize_recorded_work_result
    assert recorded_finalizer("seed") is None
    # Every registered owner finalizes; none of them launches.
    assert set(RECORDED_FINALIZERS) == {"node_chat", "project_chat"}


def test_a_kind_with_no_recorded_owner_waits_rather_than_being_settled(tmp_path) -> None:
    """Better an unfinished turn than one settled by an owner that never agreed."""

    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    store.checkpoint_agent_task("first", stage_host="remote", stage_root="/stage")
    events = json.dumps({"type": "turn.completed"}) + "\n"

    planned = _plan(store, stopped=lambda *_: True, read_journal=lambda *_: _journal(events=events))

    # The receipts fixture builds a `seed` task, which has no recorded owner yet.
    assert store.agent_task("first").kind == "seed"
    assert planned[0].reconciliation.action == "wait"
    assert "No recorded-result owner" in planned[0].reconciliation.reason
    assert planned[0].actionable is False


def test_nothing_is_planned_once_the_pass_is_settled(tmp_path) -> None:
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    store.finish_remote_provider_pass("first", "/stage/one.pid")

    assert _plan(store, stopped=lambda *_: True, read_journal=lambda *_: None) == []
