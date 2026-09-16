from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from rcp.agents.launcher import AgentProcessControl
from rcp.providers import ProviderTurnRequest
from rcp.runs.turn_collection import (
    CollectedTurn,
    CollectionPending,
    can_collect,
    read_collected_turn,
    replay_collected_events,
)
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
):
    root = Path(str(pid) + ".turn")
    root.mkdir()
    events = (
        "\n".join(
            json.dumps(value)
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
    store.checkpoint_agent_task_runtime(
        record.operation_id, provider="codex", runtime_id="codex.exec-json.v1"
    )
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
