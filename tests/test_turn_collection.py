from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents.launcher import AgentProcessControl
from rcp.limits import TURN_JOURNAL_MAX_EVENT_BYTES
from rcp.providers import ProviderTurnRequest
from rcp.runs.turn_collection import (
    CollectedTurn,
    CollectionPending,
    can_collect,
    can_stop_remote_provider,
    describe_provider_silence,
    incomplete_collection_text,
    journal_pid_file,
    journal_pid_files,
    read_collected_turn,
    recorded_provider_identity,
    replay_collected_events,
)
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, AppStore
from rcp.transport import RemoteRunStage
from rcp.transport.remote_turn_journal import read_journal


def _failed_turn(tmp_path, *, status="failed", failure_kind="transport_lost"):
    store = AppStore(tmp_path / "state.sqlite3")
    root = tmp_path / "stage"
    root.mkdir(mode=0o700)
    pid = root / "agent.pid"
    record = store.create_agent_task(
        AgentTaskRecord(
            operation_id="original",
            project_id="project",
            kind="project_chat",
            status="running",
            request={"provider": "codex", "mode": "work"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Running",
            stage_host="test-host",
            stage_root=str(root),
            native_session_id="native-thread",
        )
    )
    store.begin_remote_provider_pass(
        record.operation_id, "test-host", str(root), str(pid), journaled=True
    )
    store.deliver_remote_provider_pass(record.operation_id, str(pid))
    if status == "paused":
        store.pause_agent_task(record.operation_id)
    elif status == "interrupted":
        store.interrupt_active_agent_tasks()
    else:
        store.fail_agent_task(record.operation_id, "Provider ended", failure_kind=failure_kind)
    return store, store.agent_task(record.operation_id), pid


def _journal(
    pid: Path,
    *,
    complete=True,
    answer="Finished the original work.",
    patch='{"ops":[]}\n',
    generated_tokens=20,
    ensure_ascii=True,
):
    root = Path(str(pid) + ".turn")
    root.mkdir()
    events = (
        "\n".join(
            json.dumps(value, ensure_ascii=ensure_ascii)
            for value in (
                {"type": "thread.started", "thread_id": "native-thread"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": answer},
                },
                {
                    "type": "turn.completed",
                    "id": pid.name,
                    "usage": {"input_tokens": 100, "output_tokens": generated_tokens},
                },
            )
        )
        + "\n"
    )
    outcome = {
        "version": 1,
        "pid_file": str(pid),
        "provider": "codex",
        "runtime_id": "codex.exec-json.v1",
        "provider_version": None,
        "protocol_complete": complete,
        "journal_complete": True,
        "patch_present": patch is not None,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch is not None else None,
        "events_sha256": hashlib.sha256(events.encode()).hexdigest(),
    }
    (root / "events.jsonl").write_text(events)
    if patch is not None:
        (root / "patch.json").write_text(patch)
    (root / "outcome.json").write_text(json.dumps(outcome))
    return root, outcome, events, patch


def _local_transport(monkeypatch):
    def attach(self, root):
        self.root = Path(root)
        return self

    monkeypatch.setattr(RemoteRunStage, "attach", attach)
    monkeypatch.setattr(
        RemoteRunStage,
        "_ssh",
        lambda _self, args: subprocess.run(
            args,
            text=True,
            capture_output=True,
            check=False,
        ),
    )


def _request():
    return ProviderTurnRequest(
        prompt="",
        binary="codex",
        cwd=Path("/workspace"),
        model=None,
        reasoning=None,
        session_id="native-thread",
        read_dirs=[],
        write_dirs=[],
        write_scope=None,
        capability="scratch_patch",
        provider_version=None,
    )


def test_collection_is_never_offered_before_a_provider_started(tmp_path):
    """A turn that died before its provider started is retried, never collected.

    Auto-research mail is bound to its wake task when that task row is inserted,
    and `_stage_claimed_mail` skips staging for a collection on the grounds that
    a started provider was already shown it. Staging runs before launch, so that
    holds only while eligibility requires a start receipt. Drop this requirement
    and collection silently swallows mail no agent ever saw.
    """

    store = AppStore(tmp_path / "state.sqlite3")
    root = tmp_path / "stage"
    root.mkdir(mode=0o700)
    record = store.create_agent_task(
        AgentTaskRecord(
            operation_id="never-started",
            project_id="project",
            kind="project_chat",
            status="running",
            request={"provider": "codex", "mode": "work"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Running",
            stage_host="test-host",
            stage_root=str(root),
            native_session_id="native-thread",
        )
    )
    store.fail_agent_task(record.operation_id, "Link died", failure_kind="transport_lost")
    assert not can_collect(store, store.agent_task(record.operation_id))


@pytest.mark.parametrize("stopped", [False, None])
def test_collect_waits_for_original_process_without_reading_or_releasing_stage(
    tmp_path,
    monkeypatch,
    stopped,
):
    store, record, pid = _failed_turn(tmp_path)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: stopped)
    monkeypatch.setattr(
        RemoteRunStage, "_ssh", lambda *_: pytest.fail("Must wait before reading journal")
    )
    assert can_collect(store, record)
    with pytest.raises(CollectionPending):
        read_collected_turn(store, record)
    assert store.unresolved_remote_provider_passes("test-host", record.stage_root) == [
        ("original", str(pid))
    ]
    assert len(store.agent_tasks("project")) == 1


def test_collect_reads_bound_snapshot_and_reuses_labelled_decoder(tmp_path, monkeypatch):
    store, record, pid = _failed_turn(tmp_path)
    _root, _outcome, _events, patch = _journal(pid)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    collected = read_collected_turn(store, record)
    assert collected.patch == patch and collected.session_id == "native-thread"
    events = replay_collected_events(collected, _request())
    assert [item.text for item in events if item.event == "answer"] == [
        "Finished the original work."
    ]
    assert next(item.usage for item in events if item.usage).generated_tokens == 20
    assert events[-1].event == "done"


@pytest.mark.parametrize("field", ["events.jsonl", "patch.json"])
def test_collect_refuses_changed_snapshot(tmp_path, monkeypatch, field):
    store, record, pid = _failed_turn(tmp_path)
    root, *_ = _journal(pid)
    (root / field).write_text("tampered")
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    with pytest.raises(ValueError, match="changed after completion"):
        read_collected_turn(store, record)


def test_stopped_without_receipt_is_incomplete_not_retried(tmp_path, monkeypatch):
    store, record, _pid = _failed_turn(tmp_path)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    collected = read_collected_turn(store, record)
    events = replay_collected_events(collected, _request())
    assert len(events) == 1 and events[0].event == "error"
    assert "without a durable completion receipt" in events[0].text


@pytest.mark.parametrize(
    "override",
    [
        {"protocol_complete": False},
        {"journal_complete": False},
        {"error": "snapshot unavailable"},
        {"stopped": True},
    ],
)
def test_incomplete_or_stopped_journal_cannot_produce_success(tmp_path, override):
    _store, _record, pid = _failed_turn(tmp_path)
    _root, outcome, events, patch = _journal(pid)
    collected = CollectedTurn("original", str(pid), {**outcome, **override}, events, patch)
    assert [event.event for event in replay_collected_events(collected, _request())] == ["error"]


def test_remote_journal_reader_refuses_symlinks_and_size_overflow(tmp_path):
    _store, _record, pid = _failed_turn(tmp_path)
    root, *_ = _journal(pid)
    with pytest.raises(ValueError, match="bound"):
        read_journal(str(pid), 10)
    (root / "patch.json").unlink()
    (root / "patch.json").symlink_to(root / "events.jsonl")
    with pytest.raises(OSError):
        read_journal(str(pid), 10000)


def _next_pass(store, record, previous_pid, name):
    store.finish_remote_provider_pass(record.operation_id, str(previous_pid))
    pid = previous_pid.with_name(name)
    store.begin_remote_provider_pass(
        record.operation_id, record.stage_host, record.stage_root, str(pid), journaled=True
    )
    store.deliver_remote_provider_pass(record.operation_id, str(pid))
    return pid


def test_collection_keeps_operational_answer_and_latest_completed_correction(tmp_path, monkeypatch):
    store, record, initial_pid = _failed_turn(tmp_path)
    _journal(initial_pid, patch="original rejected patch", generated_tokens=20)
    correction_pid = _next_pass(store, record, initial_pid, "correction.pid")
    _journal(
        correction_pid,
        answer="Only fixed the graph reflection.",
        patch="corrected patch",
        generated_tokens=30,
    )
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    collected = read_collected_turn(store, record)
    assert collected.pid_file == str(correction_pid)
    assert collected.patch == "corrected patch"
    events = replay_collected_events(collected, _request())
    assert [event.text for event in events if event.event == "answer"] == [
        "Finished the original work."
    ]
    usages = [event.usage for event in events if event.usage]
    assert [usage.generated_tokens for usage in usages] == [20, 30]
    # Original live usage plus reconnect replay count each paid pass only once.
    store.record_agent_usage(record.operation_id, usages[0])
    for usage in usages:
        store.record_agent_usage(record.operation_id, usage)
    assert sum(item.generated_tokens for item in store.agent_usage("project") if item.counted) == 50


@pytest.mark.parametrize("missing_receipt", [False, True])
def test_incomplete_correction_preserves_answer_and_only_completed_patch(
    tmp_path, monkeypatch, missing_receipt
):
    store, record, initial_pid = _failed_turn(tmp_path)
    _journal(initial_pid, patch="rejected original patch")
    correction_pid = _next_pass(store, record, initial_pid, "correction.pid")
    if not missing_receipt:
        _journal(
            correction_pid,
            complete=False,
            answer="Partial correction prose.",
            patch="unfinished patch",
        )
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    collected = read_collected_turn(store, record)
    assert collected.complete
    assert collected.patch == "rejected original patch"
    assert collected.correction_error
    events = replay_collected_events(collected, _request())
    assert [event.text for event in events if event.event == "answer"] == [
        "Finished the original work."
    ]
    assert not any(event.event == "error" for event in events)
    assert events[-1].event == "done"


def test_completed_correction_without_patch_retains_repairable_candidate(tmp_path, monkeypatch):
    store, record, initial_pid = _failed_turn(tmp_path)
    _journal(initial_pid, patch="rejected original patch")
    correction_pid = _next_pass(store, record, initial_pid, "correction.pid")
    _journal(correction_pid, answer="No corrected file.", patch=None)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    collected = read_collected_turn(store, record)
    assert collected.patch == "rejected original patch"
    assert "without writing patch.json" in collected.correction_error


def test_preprompt_fallback_is_not_selected_as_operational_pass(tmp_path, monkeypatch):
    store, record, handshake_pid = _failed_turn(tmp_path)
    root, outcome, *_ = _journal(handshake_pid, complete=False, answer="No authorized turn.")
    outcome["runtime_id"] = "codex.app-server-stdio.v1"
    outcome["provider_version"] = "0.153.2"
    outcome["journal_complete"] = False
    (root / "outcome.json").write_text(json.dumps(outcome))
    initial_pid = _next_pass(store, record, handshake_pid, "operational.pid")
    _journal(initial_pid, answer="Actual operational answer.", patch="original patch")
    record = store.agent_task(record.operation_id)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    collected = read_collected_turn(store, record)
    assert collected.patch == "original patch"
    assert [
        event.text
        for event in replay_collected_events(collected, _request())
        if event.event == "answer"
    ] == ["Actual operational answer."]


def test_running_correction_blocks_collection_before_any_earlier_journal_read(
    tmp_path, monkeypatch
):
    store, record, initial_pid = _failed_turn(tmp_path)
    _journal(initial_pid)
    correction_pid = _next_pass(store, record, initial_pid, "correction.pid")
    _local_transport(monkeypatch)
    probed = []
    monkeypatch.setattr(
        AgentProcessControl, "remote_stopped", lambda host, pid: probed.append(pid) or False
    )
    monkeypatch.setattr(
        RemoteRunStage,
        "_ssh",
        lambda *_: pytest.fail("Do not read an earlier answer while correction owns scratch"),
    )
    with pytest.raises(CollectionPending):
        read_collected_turn(store, record)
    assert probed == [str(correction_pid)]


def test_app_server_collection_replays_canonical_handshake_answer_and_usage():
    def usage(turn_id, generated):
        return {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "native-thread",
                "turnId": turn_id,
                "tokenUsage": {"total": {"inputTokens": 100, "outputTokens": generated}},
            },
        }

    wire = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"config": {}}},
        {
            "id": 3,
            "result": {
                "thread": {"id": "native-thread"},
                "approvalPolicy": "never",
                "sandbox": {"type": "workspaceWrite"},
            },
        },
        usage("previous-turn", 10),
        {"id": 4, "result": {"turn": {"id": "current-turn"}}},
        {"id": "steer:accepted-message", "result": {"turnId": "current-turn"}},
        {
            "method": "item/completed",
            "params": {
                "threadId": "unrelated",
                "item": {"type": "agentMessage", "text": "Subagent answer."},
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "native-thread",
                "turnId": "current-turn",
                "item": {"type": "agentMessage", "text": "Root operational answer."},
            },
        },
        usage("current-turn", 30),
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "current-turn", "status": "completed"},
            },
        },
    ]
    collected = CollectedTurn(
        "original",
        "/stage/main.pid",
        {
            "provider": "codex",
            "runtime_id": "codex.app-server-stdio.v1",
            "provider_version": "0.153.2",
            "protocol_complete": True,
            "journal_complete": True,
            "root_thread_id": "native-thread",
            "root_turn_id": "current-turn",
        },
        "\n".join(json.dumps(value) for value in wire),
        None,
    )
    events = replay_collected_events(collected, _request())
    assert not any(event.event == "error" for event in events)
    assert [event.text for event in events if event.event == "answer"] == [
        "Root operational answer."
    ]
    assert [event.session_id for event in events if event.event == "session"] == ["native-thread"]
    usages = [event.usage for event in events if event.usage]
    assert len(usages) == 1 and usages[0].dedupe_key == "current-turn"
    assert usages[0].generated_tokens == 20
    assert events[-1].event == "done"


