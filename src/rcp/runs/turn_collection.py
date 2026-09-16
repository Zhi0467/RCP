from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl, _meaningful_stderr
from rcp.limits import TURN_JOURNAL_MAX_EVENT_BYTES
from rcp.providers import ProviderTurnRequest, profile_for, require_runtime_id
from rcp.transport import RemoteRunStage, StateUnavailable, remote_turn_journal

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.storage import AgentTaskRecord, AppStore


class CollectionPending(ValueError):
    """The original provider is alive or its host cannot yet be reached."""


def collection_source(store: AppStore, record: AgentTaskRecord) -> AgentTaskRecord:
    seen: set[str] = set()
    while store.agent_task_continuation_cause(record.operation_id) == "collect":
        if record.operation_id in seen or not record.parent_operation_id:
            raise ValueError("Collection has no valid original provider turn.")
        seen.add(record.operation_id)
        parent = store.agent_task(record.parent_operation_id)
        if parent is None or (
            parent.project_id != record.project_id
            or parent.kind != record.kind
            or parent.graph_target != record.graph_target
            or parent.stage_root != record.stage_root
            or parent.stage_host != record.stage_host
        ):
            raise ValueError("Collection changed its original task binding.")
        record = parent
    return record


def projected_chat_turn_operation_id(store: AppStore, record: AgentTaskRecord) -> str:
    """Which chat turn a projection points at; a broken chain points at itself.

    Same reason as `can_collect`: a listing must render every task it holds.
    """

    try:
        return collection_source(store, record).operation_id
    except ValueError:
        return record.operation_id


def collected_task_operation_id(execution: AgentTaskExecution) -> str:
    if execution.continuation != "collect":
        return execution.operation_id
    record = execution.store.agent_task(execution.operation_id)
    if record is None:
        raise ValueError("The collection task is missing.")
    return collection_source(execution.store, record).operation_id


def journal_pid_files(store: AppStore, record: AgentTaskRecord) -> list[str]:
    record = collection_source(store, record)
    result = []
    for receipt in store.remote_provider_start_receipts(record.operation_id):
        payload = receipt.payload
        if payload.get("journal_version") != 1:
            return []
        pid = payload.get("pid_file")
        if not isinstance(pid, str) or PurePosixPath(pid).parent != PurePosixPath(
            record.stage_root or ""
        ):
            raise ValueError("The provider journal lost its stage binding.")
        result.append(pid)
    return result


def journal_pid_file(store: AppStore, record: AgentTaskRecord) -> str | None:
    passes = journal_pid_files(store, record)
    return passes[-1] if passes else None


def collectible_surface(store: AppStore, record: AgentTaskRecord) -> bool:
    """Whether a finished turn on this task has a route that would adopt it.

    Asked twice, and the two answers must not differ. `can_collect` asks it of a
    turn that already failed, to decide whether to offer collection. A launch
    asks it of a turn still running, to decide whether losing the transport
    should leave the remote provider alive for that offer. One answer is what
    stops RCP preserving a provider nothing will ever collect: the stage fence
    would then block recovery while it ran, with no Stop control to end it, and
    discard its journal once it exited.

    An Auto-research child Work attempt is excluded despite being a Work chat.
    Collection would adopt it through the ordinary chat route, recording no
    attempt row and leaving the worker route pointed at the failed turn, so the
    episode would never see that worker finish. Its own resume path stays
    route-aware; do not build a second way in.
    """

    return bool(
        (
            record.kind == "auto_research"
            or (
                record.kind in {"node_chat", "project_chat"}
                and record.request.get("mode") == "work"
            )
        )
        and store.auto_research_child_work_for_operation(record.operation_id) is None
    )


def can_collect(store: AppStore, record: AgentTaskRecord) -> bool:
    """Offer collection, and never fail a listing for one unbindable record.

    This runs for every task in a projection. A broken continuation chain or a
    journal that lost its stage binding must withdraw the offer, not raise
    through the response. `read_collected_turn` re-checks the same bindings and
    still refuses loudly, so a human who asks anyway is told why.
    """

    try:
        return _can_collect(store, record)
    except ValueError:
        return False


