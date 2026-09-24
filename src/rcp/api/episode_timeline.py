"""Read-only actor projection; resolve recorded joins before applying the wire bound."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from rcp.api.episode_timeline_models import (
    EpisodeTimelineActor,
    EpisodeTimelineHandoff,
    EpisodeTimelineLinks,
    EpisodeTimelineMark,
    EpisodeTimelineMember,
    EpisodeTimelineMessage,
    EpisodeTimelineResponse,
    EpisodeTimelineSignal,
    EpisodeTimelineSpan,
    EpisodeTimelineText,
)
from rcp.api.episodes import episode_chain_records
from rcp.core.models import AuthorizedHuman
from rcp.limits import (
    EPISODE_TIMELINE_ERROR_MAX_LENGTH,
    EPISODE_TIMELINE_EVENT_LIMIT,
    EPISODE_TIMELINE_HEADLINE_MAX_LENGTH,
    EPISODE_TIMELINE_PREVIEW_MAX_LENGTH,
)
from rcp.storage import AgentTaskRecord, AppStore, EpisodeRecord
from rcp.storage.models import GraphWatcherRecord


def _span(operation_id: str | None) -> str | None:
    return f"span:{operation_id}" if operation_id else None


def _headline(task: AgentTaskRecord) -> str | None:
    if task.kind == "episode_report":
        return None
    messages = (task.result or {}).get("messages")
    if not isinstance(messages, list):
        return None
    # The collector appends the final answer after any trace text, so it is the last entry.
    answer = next(
        (text.strip() for text in reversed(messages) if isinstance(text, str) and text.strip()),
        None,
    )
    if answer is None:
        return None
    return re.split(r"(?<=[.!?])\s+", answer, maxsplit=1)[0][:EPISODE_TIMELINE_HEADLINE_MAX_LENGTH]


def _handoffs(store: AppStore, chain: list[EpisodeRecord]) -> list[EpisodeTimelineHandoff]:
    result = []
    for member in chain:
        for admission in store.auto_research_child_admissions(member.episode_id):
            # A cancelled admission never created its child; its command is no hand-off.
            if admission.state == "cancelled":
                continue
            command = store.auto_research_child_admission_command(admission.admission_id)
            if command is None:
                continue
            snapshot = store.auto_research_command_file(command.command_id)
            if snapshot is None or snapshot.kind not in {"instruction", "goal"}:
                continue
            kind = "worker" if admission.child_kind == "work" else "experiment"
            result.append(
                EpisodeTimelineHandoff(
                    item_id=f"handoff:{command.command_id}",
                    kind="assignment" if snapshot.kind == "instruction" else "goal",
                    from_span_id=f"span:{command.operation_id}",
                    to_actor_id=f"actor:{kind}:{admission.child_id}",
                    at=command.started_at,
                    preview=snapshot.content[:EPISODE_TIMELINE_PREVIEW_MAX_LENGTH],
                    text_ref=f"handoff:{command.command_id}",
                )
            )
    return result


def episode_timeline_text(
    store: AppStore, episode: EpisodeRecord, text_ref: str
) -> EpisodeTimelineText | None:
    chain = episode_chain_records(store, episode)
    prefix, _, record_id = text_ref.partition(":")
    if prefix == "handoff":
        # Only admission-backed assignment/goal snapshots are timeline bodies.
        if not any(item.text_ref == text_ref for item in _handoffs(store, chain)):
            return None
        snapshot = store.auto_research_command_file(record_id)
        if snapshot is not None and snapshot.episode_id in {m.episode_id for m in chain}:
            return EpisodeTimelineText(
                text_ref=text_ref,
                kind="assignment" if snapshot.kind == "instruction" else "goal",
                owner_episode_id=snapshot.episode_id,
                body=snapshot.content,
                sha256=snapshot.sha256,
            )
    elif prefix == "message":
        message = store.auto_research_message(record_id)
        if message is not None and message.episode_id in {m.episode_id for m in chain}:
            return EpisodeTimelineText(
                text_ref=text_ref,
                kind="message",
                owner_episode_id=message.episode_id,
                body=message.body,
                sha256=hashlib.sha256(message.body.encode("utf-8")).hexdigest(),
            )
    return None


def build_episode_timeline(store: AppStore, episode: EpisodeRecord) -> EpisodeTimelineResponse:
    chain = episode_chain_records(store, episode)
    auto = episode.mode == "auto_research"
    primary_kind = "orchestrator" if auto else "agent"
    primary_id = f"actor:{primary_kind}:{chain[0].episode_id}"
    actors: dict[str, EpisodeTimelineActor] = {}
    spans: list[EpisodeTimelineSpan] = []
    messages: list[EpisodeTimelineMessage] = []
    signals: list[EpisodeTimelineSignal] = []
    marks: list[EpisodeTimelineMark] = []
    handoffs = _handoffs(store, chain)
    handoff_by_actor = {item.to_actor_id: item for item in handoffs}
    tasks: dict[str, AgentTaskRecord] = {}
    task_actors: dict[str, str] = {}
    invocations: dict[str, int] = {}
    works = (
        {
            work.worker_id: work
            for member in chain
            for work in store.auto_research_child_works(member.episode_id)
        }
        if auto
        else {}
    )
    routes = (
        {
            route.child_episode_id: route
            for member in chain
            for route in store.auto_research_child_experiments(member.episode_id)
        }
        if auto
        else {}
    )
    children = store.episodes_by_ids(list(routes))
    owners = {member.episode_id: member for member in chain} | children

    def add_actor(actor_id: str, kind: str, label: str, owner: str, **values: object) -> None:
        actors[actor_id] = EpisodeTimelineActor(
            actor_id=actor_id,
            kind=kind,
            label=label,
            row_key=values.pop("row_key", actor_id),
            owner_episode_id=owner,
            **values,
        )

    def human(member: EpisodeRecord, identity: AuthorizedHuman | None) -> str:
        actor_id = f"actor:human:{identity.user_id if identity else member.episode_id}"
        if actor_id not in actors:
            add_actor(
                actor_id, "human", identity.display_name if identity else "Human", member.episode_id
            )
        return actor_id

    def mark(actor_id: str, kind: str, at: str | None, key: str, by: str | None = None) -> None:
        if at:
            marks.append(
                EpisodeTimelineMark(
                    item_id=f"mark:{key}:{kind}",
                    actor_id=actor_id,
                    kind=kind,
                    at=at,
                    by_span_id=by,
                )
            )

    add_actor(
        primary_id,
        primary_kind,
        "Orchestrator" if auto else "Experiment agent",
        chain[0].episode_id,
        started_at=chain[0].created_at,
        ended_at=chain[-1].ended_at,
        outcome=chain[-1].ending,
        links=EpisodeTimelineLinks(
            episode_id=episode.episode_id, control_node_id=episode.control_node_id
        ),
    )
    for member in chain:
        mark(human(member, member.authorized_by), "started", member.created_at, member.episode_id)
        mark(primary_id, "stop_requested", member.stop_requested_at, member.episode_id)
        mark(
            primary_id,
            "stopped",
            member.ended_at if member.stop_requested_at else None,
            member.episode_id,
        )
    for child_id, route in routes.items():
        child = children.get(child_id)
        actor_id = f"actor:experiment:{child_id}"
        handoff = handoff_by_actor.get(actor_id)
        add_actor(
            actor_id,
            "experiment",
            "Experiment",
            route.auto_research_episode_id,
            subtitle=route.control_node_id,
            row_key=f"node:{route.control_node_id}",
            started_at=child.created_at if child else route.created_at,
            ended_at=child.ended_at if child else None,
            outcome=child.ending if child else None,
            started_by_span_id=handoff.from_span_id
            if handoff
            else _span(route.parent_operation_id),
            links=EpisodeTimelineLinks(episode_id=child_id, control_node_id=route.control_node_id),
        )
        mark(actor_id, "started", route.created_at, child_id, actors[actor_id].started_by_span_id)
        if child:
            mark(actor_id, "stop_requested", child.stop_requested_at, child_id)
            mark(actor_id, "stopped", child.ended_at if child.stop_requested_at else None, child_id)
    for owner in owners.values():
        # Report turns are hidden allocations; the roster shows them as report spans.
        for task in store.episode_tasks(owner.episode_id, include_hidden=True):
            if task.project_id != episode.project_id or task.kind == "branch_merge":
                continue
            if task.visible or task.kind == "episode_report":
                tasks[task.operation_id] = task
        invocations.update(
            {
                row.operation_id: row.invocation_number
                for row in store.episode_invocations(owner.episode_id)
            }
        )
    roles = store.auto_research_invocations(list(tasks)) if auto else {}
    for task in tasks.values():
        work = store.auto_research_child_work_for_operation(task.operation_id) if auto else None
        if work and work.worker_id in works:
            task_actors[task.operation_id] = f"actor:worker:{work.worker_id}"
        elif task.episode_id in children:
            task_actors[task.operation_id] = f"actor:experiment:{task.episode_id}"
        elif (role := roles.get(task.operation_id)) and role.role == "worker":
            task_actors[task.operation_id] = f"actor:worker:{role.actor_operation_id}"
            if role.actor_operation_id not in works:
                add_actor(
                    task_actors[task.operation_id],
                    "worker",
                    "Worker",
                    role.episode_id,
                    subtitle=role.control_node_id,
                    links=EpisodeTimelineLinks(
                        task_id=role.actor_operation_id, control_node_id=role.control_node_id
                    ),
                )
        else:
            task_actors[task.operation_id] = primary_id
    waiting_workers = {
        watcher.worker_id
        for member in chain
        for watcher in store.episode_watchers(member.episode_id)
        if watcher.worker_id and watcher.status == "active"
    }
    for work in works.values():
        actor_id = f"actor:worker:{work.worker_id}"
        heading = re.search(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", work.instruction, re.MULTILINE)
        current = tasks.get(work.current_operation_id)
        handoff = handoff_by_actor.get(actor_id)
        add_actor(
            actor_id,
            "worker",
            heading.group(1) if heading else "Worker",
            work.episode_id,
            subtitle=work.control_node_id,
            started_at=work.created_at,
            # A worker waiting on its own watcher is still alive after its attempt finishes.
            ended_at=current.finished_at
            if current and work.worker_id not in waiting_workers
            else None,
            outcome=current.status if current else None,
            started_by_span_id=handoff.from_span_id
            if handoff
            else _span(work.admitted_by_operation_id),
            links=EpisodeTimelineLinks(
                task_id=work.current_operation_id,
                control_node_id=work.control_node_id,
                episode_id=work.episode_id,
            ),
        )
        task_actors[work.root_operation_id] = actor_id
        task_actors[work.worker_id] = actor_id
        mark(
            actor_id,
            "started",
            work.created_at,
            work.worker_id,
            actors[actor_id].started_by_span_id,
        )
        # Only the request time is recorded: no issuer, and no settled stop time.
        mark(actor_id, "stop_requested", work.stop_requested_at, work.worker_id)
    causes = store.agent_task_continuation_causes(list(tasks))
    for task in tasks.values():
        actor_id = task_actors[task.operation_id]
        cause = task.request.get("wake_cause") or causes.get(task.operation_id)
        spans.append(
            EpisodeTimelineSpan(
                span_id=f"span:{task.operation_id}",
                actor_id=actor_id,
                kind="report"
                if task.kind == "episode_report"
                else "attempt"
                if actors[actor_id].kind == "worker" or task.attempt > 1
                else "turn",
                started_at=task.started_at or task.created_at,
                finished_at=task.finished_at,
                status=task.status,
                attempt=task.attempt,
                invocation_number=invocations.get(task.operation_id),
                headline=_headline(task),
                error=task.error[:EPISODE_TIMELINE_ERROR_MAX_LENGTH] if task.error else None,
                cause=cause if isinstance(cause, str) else None,
                task_id=task.operation_id,
                owner_episode_id=task.episode_id,
            )
        )
    _communications(store, chain, actors, tasks, task_actors, human, messages, signals, auto)
    collections = {
        "spans": spans,
        "handoffs": handoffs,
        "messages": messages,
        "signals": signals,
        "marks": marks,
    }
    dated = []
    for name, items in collections.items():
        for item in items:
            at = (
                item.started_at
                if name == "spans"
                else item.sent_at
                if name == "messages"
                else item.recorded_at
                if name == "signals"
                else item.at
            )
            key = item.span_id if name == "spans" else item.item_id
            dated.append((at, key, name, item))
    dated.sort(key=lambda row: (row[0], row[1]), reverse=True)
    retained = dated[:EPISODE_TIMELINE_EVENT_LIMIT]
    selected = {name: [] for name in collections}
    actor_ids = set()
    span_actors = {span.span_id: span.actor_id for span in spans}
    for _, _, name, item in retained:
        selected[name].append(item)
        for field, value in item.model_dump().items():
            if field.endswith("actor_id") and value:
                actor_ids.add(value)
            elif field.endswith("span_id") and value in span_actors:
                actor_ids.add(span_actors[value])
    return EpisodeTimelineResponse(
        episode_id=episode.episode_id,
        mode=episode.mode,
        generated_at=store.now(),
        truncated=len(dated) > len(retained),
        members=[
            EpisodeTimelineMember(
                episode_id=m.episode_id,
                started_at=m.created_at,
                ended_at=m.ended_at,
                continues_episode_id=m.continues_episode_id,
            )
            for m in chain
        ],
        actors=[actor for actor in actors.values() if actor.actor_id in actor_ids],
        **selected,
    )


def _communications(
    store: AppStore,
    chain: list[EpisodeRecord],
    actors: dict[str, EpisodeTimelineActor],
    tasks: dict[str, AgentTaskRecord],
    task_actors: dict[str, str],
    human: Callable[[EpisodeRecord, AuthorizedHuman | None], str],
    messages: list[EpisodeTimelineMessage],
    signals: list[EpisodeTimelineSignal],
    auto: bool,
) -> None:
    receipts = [
        receipt
        for member in chain
        for receipt in (store.auto_research_inbox_receipts(member.episode_id) if auto else [])
    ]
    mail_receipts = {key: receipt for receipt in receipts for key in receipt.message_ids}
    notice_receipts = {key: receipt for receipt in receipts for key in receipt.notice_ids}
    watchers_by_episode = {
        member.episode_id: store.episode_watchers(member.episode_id) for member in chain
    }
    watcher_by_id = {
        watcher.watcher_id: watcher
        for watchers in watchers_by_episode.values()
        for watcher in watchers
    }
    for member in chain:
        for message in store.auto_research_messages(member.episode_id) if auto else []:
            receipt = mail_receipts.get(message.message_id)
            delivered = tasks.get(message.delivery_operation_id)
            disposition = (
                "harvested"
                if receipt and receipt.mode == "harvest"
                else "cleared"
                if receipt
                else "undelivered"
                if not message.delivered_at and not message.delivery_operation_id
                else "unknown"
                if delivered is None
                else "failed_attempt"
                if delivered.status in {"failed", "interrupted"}
                else "wake"
            )
            messages.append(
                EpisodeTimelineMessage(
                    item_id=f"message:{message.message_id}",
                    from_actor_id=human(member, message.authorized_by)
                    if message.sender_role == "human"
                    else task_actors.get(message.sender_task_id),
                    to_actor_id=task_actors.get(message.recipient_task_id),
                    sent_at=message.created_at,
                    sent_span_id=_span(message.sender_task_id),
                    delivered_at=message.delivered_at,
                    delivered_span_id=_span(message.delivery_operation_id),
                    disposition=disposition,
                    preview=message.body[:EPISODE_TIMELINE_PREVIEW_MAX_LENGTH],
                    text_ref=f"message:{message.message_id}",
                )
            )
        watchers = watchers_by_episode[member.episode_id]
        for watcher in watchers:
            graph = isinstance(watcher, GraphWatcherRecord)
            if auto and not graph:
                continue
            actor_id = None if graph else f"actor:watcher:{watcher.watcher_id}"
            node = watcher.condition.node_id if graph else watcher.node_id
            # A shell-watcher group shares one row, as it does in the Watchers fold.
            row_key = (
                f"node:{node}"
                if graph
                else f"watchers:{watcher.group_label}"
                if watcher.group_label
                else actor_id
            )
            if actor_id:
                actors[actor_id] = EpisodeTimelineActor(
                    actor_id=actor_id,
                    kind="watcher",
                    label=watcher.group_label or "Watcher",
                    subtitle=node,
                    row_key=row_key,
                    owner_episode_id=member.episode_id,
                    started_at=watcher.created_at,
                    ended_at=watcher.completed_at or watcher.stopped_at,
                    outcome=watcher.status,
                    started_by_span_id=_span(watcher.origin_operation_id),
                    links=EpisodeTimelineLinks(
                        watcher_id=watcher.watcher_id,
                        control_node_id=node,
                        episode_id=member.episode_id,
                    ),
                )
            notification = tasks.get(watcher.notification_operation_id)
            signals.append(
                EpisodeTimelineSignal(
                    item_id=f"watcher:{watcher.watcher_id}",
                    kind="watcher",
                    source_actor_id=actor_id,
                    source_row_key=row_key,
                    event="fired"
                    if watcher.completed_at
                    else "stopped"
                    if watcher.stopped_at
                    else "armed",
                    recorded_at=watcher.completed_at or watcher.stopped_at or watcher.created_at,
                    landed_at=(notification.started_at or notification.created_at)
                    if notification
                    else None,
                    landed_span_id=_span(watcher.notification_operation_id),
                    landing="woke" if watcher.notification_operation_id else None,
                    armed_span_id=_span(watcher.origin_operation_id),
                    armed_at=watcher.created_at,
                    state=watcher.status,
                    payload=watcher.condition.model_dump(mode="json")
                    if graph
                    else {
                        "check_command": watcher.check_command,
                        "stop_reason": watcher.stop_reason,
                    },
                )
            )
        for notice in store.auto_research_lifecycle_notices(member.episode_id) if auto else []:
            receipt = notice_receipts.get(notice.notice_id)
            source = task_actors.get(notice.source_id)
            if notice.source_kind in {"experiment", "experiment_episode", "episode"}:
                candidate = f"actor:experiment:{notice.source_id}"
                source = candidate if candidate in actors else None
            elif notice.source_kind in {"work", "worker"}:
                candidate = f"actor:worker:{notice.source_id}"
                source = candidate if candidate in actors else source
            watcher = (
                watcher_by_id.get(notice.source_id) if notice.source_kind == "watcher" else None
            )
            source_row = (
                actors[source].row_key
                if source in actors
                else f"{notice.source_kind}:{notice.source_id}"
            )
            if isinstance(watcher, GraphWatcherRecord):
                source = None
                source_row = f"node:{watcher.condition.node_id}"
            landing = (
                "woke"
                if notice.delivery_operation_id
                else "harvested"
                if receipt and receipt.mode == "harvest"
                else "acknowledged"
                if notice.acknowledged_at
                else None
            )
            signals.append(
                EpisodeTimelineSignal(
                    item_id=f"notice:{notice.notice_id}",
                    kind="notice",
                    source_actor_id=source,
                    source_row_key=source_row,
                    event=notice.source_event,
                    recorded_at=notice.created_at,
                    landed_at=notice.delivered_at or notice.acknowledged_at,
                    landed_span_id=_span(
                        notice.delivery_operation_id or notice.acknowledged_operation_id
                    ),
                    landing=landing,
                    state=notice.state,
                    payload=notice.payload,
                )
            )