@pytest.mark.asyncio
async def test_collection_stream_restores_only_finished_patch_and_sets_repair_gate(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from rcp.runs.turn_collection import stream_collected_turn

    store, record, initial_pid = _failed_turn(tmp_path)
    _journal(initial_pid, patch="completed original patch")
    correction_pid = _next_pass(store, record, initial_pid, "correction.pid")
    _journal(correction_pid, complete=False, patch="never apply this unfinished patch")
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)
    restored = []
    monkeypatch.setattr(
        RemoteRunStage,
        "write_workspace_text",
        lambda self, name, text: restored.append((name, text)),
    )
    execution = SimpleNamespace(
        collection_consumed=False, operation_id=record.operation_id, store=store
    )
    events = [event async for event in stream_collected_turn(execution, _request())]
    assert restored == [("patch.json", "completed original patch")]
    assert execution.collection_patch_error
    assert [event.text for event in events if event.event == "answer"] == [
        "Finished the original work."
    ]
    assert events[-1].event == "done"
    # A collection that succeeded proved the pass stopped, so it must answer the
    # start receipt it read. Leaving it open keeps the stage protected from
    # sweep and projected as live until some later turn reuses the workspace.
    assert store.unresolved_remote_provider_passes("test-host", record.stage_root) == []


def test_collection_accepts_bytes_the_live_pipe_would_have_decoded(tmp_path, monkeypatch):
    """A provider byte that is not valid UTF-8 must not strand a finished turn.

    The live reader decodes its stream with a replacement, so a turn carrying
    such a byte is delivered normally when the link holds. Collection stands in
    for that delivery and cannot be the stricter of the two.
    """

    store, record, pid = _failed_turn(tmp_path)
    root, outcome, events, patch = _journal(pid)
    raw = events.encode() + b'{"type":"item.completed","raw":"\xff"}\n'
    (root / "events.jsonl").write_bytes(raw)
    (root / "outcome.json").write_text(
        json.dumps({**outcome, "events_sha256": hashlib.sha256(raw).hexdigest()})
    )
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)

    collected = read_collected_turn(store, record)

    assert collected.patch == patch
    assert [
        item.text
        for item in replay_collected_events(collected, _request())
        if item.event == "answer"
    ] == ["Finished the original work."]
    # The wire preserves the exact byte so the digest can be checked. What the
    # turn carries onward must still be storable: SQLite encodes text as UTF-8
    # and refuses a lone surrogate, which would fail this turn after its Patch
    # had applied.
    collected.events.encode("utf-8")
    assert "\ufffd" in collected.events