def _can_collect(store: AppStore, record: AgentTaskRecord) -> bool:
    return bool(
        not record.history_only
        and not store.agent_task_has_receipt(record.operation_id, "provider_collection_incomplete")
        and (
            record.status == "interrupted"
            or (
                record.status == "failed"
                and (
                    record.failure_kind == "transport_lost"
                    or store.agent_task_continuation_cause(record.operation_id) == "collect"
                )
            )
        )
        and record.stage_host
        and record.stage_root
        and collectible_surface(store, collection_source(store, record))
        and not store.agent_task_has_continuation(record.operation_id)
        and journal_pid_file(store, record)
    )


def recorded_provider_identity(
    store: AppStore, source: AgentTaskRecord, pid_file: str
) -> str | None:
    """Which process RCP wrote down when it last saw this group alive.

    A stop is only safe to send while the pid still names that process, so a
    sighting that could not say which one it was does not authorize one later.
    """

    for receipt in store.agent_task_receipts(source.operation_id):
        if (
            receipt.category == "remote_provider_still_running"
            and receipt.payload.get("pid_file") == pid_file
        ):
            identity = receipt.payload.get("identity")
            if isinstance(identity, str) and identity:
                return identity
    return None


def can_stop_remote_provider(store: AppStore, record: AgentTaskRecord) -> bool:
    """Offer the stop only where RCP has actually watched a provider outlive its turn.

    Probing here would cost one SSH round trip per task in a listing, so this
    reads the sighting a collection attempt already recorded. It withdraws as
    soon as that pass is confirmed stopped, by this control or by anything else.
    """

    try:
        if not _can_collect(store, record):
            return False
        source = collection_source(store, record)
        pid_file = journal_pid_file(store, source)
        if pid_file is None or recorded_provider_identity(store, source, pid_file) is None:
            return False
        return (source.operation_id, pid_file) in set(
            store.unresolved_remote_provider_passes(
                source.stage_host or "", source.stage_root or ""
            )
        )
    except ValueError:
        return False


def _within_event_limit(line: str) -> bool:
    # A UTF-8 character is at most four bytes, so a short line is under the
    # limit without measuring; only a long one is worth encoding to find out.
    return (
        len(line) * 4 <= TURN_JOURNAL_MAX_EVENT_BYTES
        or len(line.encode("utf-8", "surrogateescape")) <= TURN_JOURNAL_MAX_EVENT_BYTES
    )


@dataclass(frozen=True)
class CollectedTurn:
    source_operation_id: str
    pid_file: str
    outcome: dict[str, object]
    events: str
    patch: str | None
    passes: tuple[CollectedTurn, ...] = ()
    correction_error: str | None = None
    stderr: str = ""

    @property
    def complete(self) -> bool:
        return bool(
            self.outcome.get("protocol_complete") is True
            and self.outcome.get("journal_complete") is True
            and not self.outcome.get("error")
            and not self.outcome.get("stopped")
        )

    @property
    def observed_events(self) -> list[str]:
        """The journal lines the live pipe would have decoded, in order.

        Both observers stop at the per-event limit: the wrapper refuses to feed
        an over-long line to its protocol reader, and the live reader drops the
        same line and notes the omission instead of parsing it. Only the journal
        keeps the bytes, because it is the forensic record. Reading them back as
        events would let collection act on one the live turn had refused.
        """

        return [line for line in self.events.splitlines() if _within_event_limit(line)]

    @property
    def session_id(self) -> str | None:
        root = self.outcome.get("root_thread_id")
        if isinstance(root, str) and root:
            return root
        provider = self.outcome.get("provider")
        if not isinstance(provider, str):
            return None
        profile = profile_for(provider)
        for line in self.observed_events:
            try:
                decoded = profile.decode_event(json.loads(line), line)
            except (ValueError, TypeError):
                continue
            if decoded.session_id:
                return decoded.session_id
        return None


