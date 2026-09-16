from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rcp.api.episodes import episode_chain_records, operational_episode_tasks
from rcp.core.models import AuthorizedHuman
from rcp.limits import EPISODE_TIMELINE_EVENT_LIMIT
from rcp.storage import AppStore, EpisodeRecord
from rcp.storage.models import (
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    StoredWatcherRecord,
)

EpisodeTimelineEventKind = Literal[
    "turn", "retry", "wake", "mail", "notice", "child", "lifecycle", "human"
]


_EVENT_TITLE_MAX = 120


def _armed_label(watcher: StoredWatcherRecord) -> str:
    """Name what an armed watcher waits for, so a parked episode reads as parked."""

    if not isinstance(watcher, GraphWatcherRecord):
        return "Watcher armed"
    condition = watcher.condition
    if isinstance(condition, NodeStatusGraphCondition):
        statuses = " or ".join(condition.status_in)
        label = f"Waiting for {condition.node_id} to reach {statuses}"
    else:
        label = f"Waiting for a Proposal on {condition.node_id} to resolve"
    # Node ids are agent-authored; the event title is bounded at 120.
    limit = _EVENT_TITLE_MAX
    return label if len(label) <= limit else label[: limit - 1] + "\u2026"


class EpisodeTimelineActor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["orchestrator", "worker", "wake", "human", "rcp", "child"]
    id: str | None = None
    label: str
    member: AuthorizedHuman | None = None