def test_collection_refuses_a_patch_the_live_read_would_have_refused(tmp_path, monkeypatch):
    """A patch that is not UTF-8 stops here, as it would when read live.

    `RemoteRunStage.read_workspace_text` decodes the live patch strictly, so no
    delivered turn reaches Apply with replacement characters standing in for
    graph text. Collection must not be the looser of the two paths.
    """

    store, record, pid = _failed_turn(tmp_path)
    root, outcome, _events, _patch = _journal(pid)
    raw = b'{"ops":[{"note":"\xff"}]}\n'
    (root / "patch.json").write_bytes(raw)
    (root / "outcome.json").write_text(
        json.dumps({**outcome, "patch_sha256": hashlib.sha256(raw).hexdigest()})
    )
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)

    with pytest.raises(ValueError, match="not UTF-8"):
        read_collected_turn(store, record)


def test_collected_failure_reports_the_reason_the_provider_declared(tmp_path, monkeypatch):
    """A protocol failure says why in its terminal event, not in stderr.

    The live pipe decoded that event and showed it. A turn whose uplink went
    quiet keeps the same event in its journal, so the human collecting it must
    get the same sentence rather than a generic note that output is incomplete.
    """

    store, record, pid = _failed_turn(tmp_path)
    root, outcome, events, _patch = _journal(pid, complete=False)
    raw = (
        events + json.dumps({"type": "turn.failed", "error": "the model refused"}) + "\n"
    ).encode()
    (root / "events.jsonl").write_bytes(raw)
    (root / "outcome.json").write_text(
        json.dumps({**outcome, "events_sha256": hashlib.sha256(raw).hexdigest()})
    )
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)

    collected = read_collected_turn(store, record)

    assert "the model refused" in incomplete_collection_text(collected)