def describe_provider_silence(seconds: float) -> str:
    """How long a live provider has produced nothing, in units a human reads."""

    minutes = int(seconds) // 60
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _observe_running_provider(store: AppStore, source: AgentTaskRecord, pid_file: str) -> str:
    """Report what a still-live provider looks like, and remember having seen it.

    A working provider and a wedged one present identically from here: a process
    group that is alive and producing nothing. The one number that separates them
    is how long the silence has run, and only the human knows whether this turn
    was meant to be quiet for that long. So report it rather than judge it.

    Recording the sighting is what lets the stop control be offered on this task
    alone, instead of probing every task in a listing to find out.
    """

    sighting: dict[str, object] = {}
    with suppress(Exception):
        sighting = AgentProcessControl.remote_provider_sighting(source.stage_host or "", pid_file)
    idle = sighting.get("idle_seconds")
    with suppress(Exception):
        # Once is enough: this marks that a provider outlived its turn here, and
        # the reading a human acts on is the one in the message below. Recording
        # every attempt would grow a category that retention deliberately keeps.
        if not store.agent_task_has_receipt(source.operation_id, "remote_provider_still_running"):
            store.record_agent_task_receipt(
                source.operation_id,
                "remote_provider_still_running",
                # The identity is what a stop sent long after this sighting is
                # held to, so it is written down with the sighting that offers it.
                {
                    "pid_file": pid_file,
                    "idle_seconds": idle,
                    "identity": sighting.get("identity"),
                },
            )
    text = "The original provider is still running; RCP will collect it when it finishes."
    if isinstance(idle, (int, float)):
        text += f" It has written nothing for {describe_provider_silence(float(idle))}."
    return text


def read_collected_turn(store: AppStore, record: AgentTaskRecord) -> CollectedTurn:
    source = collection_source(store, record)
    pid_files = journal_pid_files(store, source)
    if not pid_files or not source.stage_host or not source.stage_root:
        raise ValueError("This turn has no durable provider journal to collect.")
    stage = RemoteRunStage(source.stage_host).attach(source.stage_root)
    # Never replace shared scratch while the last correction may still write it.
    # Earlier passes were settled before this protected last start was admitted.
    stopped = AgentProcessControl.remote_stopped(source.stage_host, pid_files[-1])
    if stopped is False:
        raise CollectionPending(_observe_running_provider(store, source, pid_files[-1]))
    if stopped is not True:
        raise CollectionPending(
            "The provider host is unavailable; collection is waiting for it to return."
        )
    passes = tuple(_read_pass(stage, source, pid) for pid in pid_files)
    completed = [item for item in passes if item.complete]
    if not completed:
        return passes[-1]
    operational = completed[0]
    operational_index = passes.index(operational)
    # A pre-prompt runtime fallback may precede the real turn. Every pass after
    # prompt acceptance retains that native session and pinned runtime.
    for item in passes[operational_index:]:
        if item.session_id and item.session_id != operational.session_id:
            raise ValueError("The provider correction changed its captured native session.")
        runtime_id = item.outcome.get("runtime_id")
        if runtime_id and runtime_id != operational.outcome.get("runtime_id"):
            raise ValueError("The provider correction changed its captured runtime.")
        if item.complete and source.runtime_id and runtime_id != source.runtime_id:
            raise ValueError("The provider journal belongs to a different invocation.")
    latest = completed[-1]
    correction_error = None
    if not passes[-1].complete:
        correction_error = str(
            passes[-1].outcome.get("error")
            or "The graph correction stopped before completing. The last completed Patch is retained for repair."
        )
    patch = latest.patch
    if latest is not operational and patch is None:
        previous_patch = next(
            (item.patch for item in reversed(completed[:-1]) if item.patch is not None), None
        )
        if previous_patch is not None:
            patch = previous_patch
            correction_error = (
                correction_error
                or "The correction completed without writing patch.json. The last completed Patch is retained for repair."
            )
    return CollectedTurn(
        source.operation_id,
        pid_files[-1],
        operational.outcome,
        operational.events,
        patch,
        passes,
        correction_error,
        operational.stderr,
    )