class EpisodeTimelineLinks(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str | None = None
    message_id: str | None = None
    notice_id: str | None = None
    episode_id: str | None = None
    control_node_id: str | None = None


class EpisodeTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str
    kind: EpisodeTimelineEventKind
    at: str
    actor: EpisodeTimelineActor
    parent_event_id: str | None = None
    title: str = Field(max_length=120)
    detail: str | None = Field(default=None, max_length=500)
    status: str | None = None
    cause: str | None = None
    links: EpisodeTimelineLinks = Field(default_factory=EpisodeTimelineLinks)
    provenance: Literal["recorded", "unknown"] = "recorded"


class EpisodeTimelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    episode_id: str
    mode: Literal["auto_research", "experiment_loop"]
    events: list[EpisodeTimelineEvent]
    truncated: bool


def _detail(value: str | None) -> str | None:
    return " ".join(value.split())[:500] if value else None


def _human(member: AuthorizedHuman | None, user_id: str | None = None) -> EpisodeTimelineActor:
    return EpisodeTimelineActor(
        kind="human",
        id=member.user_id if member else user_id,
        label=member.display_name if member else "Human",
        member=member,
    )


def build_episode_timeline(store: AppStore, episode: EpisodeRecord) -> EpisodeTimelineResponse:
    """Project recorded facts without dispatching work or inferring lifecycle decisions.

    A continuation chain is one run, so the timeline spans every member with a
    ``continued`` boundary between them. The requested episode keeps its plain
    event ids; other members' ids carry their episode id.
    """

    events: list[EpisodeTimelineEvent] = []
    chain = episode_chain_records(store, episode)
    skipped_members = False
    # Newest member first: a continuation is created after its source ended, so
    # once the newer members alone overflow the response no older member can
    # place an event in it, and its hydration is skipped.
    for position in range(len(chain) - 1, -1, -1):
        if len(events) > EPISODE_TIMELINE_EVENT_LIMIT:
            skipped_members = True
            break
        member = chain[position]
        primary = member.episode_id == episode.episode_id
        if position:
            events.append(
                EpisodeTimelineEvent(
                    kind="lifecycle",
                    event_id=f"lifecycle:continued:{member.episode_id}",
                    at=member.created_at,
                    title="Continued with more turns",
                    actor=_human(member.authorized_by),
                    status=str(member.invocation_ceiling),
                    provenance="recorded" if member.authorized_by else "unknown",
                    links=EpisodeTimelineLinks(episode_id=member.episode_id),
                )
            )
        events.extend(_member_events(store, member, primary=primary))
    events.sort(key=lambda event: (event.at, event.event_id))
    return EpisodeTimelineResponse(
        episode_id=episode.episode_id,
        mode=episode.mode,
        events=events[-EPISODE_TIMELINE_EVENT_LIMIT:],
        truncated=skipped_members or len(events) > EPISODE_TIMELINE_EVENT_LIMIT,
    )


def _member_events(
    store: AppStore, episode: EpisodeRecord, *, primary: bool
) -> list[EpisodeTimelineEvent]:
    auto = episode.mode == "auto_research"
    # The response keeps the newest EPISODE_TIMELINE_EVENT_LIMIT events. Every task
    # is exactly one event at its creation time, so no task older than the newest
    # limit + 1 can reach the wire; the extra row keeps ``truncated`` honest.
    newest = EPISODE_TIMELINE_EVENT_LIMIT + 1
    tasks = (
        [
            task
            for task in store.auto_research_tasks(episode.episode_id, newest=newest)
            if task.visible
        ]
        if auto
        else operational_episode_tasks(store, episode, newest=newest)
    )
    by_task = {task.operation_id: task for task in tasks}
    if auto:
        # Ordinary child Work allocations are outside auto_research_invocations.
        for task in operational_episode_tasks(store, episode, newest=newest):
            by_task.setdefault(task.operation_id, task)
        tasks = list(by_task.values())
    degradations = store.agent_task_degradations(list(by_task))
    roles = store.auto_research_invocations(list(by_task)) if auto else {}
    recoveries = store.auto_research_recoveries(episode.episode_id, newest=newest) if auto else []
    recovery_by_task = {row.admitted_operation_id: row for row in recoveries}
    # Child routes and watchers are bounded the same way: each is at least one
    # event, so only the newest limit + 1 can reach the wire.
    works = store.auto_research_child_works(episode.episode_id, newest=newest) if auto else []
    work_origins = {row.root_operation_id: row for row in works}
    work_for_task = {}
    for task in tasks:
        current = task
        seen: set[str] = set()
        while current.operation_id not in seen:
            seen.add(current.operation_id)
            if current.operation_id in work_origins:
                work_for_task[task.operation_id] = work_origins[current.operation_id]
                break
            parent = by_task.get(current.parent_operation_id)
            if parent is None:
                break
            current = parent
    routes = (
        store.auto_research_child_experiments(episode.episode_id, newest=newest) if auto else []
    )
    children = store.episodes_by_ids([route.child_episode_id for route in routes])
    # Notice and message bodies run up to 16 KB; hydrate only as many as could reach the wire.
    notices = (
        store.auto_research_lifecycle_notices(episode.episode_id, newest=newest) if auto else []
    )
    messages = store.auto_research_messages(episode.episode_id, newest=newest) if auto else []
    # An Auto-research episode's own watchers are graph conditions, and between
    # turns they are the only record of what the orchestrator is waiting for.
    # Its shell watchers belong to child episodes and stay on their timelines.
    watchers = [
        watcher
        for watcher in store.episode_watchers(episode.episode_id, newest=newest)
        if not auto or isinstance(watcher, GraphWatcherRecord)
    ]
    wrapup = store.episode_wrapup(episode.episode_id)
    report = store.episode_report(episode.episode_id)
    members = {member.user_id: member for member in store.space_users()}
    causes = store.agent_task_continuation_causes([task.operation_id for task in tasks])
    kinds: dict[str, EpisodeTimelineEventKind] = {}
    actors: dict[str, EpisodeTimelineActor] = {}
    for task in tasks:
        invocation = roles.get(task.operation_id)
        cause = causes[task.operation_id]
        wake = (
            bool(task.request.get("wake_cause"))
            or cause in {"watcher_wake", "graph_condition_wake", "message_wake", "lifecycle_wake"}
            or task.request.get("trigger") == "watcher"
        )
        kinds[task.operation_id] = (
            "retry" if task.attempt > 1 and task.parent_operation_id else "wake" if wake else "turn"
        )
        role = (
            "wake"
            if wake
            else invocation.role
            if invocation
            else "orchestrator"
            if task.operation_id == episode.root_operation_id
            else "worker"
        )
        actors[task.operation_id] = EpisodeTimelineActor(
            kind=role,
            id=invocation.actor_operation_id if invocation else task.operation_id,
            label=role.capitalize(),
        )

    def event_key(event_id: str) -> str:
        return event_id if primary else f"{episode.episode_id}:{event_id}"

    def task_event(operation_id: str | None) -> str | None:
        return event_key(f"{kinds[operation_id]}:{operation_id}") if operation_id in kinds else None

    events: list[EpisodeTimelineEvent] = []
    rcp = EpisodeTimelineActor(kind="rcp", label="RCP")

    def add(
        kind: EpisodeTimelineEventKind,
        event_id: str,
        at: str | None,
        title: str,
        *,
        actor: EpisodeTimelineActor = rcp,
        **values: object,
    ) -> None:
        if at is None:
            values["provenance"] = "unknown"
        events.append(
            EpisodeTimelineEvent(
                kind=kind,
                event_id=event_key(event_id),
                at=at or episode.updated_at,
                title=title[:120],
                actor=actor,
                **values,
            )
        )

    for task in tasks:
        kind = kinds[task.operation_id]
        parent = task_event(task.parent_operation_id) if kind == "retry" else None
        invocation = roles.get(task.operation_id)
        work = work_for_task.get(task.operation_id)
        if work is None and invocation:
            work = work_origins.get(invocation.actor_operation_id)
        if kind != "retry" and work:
            parent = task_event(work.admitted_by_operation_id)
        cause = (
            task.request.get("wake_cause") or causes[task.operation_id] if kind == "wake" else None
        )
        provenance = "recorded"
        if kind == "retry":
            cause = "automatic recovery" if task.operation_id in recovery_by_task else None
            if cause is None:
                provenance = "unknown"
        if (
            kind == "turn"
            and auto
            and task.operation_id not in roles
            and task.operation_id not in work_for_task
        ):
            provenance = "unknown"
        add(
            kind,
            f"{kind}:{task.operation_id}",
            task.created_at,
            ("Wake" if kind == "wake" else f"{actors[task.operation_id].label} {kind}")
            + (f" · attempt {task.attempt}" if kind == "retry" else ""),
            actor=actors[task.operation_id],
            parent_event_id=parent,
            detail=_detail(
                " · ".join(filter(None, (degradations.get(task.operation_id), task.status_message)))
            ),
            status=task.status,
            cause=cause if isinstance(cause, str) else None,
            links=EpisodeTimelineLinks(
                task_id=task.operation_id,
                episode_id=episode.episode_id,
                control_node_id=work.control_node_id
                if work
                else invocation.control_node_id
                if invocation
                else episode.control_node_id
                or (
                    task.request.get("node_id")
                    if isinstance(task.request.get("node_id"), str)
                    else None
                ),
            ),
            provenance=provenance,
        )

    for notice in notices:
        cause = notice.source_event
        if notice.wake_suppressed:
            cause += f"; wake_suppressed={notice.wake_suppressed}"
        add(
            "notice",
            f"notice:{notice.notice_id}",
            notice.created_at,
            f"{notice.source_kind} {notice.source_event}".replace("_", " ").capitalize(),
            parent_event_id=task_event(notice.delivery_operation_id)
            or task_event(notice.acknowledged_operation_id),
            status=notice.state,
            cause=cause,
            links=EpisodeTimelineLinks(notice_id=notice.notice_id, episode_id=episode.episode_id),
        )
    for message in messages:
        # The sender role is the record's own attribution; the sending task's
        # kind is not, since an orchestrator writes mail from a wake turn too.
        actor = (
            _human(message.authorized_by)
            if message.sender_role == "human"
            else EpisodeTimelineActor(
                kind=message.sender_role,
                id=message.sender_task_id,
                label=message.sender_role.capitalize(),
            )
        )
        add(
            "mail",
            f"mail:{message.message_id}",
            message.created_at,
            f"Message from {actor.label}",
            actor=actor,
            detail=_detail(message.body),
            parent_event_id=task_event(message.delivery_operation_id),
            status="delivered" if message.delivery_operation_id else "pending",
            provenance="unknown"
            if message.sender_role == "human" and message.authorized_by is None
            else "recorded",
            links=EpisodeTimelineLinks(
                message_id=message.message_id,
                task_id=message.sender_task_id,
                episode_id=episode.episode_id,
                control_node_id=message.control_node_id,
            ),
        )
    for work in works:
        task = by_task.get(work.current_operation_id)
        add(
            "child",
            f"child:{work.worker_id}:admitted",
            work.created_at,
            "Work admitted",
            actor=EpisodeTimelineActor(kind="child", id=work.worker_id, label="Work"),
            parent_event_id=task_event(work.admitted_by_operation_id),
            status=task.status if task else None,
            links=EpisodeTimelineLinks(
                task_id=work.current_operation_id,
                episode_id=episode.episode_id,
                control_node_id=work.control_node_id,
            ),
        )
    for route in routes:
        child = children.get(route.child_episode_id)
        links = EpisodeTimelineLinks(
            episode_id=route.child_episode_id, control_node_id=route.control_node_id
        )
        actor = EpisodeTimelineActor(kind="child", id=route.child_episode_id, label="Experiment")
        created_id = f"child:{route.child_episode_id}:created"
        add(
            "child",
            created_id,
            route.created_at,
            "Experiment created",
            actor=actor,
            parent_event_id=task_event(route.parent_operation_id),
            status=route.state,
            links=links,
        )
        if child and child.ended_at:
            add(
                "child",
                f"child:{child.episode_id}:ended",
                child.ended_at,
                "Experiment ended",
                actor=actor,
                parent_event_id=event_key(created_id),
                status=child.ending,
                links=links,
            )
        if child and child.stop_requested_at:
            add(
                "child",
                f"child:{child.episode_id}:stopped",
                child.stop_requested_at,
                "Experiment stopped",
                actor=actor,
                parent_event_id=event_key(created_id),
                cause=child.stop_initiated_by,
                provenance="recorded" if child.stop_initiated_by else "unknown",
                links=links,
            )
    for watcher in watchers:
        links = EpisodeTimelineLinks(
            task_id=watcher.notification_operation_id or watcher.origin_operation_id,
            episode_id=episode.episode_id,
            control_node_id=watcher.node_id,
        )
        add(
            "notice",
            f"notice:{watcher.watcher_id}:armed",
            watcher.created_at,
            _armed_label(watcher),
            parent_event_id=task_event(watcher.origin_operation_id),
            status="armed",
            cause="watcher_armed",
            links=links,
        )
        if watcher.completed_at:
            add(
                "notice",
                f"notice:{watcher.watcher_id}:completed",
                watcher.completed_at,
                "Watcher completed",
                parent_event_id=task_event(watcher.notification_operation_id),
                status="completed",
                cause="watcher_completed",
                links=links,
            )
        if watcher.stopped_at:
            add(
                "notice",
                f"notice:{watcher.watcher_id}:stopped",
                watcher.stopped_at,
                "Watcher stopped",
                parent_event_id=task_event(watcher.stop_operation_id),
                status="stopped",
                cause=watcher.stop_reason,
                links=links,
            )
    if episode.invocations_used == episode.invocation_ceiling:
        # Only the ceiling invocation is an event; it is the newest one by number.
        invocation = next(
            (
                row
                for row in store.episode_invocations(episode.episode_id, newest=1)
                if row.invocation_number == episode.invocation_ceiling
            ),
            None,
        )
        add(
            "lifecycle",
            "lifecycle:ceiling_reached",
            invocation.created_at if invocation else None,
            "Invocation ceiling reached",
            links=EpisodeTimelineLinks(task_id=invocation.operation_id if invocation else None),
        )
    if episode.ending:
        add(
            "lifecycle",
            "lifecycle:ending_fenced",
            wrapup.created_at if wrapup else episode.ended_at,
            "Ending fenced",
            status=episode.ending,
        )
    for recovery in recoveries:
        if recovery.status in {"exhausted", "blocked"}:
            name = "recovery_exhausted" if recovery.status == "exhausted" else "recovery_blocked"
            add(
                "lifecycle",
                f"lifecycle:{name}:{recovery.recovery_id}",
                recovery.updated_at,
                name.replace("_", " ").capitalize(),
                parent_event_id=task_event(recovery.operation_id),
                status=recovery.status,
                detail=_detail(recovery.diagnostic),
                cause=recovery.failure_kind,
                links=EpisodeTimelineLinks(task_id=recovery.operation_id),
            )
    if episode.wrapup_state in {"failed", "ready"}:
        name = "wrapup_failed" if episode.wrapup_state == "failed" else "report_ready"
        add(
            "lifecycle",
            f"lifecycle:{name}",
            wrapup.finished_at if wrapup else report.created_at if report else None,
            name.replace("_", " ").capitalize(),
            status=episode.wrapup_state,
            detail=_detail(episode.wrapup_error),
        )
    add(
        "human",
        f"human:started:{episode.created_at}",
        episode.created_at,
        "Started",
        actor=_human(episode.authorized_by),
        provenance="recorded" if episode.authorized_by else "unknown",
    )
    if episode.stop_requested_at:
        initiator = episode.stop_initiated_by
        user_id = (
            initiator.removeprefix("human:")
            if initiator and initiator.startswith("human:")
            else None
        )
        user = members.get(user_id)
        member = (
            AuthorizedHuman(
                space_id=store.space_id, user_id=user.user_id, display_name=user.display_name
            )
            if user and user.display_name
            else None
        )
        actor = _human(member, user_id) if user_id or not initiator else rcp
        add(
            "human" if user_id or not initiator else "lifecycle",
            f"human:stopped:{episode.stop_requested_at}"
            if user_id or not initiator
            else "lifecycle:stopped",
            episode.stop_requested_at,
            "Stopped",
            actor=actor,
            cause=initiator,
            provenance="recorded" if initiator else "unknown",
        )
    return events