def test_an_app_server_turn_that_failed_says_so_after_the_link_went(tmp_path, monkeypatch):
    """The turn object the server wrote is the only record left, and it is read."""

    store, record, pid = _failed_turn(tmp_path)
    root, outcome, events, _patch = _journal(pid, complete=False)
    raw = (
        events
        + json.dumps(
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "turn-1",
                        "status": "failed",
                        "error": {"message": "the sandbox denied the write"},
                    }
                },
            }
        )
        + "\n"
    ).encode()
    (root / "events.jsonl").write_bytes(raw)
    (root / "outcome.json").write_text(
        json.dumps(
            {
                **outcome,
                "runtime_id": "codex.app-server-stdio.v1",
                "events_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    )
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)

    collected = read_collected_turn(store, record)

    assert "the sandbox denied the write" in incomplete_collection_text(collected)


def test_collected_failure_reports_the_provider_stderr(tmp_path, monkeypatch):
    """A collected failure says what the live pipe would have said."""

    store, record, pid = _failed_turn(tmp_path)
    root, *_ = _journal(pid, complete=False)
    (root / "stderr.txt").write_text("codex: fatal: the model refused the request\n")
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)

    events = replay_collected_events(read_collected_turn(store, record), _request())

    assert [item.event for item in events] == ["error"]
    assert "the model refused the request" in events[0].text


def test_unbindable_journal_withdraws_the_offer_without_failing_the_projection(tmp_path):
    """One broken record must not take a whole task listing down with it.

    `can_collect` runs for every task in a projection, so a journal that lost
    its stage binding withdraws the offer. The action path still refuses loudly.
    """

    store, record, _pid = _failed_turn(tmp_path)
    elsewhere = tmp_path / "other-stage"
    elsewhere.mkdir(mode=0o700)
    # A later stage checkpoint moves the task off the stage its recorded pass
    # still names, which is how the two can disagree in the first place.
    store.checkpoint_agent_task(
        record.operation_id, stage_host="test-host", stage_root=str(elsewhere)
    )
    record = store.agent_task(record.operation_id)
    assert record is not None

    assert can_collect(store, record) is False
    with pytest.raises(ValueError, match="lost its stage binding"):
        read_collected_turn(store, record)


def test_collection_refuses_an_episode_whose_retained_context_is_gone():
    """Collection settles an episode turn, so it owes the same context judgement.

    Retry and Resume get this through their launch preflight. Collection skips
    that preflight deliberately, because it admits no provider -- but it still
    settles the turn, so a lineage that can no longer say which episode it
    belongs to must refuse here as it would there. The Stop a human already
    requested is settled on the way out, exactly as the preflight does.
    """

    from types import SimpleNamespace

    from rcp.runs.experiment_recovery import require_experiment_episode_context

    settled: list[str] = []
    diagnostics: list[str] = []
    store = SimpleNamespace(
        experiment_episode_recovery_context_problem=lambda _op: "context candidate is invalid",
        record_experiment_episode_diagnostic=lambda **kwargs: diagnostics.append(
            kwargs["diagnostic"]
        ),
        experiment_episode=lambda _episode_id: SimpleNamespace(
            episode_id="episode-1", stop_requested_at="2026-09-16T00:00:00Z", graph_target=None
        ),
        settle_experiment_loop_stop=lambda *_a, **kwargs: settled.append(kwargs["episode_id"]),
    )
    tasks = SimpleNamespace(store=store)
    record = SimpleNamespace(operation_id="original", project_id="project")
    loop_request = RunRequest(
        provider="codex",
        run_on="laptop",
        chat_scope="node",
        node_id="exp/thing",
        chat_id="chat-1",
        message="Continue the bounded Experiment loop.",
        mode="work",
        patch_kind="experiment_loop",
        control_node_id="exp/thing",
        control_episode_id="episode-1",
    )
    with pytest.raises(ValueError, match="context candidate is invalid"):
        require_experiment_episode_context(tasks, record, request=loop_request)
    assert diagnostics == ["context candidate is invalid"]
    assert settled == ["episode-1"]


def test_context_judgement_ignores_a_turn_that_is_not_an_episode():
    from types import SimpleNamespace

    from rcp.runs.experiment_recovery import require_experiment_episode_context

    store = SimpleNamespace(
        experiment_episode_recovery_context_problem=lambda _op: pytest.fail(
            "A plain chat turn has no episode context to judge"
        )
    )
    require_experiment_episode_context(
        SimpleNamespace(store=store),
        SimpleNamespace(operation_id="original", project_id="project"),
        request=RunRequest(provider="codex", run_on="laptop", chat_id="chat-1", message="hi"),
    )


def test_a_running_provider_reports_its_silence_and_opens_the_stop(tmp_path, monkeypatch):
    """Working and wedged look identical, so give the human the number and the control.

    A live group producing nothing is all collection can see. How long it has
    been producing nothing is the one thing that separates a long tool call from
    a wedge, and only the human knows which this turn should be. The sighting is
    recorded so the stop can be offered on this task without probing every task.
    """

    store, record, pid = _failed_turn(tmp_path)
    _journal(pid)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: False)
    monkeypatch.setattr(
        AgentProcessControl,
        "remote_provider_sighting",
        lambda *_: {"idle_seconds": 11_520.0, "identity": "boot:904821"},
    )

    assert not can_stop_remote_provider(store, record)
    with pytest.raises(CollectionPending, match="3h 12m"):
        read_collected_turn(store, record)

    assert can_stop_remote_provider(store, store.agent_task(record.operation_id))
    # The sighting marks the condition, not each look at it.
    read_pending = pytest.raises(CollectionPending)
    with read_pending:
        read_collected_turn(store, record)
    assert [item.category for item in store.agent_task_receipts(record.operation_id)].count(
        "remote_provider_still_running"
    ) == 1