def _read_pass(stage: RemoteRunStage, source: AgentTaskRecord, pid_file: str) -> CollectedTurn:
    # Each entry is independently bounded by the journal writer. The transport
    # enforces the same overall ceiling before decoding anything into memory.
    from rcp.limits import TURN_JOURNAL_MAX_BYTES

    result = stage.run_shipped_module(remote_turn_journal, pid_file, str(TURN_JOURNAL_MAX_BYTES))
    if result.returncode == 255:
        raise CollectionPending("The provider host became unavailable during collection.")
    if result.returncode:
        raise StateUnavailable(result.stderr.strip() or "The provider journal cannot be read.")
    document = json.loads(result.stdout)
    if document.get("missing"):
        return CollectedTurn(
            source.operation_id,
            pid_file,
            {
                "protocol_complete": False,
                "journal_complete": False,
                "error": "The provider stopped without a durable completion receipt.",
            },
            "",
            None,
        )
    outcome = document.get("outcome")
    events = document.get("events")
    patch = document.get("patch")
    errors = document.get("stderr")
    if (
        not isinstance(outcome, dict)
        or not isinstance(events, str)
        or (patch is not None and not isinstance(patch, str))
    ):
        raise ValueError("The provider journal is malformed.")
    if (
        outcome.get("version") != 1
        or outcome.get("pid_file") != pid_file
        or outcome.get("provider") != source.request.get("provider")
    ):
        raise ValueError("The provider journal belongs to a different invocation.")
    require_runtime_id(str(outcome.get("provider")), str(outcome.get("runtime_id")))
    for field, content in (("events_sha256", events), ("patch_sha256", patch)):
        # `surrogateescape` on both sides recovers the writer's exact bytes,
        # including any the provider emitted that are not valid UTF-8.
        if (
            content is not None
            and outcome.get(field)
            != hashlib.sha256(content.encode("utf-8", "surrogateescape")).hexdigest()
        ):
            raise ValueError("The provider journal changed after completion.")
    return CollectedTurn(
        source.operation_id,
        pid_file,
        outcome,
        _decoded_like_the_live_stream(events),
        _decoded_like_the_live_patch(patch),
        stderr=_decoded_like_the_live_stream(errors) if isinstance(errors, str) else "",
    )


def _decoded_like_the_live_stream(text: str) -> str:
    """Re-decode verified journal bytes the way the live pipe decodes a stream.

    The wire round-trips exact bytes so the writer's digest can be checked, which
    leaves a lone surrogate wherever the provider emitted a byte that is not
    UTF-8. Nothing downstream survives one: SQLite refuses to store it, so the
    turn would fail late, after its Patch had already applied.
    """

    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _decoded_like_the_live_patch(patch: str | None) -> str | None:
    """Refuse a patch that is not UTF-8, exactly as reading it live would.

    `RemoteRunStage.read_workspace_text` decodes the live patch strictly, so a
    delivered turn never reaches Apply with replacement characters standing in
    for graph text. Collection stands in for that delivery, not for a looser one.
    """

    if patch is None:
        return None
    try:
        return patch.encode("utf-8", "surrogateescape").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The provider's patch.json is not UTF-8 text.") from exc


def _declared_failure_reason(collected: CollectedTurn) -> str | None:
    """The provider's own words for a turn it ended without completing.

    A protocol-declared failure names its reason in the terminal event, not in
    stderr and not in the wrapper's error field, and the live pipe showed that
    event. Ask the decoder that owns the runtime's wording rather than matching
    event shapes here. The app-server runtime states this through a turn object
    that needs the live request this settlement does not have, so it keeps the
    general message.
    """

    outcome = collected.outcome
    if str(outcome.get("runtime_id") or "") == "codex.app-server-stdio.v1":
        return None
    try:
        profile = profile_for(str(outcome.get("provider") or ""))
    except (KeyError, ValueError):
        return None
    for line in reversed(collected.observed_events):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        decoded = profile.decode_event(value, line)
        if decoded.event == "error" and decoded.text:
            return decoded.text
    return None


def incomplete_collection_text(collected: CollectedTurn) -> str:
    """What a stopped pass that never completed tells the human.

    Two readers settle this same turn -- the preflight that fails the task and
    the replay behind it -- and a human must not get a different answer from
    whichever got there first. The live pipe enriches a failure with the
    provider's own stderr, and a collected failure is that same failure.
    """

    text = str(
        collected.outcome.get("error")
        or _declared_failure_reason(collected)
        or "The remote provider stopped before completing this turn. Its retained output is incomplete."
    )
    detail = _meaningful_stderr(collected.stderr)
    if detail and detail not in text:
        return "\n".join((text, detail))
    return text


