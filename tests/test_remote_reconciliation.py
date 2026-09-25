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
    return {
        "accepted": ({"version": 1, "pid_file": "/stage/one.pid", "at": 1.0} if accepted else None),
        "outcome": outcome,
        "events": events,
        "stderr": "",
        "patch": None,
    }


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


def test_a_pass_that_never_took_the_prompt_says_so(waiting) -> None:
    """The one state where nothing ran, and so the one where a retry is safe."""

    result = _reconcile(waiting, stopped=True, journal=_journal(accepted=False))

    assert result.action == "fail"


def test_a_stopped_provider_that_wrote_nothing_fails(waiting) -> None:
    result = _reconcile(waiting, stopped=True, journal=None)

    assert result.action == "fail"


def test_a_journal_that_cannot_be_trusted_fails_rather_than_applies(waiting) -> None:
    journal = _journal(events=json.dumps({"type": "turn.completed"}) + "\n")
    journal["events"] = journal["events"] + "tampered\n"

    result = _reconcile(waiting, stopped=True, journal=journal)

    assert result.action == "fail"


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


@pytest.mark.parametrize("kind", ["project_chat", "node_chat"])
@pytest.mark.parametrize("mode", ["work", "discuss"])
def test_chat_kind_alone_never_selects_a_recorded_owner(tmp_path, kind, mode) -> None:
    from rcp.runs.remote_finalization import recorded_finalizer
    from rcp.storage import AgentTaskRecord

    store = _store(tmp_path)
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="chat",
            project_id="chat",
            kind=kind,
            status="queued",
            request={"mode": mode},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Queued",
        )
    )
    assert recorded_finalizer(store, "chat") is None


def test_the_owner_that_retained_the_context_finalizes_it(tmp_path) -> None:
    from rcp.runs.remote_finalization import recorded_finalizer
    from rcp.runs.tasks.work import WORK_FINALIZATION_CONTEXT_ROLE, finalize_recorded_work_result

    store = _store(tmp_path)
    content = "{}"
    store.record_agent_task_contract(
        "first",
        WORK_FINALIZATION_CONTEXT_ROLE,
        content,
        hashlib.sha256(content.encode()).hexdigest(),
    )
    assert recorded_finalizer(store, "first") is finalize_recorded_work_result


def test_a_kind_with_no_recorded_owner_waits_rather_than_being_settled(tmp_path) -> None:
    """Better an unfinished turn than one settled by an owner that never agreed."""

    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    store.checkpoint_agent_task("first", stage_host="remote", stage_root="/stage")
    store.interrupt_active_agent_tasks()
    events = json.dumps({"type": "turn.completed"}) + "\n"

    planned = _plan(store, stopped=lambda *_: True, read_journal=lambda *_: _journal(events=events))

    # The receipts fixture builds a `seed` task, which has no recorded owner yet.
    assert store.agent_task("first").kind == "seed"
    assert planned[0].reconciliation.action == "wait"
    assert planned[0].actionable is False


def test_a_turn_a_live_worker_is_watching_is_never_planned(tmp_path) -> None:
    """An unresolved pass is not an abandoned one.

    A worker streaming its provider right now holds exactly the same receipt: the
    pass is open because the turn has not finished. Planning against that races
    the worker to settle one turn twice, so only the durable phase -- which no
    live worker sets -- admits a task here.
    """

    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    store.checkpoint_agent_task("first", stage_host="remote", stage_root="/stage")

    assert store.operation_ids_awaiting_remote_result() == []
    assert _plan(store, stopped=lambda *_: True, read_journal=lambda *_: None) == []


def test_only_one_caller_can_claim_a_recorded_finalization(tmp_path) -> None:
    """Two settlers would apply one turn twice, so the claim has to be exclusive."""

    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    store.interrupt_active_agent_tasks()

    assert store.claim_recorded_finalization("first") is True
    assert store.claim_recorded_finalization("first") is False
    assert store.operation_ids_awaiting_remote_result() == []

    store.release_recorded_finalization("first")

    assert store.operation_ids_awaiting_remote_result() == ["first"]


def test_nothing_is_planned_once_the_pass_is_settled(tmp_path) -> None:
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid", supervised=True)
    store.finish_remote_provider_pass("first", "/stage/one.pid")

    assert _plan(store, stopped=lambda *_: True, read_journal=lambda *_: None) == []


def test_a_host_that_answered_with_bad_evidence_fails_rather_than_waits(waiting) -> None:
    """Waiting for a reachable host to answer differently is waiting forever."""

    from rcp.runs.remote_reconciliation import JournalCorrupt

    result = _reconcile(waiting, stopped=True, raises=JournalCorrupt("unsafe entry"))

    assert result.action == "fail"


def test_the_shipped_reader_separates_bad_evidence_from_an_unreachable_host(tmp_path) -> None:
    """The two exits mean different things, so the reader must not share one."""

    import subprocess
    import sys

    from rcp.transport.state import _remote_script

    stage = tmp_path / "stage"
    (stage / "agent.pid.turn").mkdir(parents=True)
    (stage / "agent.pid.turn" / "outcome.json").write_text("not json", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _remote_script("remote_turn_journal.py"),
            str(stage / "agent.pid"),
            "1000000",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 3