def test_the_stop_is_withdrawn_once_the_pass_is_confirmed_stopped(tmp_path, monkeypatch):
    """Nothing should offer to stop a provider that has already gone."""

    store, record, pid = _failed_turn(tmp_path)
    _journal(pid)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: False)
    monkeypatch.setattr(
        AgentProcessControl, "remote_provider_sighting", lambda *_: {"identity": "boot:904821"}
    )
    with pytest.raises(CollectionPending):
        read_collected_turn(store, record)
    record = store.agent_task(record.operation_id)
    assert can_stop_remote_provider(store, record)

    store.finish_remote_provider_pass(record.operation_id, str(pid))

    assert not can_stop_remote_provider(store, store.agent_task(record.operation_id))


def test_provider_silence_reads_in_units_a_human_acts_on():
    assert describe_provider_silence(12.0) == "under a minute"
    assert describe_provider_silence(300.0) == "5m"
    assert describe_provider_silence(3_660.0) == "1h 01m"


def test_collection_is_not_offered_for_a_routed_child_work_turn(tmp_path):
    """An Auto-research worker is an ordinary Work chat everywhere but its route.

    Collection would adopt it as one: no attempt row, and the route still naming
    the failed turn as current, so the episode would wait on a worker that had
    already been replaced. Its own resume path knows the route; this one does
    not, so the offer is withdrawn rather than answered wrongly.
    """

    from tests.test_auto_research_children_storage import _auto_parent, _project, _work_pair

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    episode, root = _auto_parent(store)
    route, task = _work_pair(store, episode, root, worker_id="worker-one")
    store.create_auto_research_child_work(route, task)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    pid = stage / "agent.pid"
    store.checkpoint_agent_task(task.operation_id, stage_host="test-host", stage_root=str(stage))
    store.begin_remote_provider_pass(
        task.operation_id, "test-host", str(stage), str(pid), journaled=True
    )
    store.fail_agent_task(task.operation_id, "Provider ended", failure_kind="transport_lost")
    failed = store.agent_task(task.operation_id)
    assert failed is not None and failed.failure_kind == "transport_lost"
    assert store.auto_research_child_work_for_operation(task.operation_id) is not None
    assert can_collect(store, failed) is False