def replay_collected_events(
    collected: CollectedTurn, request: ProviderTurnRequest
) -> list[AgentEvent]:
    if not collected.complete:
        return [AgentEvent(event="error", text=incomplete_collection_text(collected))]
    events = _decode_pass(collected, request)
    for item in collected.passes:
        if item.pid_file == collected.outcome.get("pid_file"):
            continue
        if item.outcome.get("journal_complete") is True:
            # Correction prose is not the operational answer. Its labelled usage
            # still belongs to the original task's existing deduplication ledger.
            events.extend(
                AgentEvent(event="raw", usage=event.usage)
                for event in _decode_pass(item, request)
                if event.usage is not None
            )
    events.append(AgentEvent(event="done"))
    return events


def _decode_pass(collected: CollectedTurn, request: ProviderTurnRequest) -> list[AgentEvent]:
    outcome = collected.outcome
    request = dataclasses.replace(request, provider_version=outcome.get("provider_version"))
    provider = str(outcome["provider"])
    runtime_id = str(outcome["runtime_id"])
    require_runtime_id(provider, runtime_id)
    profile = profile_for(provider)
    events: list[AgentEvent] = [AgentEvent(event="runtime", text=runtime_id)]
    # JSONL profiles already own labelled answers and usage. Their live turn
    # object additionally owns steering; collection never replays steering.
    if runtime_id != "codex.app-server-stdio.v1":
        for line in collected.observed_events:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            decoded = profile.decode_event(value, line)
            events.append(
                AgentEvent(
                    event=decoded.event,
                    text=decoded.text,
                    session_id=decoded.session_id,
                    usage=decoded.usage,
                )
            )
    else:
        turn = profile.runtime(runtime_id).turn(request)
        turn.initial_input()
        for line in collected.observed_events:
            if line.startswith("RCP_COMMAND_BROKER_READY:"):
                continue
            # Steering responses were already observed by the live pipe; no
            # replacement interactive session exists to acknowledge them.
            try:
                value = json.loads(line)
            except ValueError:
                value = None
            if isinstance(value, dict) and str(value.get("id", "")).startswith("steer:"):
                continue
            step = turn.receive_line(line)
            events.extend(
                AgentEvent(
                    event=item.event, text=item.text, session_id=item.session_id, usage=item.usage
                )
                for item in step.events
            )
    return events


async def stream_collected_turn(
    execution: AgentTaskExecution,
    request: ProviderTurnRequest,
) -> AsyncIterator[AgentEvent]:
    if execution.collection_consumed:
        raise ValueError("Collection cannot launch or replay a correction turn.")
    execution.collection_consumed = True
    record = execution.store.agent_task(execution.operation_id)
    if record is None:
        raise ValueError("The collection task is missing.")
    collected = await asyncio.to_thread(read_collected_turn, execution.store, record)
    events = replay_collected_events(collected, request)
    execution.collection_patch_error = collected.correction_error
    if not any(event.event == "error" for event in events):
        stage = RemoteRunStage(record.stage_host or "").attach(record.stage_root or "")
        if collected.patch is not None:
            await asyncio.to_thread(stage.write_workspace_text, "patch.json", collected.patch)
        else:
            await asyncio.to_thread(stage.remove_workspace_file, "patch.json")
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "provider_turn_collected",
        {
            "source_operation_id": collected.source_operation_id,
            "pid_file": collected.pid_file,
            "complete": collected.outcome.get("protocol_complete") is True,
            "correction_incomplete": collected.correction_error is not None,
        },
    )
    # `read_collected_turn` refuses unless this pass is proven stopped, so the
    # receipt it left open is now answerable. The incomplete path already closes
    # it; a success that did not would leave the stage protected from sweep and
    # projected as live until some later turn happened to reuse the workspace.
    execution.store.finish_remote_provider_pass(collected.source_operation_id, collected.pid_file)
    for event in events:
        yield event
