import type { EpisodeTimelineEvent, EpisodeTimelineEventKind } from "./types";

export type TimelineLane = "orchestrator" | "workers" | "children" | "mail" | "lifecycle" | "human";
export type TimelineTone = "neutral" | "active" | "warning" | "danger" | "success" | "muted";
export interface TimelineRenderRule {
  lane: TimelineLane;
  glyph: string;
  tone: TimelineTone;
  fold: "never" | "folded" | "always";
}
export type TimelineRenderConfig = Record<EpisodeTimelineEventKind, TimelineRenderRule> & {
  statusTone?: (event: EpisodeTimelineEvent) => TimelineTone | undefined;
};
export interface TimelineRow {
  event: EpisodeTimelineEvent;
  children: TimelineRow[];
}

export const DEFAULT_TIMELINE_CONFIG: TimelineRenderConfig = {
  turn: { lane: "orchestrator", glyph: "●", tone: "neutral", fold: "never" },
  retry: { lane: "orchestrator", glyph: "↻", tone: "warning", fold: "never" },
  wake: { lane: "orchestrator", glyph: "↗", tone: "active", fold: "never" },
  child: { lane: "children", glyph: "◇", tone: "neutral", fold: "never" },
  mail: { lane: "mail", glyph: "✉", tone: "neutral", fold: "folded" },
  notice: { lane: "mail", glyph: "·", tone: "muted", fold: "folded" },
  lifecycle: { lane: "lifecycle", glyph: "◆", tone: "muted", fold: "never" },
  human: { lane: "human", glyph: "○", tone: "neutral", fold: "never" },
  statusTone: (event) => {
    if (["failed", "blocked", "interrupted"].includes(event.status ?? "")) return "danger";
    if (["running", "queued", "active"].includes(event.status ?? "")) return "active";
    if (["succeeded", "completed", "ready"].includes(event.status ?? "")) return "success";
    if (["exhausted", "paused", "pending"].includes(event.status ?? "")) return "warning";
    return undefined;
  },
};

function compareEvents(a: EpisodeTimelineEvent, b: EpisodeTimelineEvent): number {
  return Date.parse(a.at) - Date.parse(b.at) || a.event_id.localeCompare(b.event_id);
}

export class EpisodeTimeline {
  readonly events: EpisodeTimelineEvent[];
  readonly config: TimelineRenderConfig;

  constructor(events: EpisodeTimelineEvent[], config = DEFAULT_TIMELINE_CONFIG) {
    this.events = [...events].sort(compareEvents);
    this.config = config;
  }

  rule(event: EpisodeTimelineEvent): TimelineRenderRule {
    const rule = this.config[event.kind];
    return {
      ...rule,
      lane: rule.lane === "orchestrator" && event.actor.kind === "worker" ? "workers" : rule.lane,
      tone: this.config.statusTone?.(event) ?? rule.tone,
    };
  }

  lanes(): TimelineLane[] {
    const used = new Set(this.events.map((event) => this.rule(event).lane));
    const ordered = Object.values(this.config).flatMap((rule) =>
      typeof rule === "function"
        ? []
        : [rule.lane, ...(rule.lane === "orchestrator" ? ["workers"] : [])],
    ) as TimelineLane[];
    return [...new Set(ordered)].filter((lane) => used.has(lane));
  }

  rows(): TimelineRow[] {
    const rows = new Map(
      this.events.map((event) => [event.event_id, { event, children: [] } as TimelineRow]),
    );
    const roots: TimelineRow[] = [];
    for (const row of rows.values()) {
      const ancestors = new Set([row.event.event_id]);
      let parentId = row.event.parent_event_id;
      let cycle = false;
      while (parentId && rows.has(parentId)) {
        if (ancestors.has(parentId)) {
          cycle = true;
          break;
        }
        ancestors.add(parentId);
        parentId = rows.get(parentId)!.event.parent_event_id;
      }
      const parent = row.event.parent_event_id && rows.get(row.event.parent_event_id);
      if (parent && !cycle) parent.children.push(row);
      else roots.push(row);
    }
    return roots;
  }

  // State contains toggles from the configured default. "always" starts open.
  isFolded(eventId: string, state: Set<string>): boolean {
    const event = this.events.find((item) => item.event_id === eventId);
    if (!event || this.rule(event).fold === "never") return false;
    return (this.rule(event).fold === "folded") !== state.has(eventId);
  }

  summary(): { turns: number; wakes: number; retries: number; mail: number; children: number } {
    const count = (kind: EpisodeTimelineEventKind) =>
      this.events.filter((event) => event.kind === kind).length;
    return {
      turns: count("turn"),
      wakes: count("wake"),
      retries: count("retry"),
      mail: count("mail"),
      children: count("child"),
    };
  }
}
