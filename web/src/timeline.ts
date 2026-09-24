import type {
  EpisodeTimelineActor,
  EpisodeTimelineMessage,
  EpisodeTimelineResponse,
  EpisodeTimelineSignal,
  ExperimentLoopIndexEntry,
} from "./types";

export type TimelineWindow = [number, number];
export const MIN_TIMELINE_WINDOW = 10 * 60 * 1000;
export const ACTOR_KINDS: EpisodeTimelineActor["kind"][] = [
  "human",
  "orchestrator",
  "agent",
  "worker",
  "experiment",
  "watcher",
];
export interface TimelineRow {
  rowKey: string;
  kind: EpisodeTimelineActor["kind"];
  label: string;
  subtitle: string | null;
  actors: EpisodeTimelineActor[];
}

export function timelineActorLabel(
  actor: EpisodeTimelineActor,
  children: ExperimentLoopIndexEntry[] = [],
): string {
  if (actor.kind === "worker") return actor.label.replace(/^Assignment:\s*/i, "");
  if (actor.kind === "experiment")
    return (
      children.find((child) => child.episode.episode_id === actor.links.episode_id)?.node.title ??
      actor.links.control_node_id ??
      actor.subtitle ??
      actor.row_key.replace(/^node:/, "")
    );
  return actor.label;
}

export function timelineOutcome(actor: EpisodeTimelineActor): string {
  return actor.outcome === "exhausted"
    ? "out of turns"
    : (actor.outcome ?? (actor.ended_at ? "unknown" : "running"));
}

export const messageDisposition = {
  wake: "delivered with a wake",
  harvested: "harvested",
  cleared: "cleared",
  failed_attempt: "delivered to an attempt that failed",
  undelivered: "not delivered",
  unknown: "delivery unknown",
};

export function timelineRows(
  data: EpisodeTimelineResponse,
  children: ExperimentLoopIndexEntry[] = [],
): TimelineRow[] {
  const rows = new Map<string, TimelineRow>();
  for (const actor of [...data.actors].sort(
    (a, b) =>
      ACTOR_KINDS.indexOf(a.kind) - ACTOR_KINDS.indexOf(b.kind) ||
      (Date.parse(a.started_at ?? "") || 0) - (Date.parse(b.started_at ?? "") || 0),
  )) {
    const row = rows.get(actor.row_key);
    if (row) {
      row.actors.push(actor);
      if (actor.kind === "experiment") row.label = timelineActorLabel(actor, children);
    } else
      rows.set(actor.row_key, {
        rowKey: actor.row_key,
        kind: actor.kind,
        label: timelineActorLabel(actor, children),
        subtitle:
          actor.kind === "experiment"
            ? (actor.links.control_node_id ?? actor.subtitle)
            : actor.subtitle,
        actors: [actor],
      });
  }
  return [...rows.values()];
}

export function timelineBounds(data: EpisodeTimelineResponse): TimelineWindow {
  const dates = [
    ...data.members.flatMap((m) => [m.started_at, m.ended_at]),
    ...data.actors.flatMap((a) => [a.started_at, a.ended_at]),
    ...data.spans.flatMap((s) => [s.started_at, s.finished_at]),
    ...data.handoffs.map((h) => h.at),
    ...data.messages.flatMap((m) => [m.sent_at, m.delivered_at]),
    ...data.signals.flatMap((s) => [s.recorded_at, s.landed_at, s.armed_at]),
    ...data.marks.map((m) => m.at),
    ...(data.members.some((m) => !m.ended_at) ? [data.generated_at] : []),
  ].flatMap((date) => (date && Number.isFinite(Date.parse(date)) ? [Date.parse(date)] : []));
  const start = dates.length ? Math.min(...dates) : Date.parse(data.generated_at);
  return [start, Math.max(start + MIN_TIMELINE_WINDOW, ...dates)];
}

export function clampWindow(window: TimelineWindow, bounds: TimelineWindow): TimelineWindow {
  const width = Math.min(
    bounds[1] - bounds[0],
    Math.max(MIN_TIMELINE_WINDOW, window[1] - window[0]),
  );
  const start = Math.max(bounds[0], Math.min(bounds[1] - width, window[0]));
  return [start, start + width];
}

export function zoomWindow(
  window: TimelineWindow,
  bounds: TimelineWindow,
  anchor: number,
  factor: number,
): TimelineWindow {
  const fraction = Math.max(0, Math.min(1, (anchor - window[0]) / (window[1] - window[0])));
  const width = Math.min(
    bounds[1] - bounds[0],
    Math.max(MIN_TIMELINE_WINDOW, (window[1] - window[0]) * factor),
  );
  return clampWindow([anchor - width * fraction, anchor + width * (1 - fraction)], bounds);
}

export function panWindow(
  window: TimelineWindow,
  bounds: TimelineWindow,
  delta: number,
): TimelineWindow {
  return clampWindow([window[0] + delta, window[1] + delta], bounds);
}