def test_collection_ignores_an_event_the_live_pipe_would_have_omitted():
    """The journal keeps an oversized event; neither reader is allowed to decode it.

    The wrapper stops feeding a line to its protocol reader once it passes the
    per-event limit, and the live pipe drops the same line and reports the
    omission instead of parsing it. Only the journal keeps the bytes, so reading
    them back as events would let a collected turn act on one the live turn had
    already refused -- here, adopting a provider session the human never saw.
    """

    oversized = json.dumps(
        {
            "type": "thread.started",
            "thread_id": "never-delivered",
            "pad": "x" * TURN_JOURNAL_MAX_EVENT_BYTES,
        }
    )
    delivered = json.dumps({"type": "thread.started", "thread_id": "native-thread"})
    collected = CollectedTurn(
        source_operation_id="original",
        pid_file="/stage/agent.pid",
        outcome={"provider": "codex", "runtime_id": "codex.exec-json.v1"},
        events="\n".join((oversized, delivered)),
        patch=None,
    )

    assert collected.observed_events == [delivered]
    assert collected.session_id == "native-thread"


def test_an_unidentified_provider_is_never_offered_a_delayed_stop(tmp_path, monkeypatch):
    """A stop RCP cannot aim is not a stop it should offer.

    The control is drawn from a sighting that may be hours old. Sending it needs
    proof that the pid still names the process that was seen, and a host that
    could not say which process that was can never supply it.
    """

    store, record, pid = _failed_turn(tmp_path)
    _journal(pid)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: False)
    monkeypatch.setattr(
        AgentProcessControl, "remote_provider_sighting", lambda *_: {"idle_seconds": 60.0}
    )
    with pytest.raises(CollectionPending):
        read_collected_turn(store, record)

    current = store.agent_task(record.operation_id)
    assert store.agent_task_has_receipt(record.operation_id, "remote_provider_still_running")
    assert recorded_provider_identity(store, current, str(pid)) is None
    assert not can_stop_remote_provider(store, current)


