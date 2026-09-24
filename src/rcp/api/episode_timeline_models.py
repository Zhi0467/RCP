from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rcp.limits import (
    EPISODE_TIMELINE_ERROR_MAX_LENGTH,
    EPISODE_TIMELINE_HEADLINE_MAX_LENGTH,
    EPISODE_TIMELINE_PREVIEW_MAX_LENGTH,
)


class TimelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EpisodeTimelineMember(TimelineModel):
    episode_id: str
    started_at: str
    ended_at: str | None
    continues_episode_id: str | None


class EpisodeTimelineLinks(TimelineModel):
    task_id: str | None = None
    episode_id: str | None = None
    control_node_id: str | None = None
    watcher_id: str | None = None


class EpisodeTimelineActor(TimelineModel):
    actor_id: str
    kind: Literal["human", "orchestrator", "worker", "experiment", "agent", "watcher"]
    label: str
    subtitle: str | None = None
    row_key: str
    owner_episode_id: str
    started_at: str | None = None
    ended_at: str | None = None
    outcome: str | None = None
    started_by_span_id: str | None = None
    links: EpisodeTimelineLinks = Field(default_factory=EpisodeTimelineLinks)


class EpisodeTimelineSpan(TimelineModel):
    span_id: str
    actor_id: str
    kind: Literal["turn", "attempt", "report"]
    started_at: str
    finished_at: str | None
    status: str
    attempt: int | None
    invocation_number: int | None
    headline: str | None = Field(max_length=EPISODE_TIMELINE_HEADLINE_MAX_LENGTH)
    error: str | None = Field(max_length=EPISODE_TIMELINE_ERROR_MAX_LENGTH)
    cause: str | None
    task_id: str
    owner_episode_id: str


class EpisodeTimelineHandoff(TimelineModel):
    item_id: str
    kind: Literal["assignment", "goal"]
    from_span_id: str
    to_actor_id: str
    at: str
    preview: str = Field(max_length=EPISODE_TIMELINE_PREVIEW_MAX_LENGTH)
    text_ref: str


class EpisodeTimelineMessage(TimelineModel):
    item_id: str
    from_actor_id: str | None
    to_actor_id: str | None
    sent_at: str
    sent_span_id: str | None
    delivered_at: str | None
    delivered_span_id: str | None
    disposition: Literal["wake", "harvested", "cleared", "failed_attempt", "undelivered", "unknown"]
    preview: str = Field(max_length=EPISODE_TIMELINE_PREVIEW_MAX_LENGTH)
    text_ref: str


class EpisodeTimelineSignal(TimelineModel):
    item_id: str
    kind: Literal["notice", "watcher"]
    source_actor_id: str | None
    source_row_key: str
    event: str
    recorded_at: str
    landed_at: str | None = None
    landed_span_id: str | None = None
    landing: Literal["woke", "harvested", "acknowledged"] | None = None
    armed_span_id: str | None = None
    armed_at: str | None = None
    state: str | None = None
    payload: dict[str, object]


class EpisodeTimelineMark(TimelineModel):
    item_id: str
    actor_id: str
    kind: Literal["started", "stop_requested", "stopped"]
    at: str
    by_span_id: str | None = None


class EpisodeTimelineResponse(TimelineModel):
    episode_id: str
    mode: Literal["auto_research", "experiment_loop"]
    generated_at: str
    truncated: bool
    members: list[EpisodeTimelineMember]
    actors: list[EpisodeTimelineActor]
    spans: list[EpisodeTimelineSpan]
    handoffs: list[EpisodeTimelineHandoff]
    messages: list[EpisodeTimelineMessage]
    signals: list[EpisodeTimelineSignal]
    marks: list[EpisodeTimelineMark]


class EpisodeTimelineText(TimelineModel):
    text_ref: str
    kind: Literal["assignment", "goal", "message"]
    owner_episode_id: str
    body: str
    sha256: str