// Direct recorded neighbors only: shared actors must not turn selection into a
// transitive walk through the entire run. Keep absent IDs for edge endpoints.
export function timelineRelated(data: EpisodeTimelineResponse, id: string): Set<string> {
  const related = new Set([id]);
  const actor = data.actors.find((a) => a.actor_id === id);
  const span = data.spans.find((s) => s.span_id === id);
  const add = (...ids: (string | null | undefined)[]) =>
    ids.forEach((i) => {
      if (i) related.add(i);
    });
  if (actor) {
    add(actor.started_by_span_id, actor.row_key);
    data.spans.filter((s) => s.actor_id === id).forEach((s) => add(s.span_id));
  }
  if (span) {
    add(span.actor_id);
    data.actors.filter((a) => a.started_by_span_id === id).forEach((a) => add(a.actor_id));
  }
  for (const item of data.handoffs) {
    if (item.item_id === id || item.from_span_id === id || item.to_actor_id === id) {
      add(item.item_id, item.from_span_id, item.to_actor_id);
      add(data.spans.find((s) => s.span_id === item.from_span_id)?.actor_id);
    }
  }
  for (const item of data.messages) {
    if (
      [
        item.item_id,
        item.from_actor_id,
        item.to_actor_id,
        item.sent_span_id,
        item.delivered_span_id,
      ].includes(id)
    )
      add(
        item.item_id,
        item.from_actor_id,
        item.to_actor_id,
        item.sent_span_id,
        item.delivered_span_id,
      );
  }
  for (const item of data.signals) {
    if (
      [
        item.item_id,
        item.source_actor_id,
        item.source_row_key,
        item.landed_span_id,
        item.armed_span_id,
      ].includes(id) ||
      (actor && actor.row_key === item.source_row_key)
    ) {
      add(
        item.item_id,
        item.source_actor_id,
        item.source_row_key,
        item.landed_span_id,
        item.armed_span_id,
      );
      data.actors.filter((a) => a.row_key === item.source_row_key).forEach((a) => add(a.actor_id));
    }
  }
  for (const item of data.marks) {
    if ([item.item_id, item.actor_id, item.by_span_id].includes(id))
      add(item.item_id, item.actor_id, item.by_span_id);
  }
  return related;
}

export function timelineWakeRows(
  data: EpisodeTimelineResponse,
  children: ExperimentLoopIndexEntry[] = [],
) {
  const label = (id: string | null) => {
    const actor = data.actors.find((a) => a.actor_id === id);
    return actor ? timelineActorLabel(actor, children) : "Unknown actor";
  };
  const node = (signal: EpisodeTimelineSignal) => {
    const source = data.actors.find((a) => a.row_key === signal.source_row_key);
    return (
      source?.links.control_node_id ??
      source?.subtitle ??
      signal.source_row_key.replace(/^node:/, "")
    );
  };
  const causes: Record<string, string> = {
    fresh: "Started",
    lifecycle: "Lifecycle notice",
    graph_condition: "Watcher fired",
    message: "Message",
  };
  const actors = new Set(
    data.actors.filter((a) => a.kind === "orchestrator").map((a) => a.actor_id),
  );
  return data.spans
    .filter((s) => actors.has(s.actor_id))
    .sort(
      (a, b) =>
        Date.parse(a.started_at) - Date.parse(b.started_at) || a.span_id.localeCompare(b.span_id),
    )
    .map((span, index) => ({
      span,
      number: index + 1,
      cause: span.cause ? (causes[span.cause] ?? span.cause) : null,
      landed: [
        ...data.signals
          .filter((s) => s.landed_span_id === span.span_id)
          .map((s) => ({
            item_id: s.item_id,
            label: [
              s.kind === "watcher" ? `Watcher on ${node(s)}` : label(s.source_actor_id),
              s.event,
              s.landing,
            ]
              .filter(Boolean)
              .join(" · "),
          })),
        ...data.messages
          .filter((m) => m.delivered_span_id === span.span_id)
          .map((m) => ({
            item_id: m.item_id,
            label: `Message from ${label(m.from_actor_id)} · ${messageDisposition[m.disposition]}`,
          })),
      ],
      actions: [
        ...data.handoffs
          .filter((h) => h.from_span_id === span.span_id)
          .map((h) => ({
            item_id: h.item_id,
            at: h.at,
            icon: "document",
            label: `Started ${h.kind === "assignment" ? "worker" : "Experiment"} ${label(h.to_actor_id)}`,
          })),
        ...data.messages
          .filter((m) => m.sent_span_id === span.span_id)
          .map((m) => ({
            item_id: m.item_id,
            at: m.sent_at,
            icon: "envelope",
            label: `Sent message to ${label(m.to_actor_id)}`,
          })),
        ...data.signals
          .filter((s) => s.kind === "watcher" && s.armed_span_id === span.span_id)
          .map((s) => ({
            item_id: s.item_id,
            at: s.armed_at,
            icon: null,
            label: `Armed watcher on ${node(s)}`,
          })),
      ].sort((a, b) => (a.at ?? "").localeCompare(b.at ?? "")),
    }));
}

export function timelineSummary(data: EpisodeTimelineResponse) {
  const actors = ACTOR_KINDS.flatMap((kind) => {
    const matching = data.actors.filter((a) => a.kind === kind);
    if (!matching.length) return [];
    const outcomes: Record<string, number> = {};
    for (const actor of matching) {
      const outcome = timelineOutcome(actor);
      outcomes[outcome] = (outcomes[outcome] ?? 0) + 1;
    }
    return [{ kind, total: matching.length, outcomes }];
  });
  const messages: Record<EpisodeTimelineMessage["disposition"], number> = {
    wake: 0,
    harvested: 0,
    cleared: 0,
    failed_attempt: 0,
    undelivered: 0,
    unknown: 0,
  };
  data.messages.forEach((message) => messages[message.disposition]++);
  return {
    actors,
    totalActors: data.actors.length,
    handoffs: data.handoffs.length,
    messages,
    totalMessages: data.messages.length,
    truncated: data.truncated,
  };
}