def test_the_latest_pass_is_found_past_the_receipt_display_ceiling(tmp_path):
    """Collection must read every pass, not the page a projection would show.

    `agent_task_receipts` returns the oldest receipts up to a display ceiling. A
    turn that ran enough compute commands to fill it and then opened a
    correction pass would hide that pass behind them, and collection would adopt
    the journal of the pass the correction had already replaced.
    """

    from rcp.limits import AGENT_TASK_RECEIPT_LIST_LIMIT

    store, record, pid = _failed_turn(tmp_path)
    for index in range(AGENT_TASK_RECEIPT_LIST_LIMIT):
        # Protected categories, because retention keeps exactly these: a turn
        # reaches the ceiling through its own compute commands, not through
        # diagnostics that get pruned.
        store.record_agent_task_receipt(
            record.operation_id, "compute_command_started", {"index": index}, tier="summary"
        )
    correction = _next_pass(store, record, pid, "agent.pid.1")

    assert len(store.agent_task_receipts(record.operation_id)) == AGENT_TASK_RECEIPT_LIST_LIMIT
    assert journal_pid_files(store, record) == [str(pid), str(correction)]
    assert journal_pid_file(store, record) == str(correction)


def test_a_collected_retry_refuses_the_patch_it_only_inherited(tmp_path):
    """A Retry reuses its stage, so the file it starts with is the last attempt's.

    Live settlement compares against the baseline the attempt recorded and
    refuses a Patch the turn merely inherited. Collection replays that same turn
    and cannot recompute the comparison -- by then the stage holds one file and
    nothing says who wrote it -- so it reads what the attempt wrote down.
    Otherwise a retry that wrote no Patch applies its predecessor's.
    """

    from rcp.runs.tasks.work import _capture_retry_deliverable_baseline

    store, record, pid = _failed_turn(tmp_path)
    attempt = store.create_agent_task(
        record.model_copy(
            update={"operation_id": "retry-attempt", "parent_operation_id": record.operation_id}
        ),
        continuation_cause="retry",
    )
    child = store.create_agent_task(
        attempt.model_copy(
            update={"operation_id": "collection", "parent_operation_id": attempt.operation_id}
        ),
        continuation_cause="collect",
    )
    turn = SimpleNamespace(
        continuation="collect",
        retrying=False,
        execution=SimpleNamespace(store=store, operation_id=child.operation_id),
    )

    # Nothing recorded yet: collection must not invent a baseline.
    assert _capture_retry_deliverable_baseline(turn).patch_digest is None

    inherited = hashlib.sha256(b'{"operations": []}').hexdigest()
    store.record_agent_task_receipt(
        attempt.operation_id,
        "retry_deliverable_baseline",
        {"patch_sha256": inherited, "watch_sha256": None, "experiment_watch_sha256": {"out": "d"}},
        tier="summary",
    )

    baseline = _capture_retry_deliverable_baseline(turn)
    assert baseline.patch_digest == inherited
    assert baseline.experiment_watch_digests == {"out": "d"}


def test_a_reservation_that_never_launched_is_retried_not_collected(tmp_path):
    """A pass is written down before its SSH command runs, so one can outlive nothing.

    RCP stopping inside that window leaves a start receipt with no wrapper, no
    pidfile and no turn. Every probe of it answers unknown, so offering
    collection would keep rescheduling the same attempt and divert the ordinary
    Retry that would actually recover the work.
    """

    store = AppStore(tmp_path / "state.sqlite3")
    root = tmp_path / "stage"
    root.mkdir(mode=0o700)
    pid = root / "agent.pid"
    record = store.create_agent_task(
        AgentTaskRecord(
            operation_id="reserved",
            project_id="project",
            kind="project_chat",
            status="running",
            request={"provider": "codex", "mode": "work"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Running",
            stage_host="test-host",
            stage_root=str(root),
            native_session_id="native-thread",
        )
    )
    store.begin_remote_provider_pass(
        record.operation_id, "test-host", str(root), str(pid), journaled=True
    )
    store.interrupt_active_agent_tasks()
    reserved = store.agent_task(record.operation_id)
    assert not can_collect(store, reserved)

    # The same reservation, once its turn was actually handed over.
    store.deliver_remote_provider_pass(record.operation_id, str(pid))
    assert can_collect(store, store.agent_task(record.operation_id))


def test_a_correction_caught_before_its_launch_is_not_the_pass_collection_adopts(tmp_path):
    """One task opens several passes, so the proof of delivery has to name one.

    An operational pass that completed and a correction pass a stop caught
    mid-launch look identical from the task: both are reservations on the same
    turn. Choosing the latest would pick a pidfile no wrapper ever wrote, and
    every probe of it answers unknown while Retry keeps diverting here.
    """

    store, record, operational = _failed_turn(tmp_path)
    correction = operational.with_name("correction.pid")
    store.finish_remote_provider_pass(record.operation_id, str(operational))
    store.begin_remote_provider_pass(
        record.operation_id, record.stage_host, record.stage_root, str(correction), journaled=True
    )

    assert journal_pid_files(store, record) == [str(operational)]
    assert journal_pid_file(store, record) == str(operational)

    # The same correction, once its own turn was handed over.
    store.deliver_remote_provider_pass(record.operation_id, str(correction))
    assert journal_pid_file(store, record) == str(correction)


def test_the_stop_identity_is_found_past_the_receipt_display_ceiling(tmp_path):
    """A wedged provider must stay stoppable however much the turn recorded.

    The sighting that authorizes a delayed stop is written after everything the
    turn did. Read through the oldest-first display page, a turn that filled it
    with its own compute commands would hide that sighting, and the Stop control
    would be withheld from the one provider that still needs it.
    """

    from rcp.limits import AGENT_TASK_RECEIPT_LIST_LIMIT

    store, record, pid = _failed_turn(tmp_path)
    for index in range(AGENT_TASK_RECEIPT_LIST_LIMIT):
        store.record_agent_task_receipt(
            record.operation_id, "compute_command_started", {"index": index}, tier="summary"
        )
    store.record_agent_task_receipt(
        record.operation_id,
        "remote_provider_still_running",
        {"pid_file": str(pid), "identity": "boot:4242"},
        tier="summary",
    )

    assert not any(
        receipt.category == "remote_provider_still_running"
        for receipt in store.agent_task_receipts(record.operation_id)
    )
    current = store.agent_task(record.operation_id)
    assert recorded_provider_identity(store, current, str(pid)) == "boot:4242"
    assert can_stop_remote_provider(store, current)


def test_an_answer_carrying_a_line_separator_survives_replay(tmp_path, monkeypatch):
    """The journal's records end where the writer ended them, and nowhere else.

    A provider that does not escape non-ASCII can put U+2028 inside an answer,
    and both the live reader and the journal still delimit events with one byte.
    Splitting the retained text the way Python splits lines breaks that event in
    two, and the decoder is handed two fragments of invalid JSON: the turn then
    settles as completed, applying its Patch, with no answer to show for it.
    """

    answer = "First finding.\u2028Second finding."
    store, record, pid = _failed_turn(tmp_path)
    _journal(pid, answer=answer, ensure_ascii=False)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: True)

    collected = read_collected_turn(store, record)
    assert "\u2028" in collected.events
    events = replay_collected_events(collected, _request())

    assert [item.text for item in events if item.event == "answer"] == [answer]
    assert events[-1].event == "done"


def test_a_sighting_that_could_not_name_the_process_is_not_the_last_word(tmp_path, monkeypatch):
    """One probe failing must not withdraw the Stop control for good.

    The sighting is recorded once so the control can be offered without probing
    every task in a listing. Recorded from a probe that answered without naming
    the process, it holds a place the identity was meant to fill, and every
    later probe skips it -- so a wedged provider stays unstoppable however many
    times RCP goes back and succeeds.
    """

    store, record, pid = _failed_turn(tmp_path)
    _journal(pid)
    _local_transport(monkeypatch)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_: False)
    monkeypatch.setattr(
        AgentProcessControl, "remote_provider_sighting", lambda *_: {"idle_seconds": 60.0}
    )
    with pytest.raises(CollectionPending):
        read_collected_turn(store, record)
    assert not can_stop_remote_provider(store, store.agent_task(record.operation_id))

    # The next probe reaches the host and names the process it found.
    monkeypatch.setattr(
        AgentProcessControl,
        "remote_provider_sighting",
        lambda *_: {"idle_seconds": 120.0, "identity": "boot:517"},
    )
    with pytest.raises(CollectionPending):
        read_collected_turn(store, record)

    current = store.agent_task(record.operation_id)
    assert recorded_provider_identity(store, current, str(pid)) == "boot:517"
    assert can_stop_remote_provider(store, current)
