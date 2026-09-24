import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { fetchTimelineText } from "../api";
import {
  timelineRows,
  timelineBounds,
  clampWindow,
  zoomWindow,
  panWindow,
  timelineRelated,
  timelineWakeRows,
  timelineSummary,
  timelineActorLabel,
  timelineOutcome,
  messageDisposition as disposition,
} from "../timeline";
import type {
  EpisodeTimelineResponse,
  EpisodeTimelineSpan,
  ExperimentLoopIndexEntry,
  GraphTargetRef,
} from "../types";

const groups = {
  human: "People",
  orchestrator: "Orchestrator",
  agent: "Experiment agent",
  worker: "Workers",
  experiment: "Experiments",
  watcher: "Watchers",
};
const stamp = (s: string | number) =>
  new Date(s).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
const clock = (n: number) =>
  new Date(n).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const tone = (s: string | null) =>
  s === "exhausted"
    ? "warning"
    : ["failed", "error", "interrupted"].includes(s ?? "")
      ? "danger"
      : ["running", "active", "queued", "armed"].includes(s ?? "")
        ? "active"
        : ["completed", "succeeded", "ready"].includes(s ?? "")
          ? "success"
          : "neutral";

export function EpisodeTimeline({
  response: data,
  apiBase,
  episodeId,
  onInspectTask,
  childExperiments = [],
  onOpenExperimentEntry,
}: {
  response: EpisodeTimelineResponse;
  apiBase: string;
  episodeId: string;
  graphTarget?: GraphTargetRef;
  onInspectTask: (id: string) => void;
  childExperiments?: ExperimentLoopIndexEntry[];
  onOpenExperimentEntry?: (entry: ExperimentLoopIndexEntry) => void;
}) {
  const uid = useId().replaceAll(":", "");
  const host = useRef<HTMLDivElement>(null),
    svg = useRef<SVGSVGElement>(null),
    pop = useRef<HTMLDivElement>(null);
  const rows = useMemo(() => timelineRows(data, childExperiments), [data, childExperiments]);
  const bounds = useMemo(() => timelineBounds(data), [data]);
  const [window, setWindow] = useState<[number, number] | null>(null);
  const view = window ? clampWindow(window, bounds) : bounds;
  const [selected, setSelected] = useState<string | null>(null);
  const [card, setCard] = useState<{ id: string; x: number; y: number } | null>(null);
  const [text, setText] = useState<{ body?: string; error?: string }>({});
  const [width, setWidth] = useState(900);
  const [scrollLeft, setScrollLeft] = useState(0);
  const drag = useRef<{ x: number; view: [number, number]; moved: boolean } | null>(null);
  const suppress = useRef(false);
  const pointers = useRef(new Map<number, number>());
  const pinch = useRef<number | null>(null);
  const trigger = useRef<Element | null>(null);
  const related = selected ? timelineRelated(data, selected) : null;
  const summary = timelineSummary(data),
    wakes = timelineWakeRows(data, childExperiments);
  const left = 190,
    right = width - 110,
    track = right - left;
  const x = (t: string | number) =>
    left +
    Math.max(
      0,
      Math.min(1, ((typeof t === "string" ? Date.parse(t) : t) - view[0]) / (view[1] - view[0])),
    ) *
      track;
  const visible = (a: string, b: string | null = a) =>
    Date.parse(b ?? data.generated_at) >= view[0] && Date.parse(a) <= view[1];
  let bottom = 46,
    previous = "";
  const layout = rows.map((row) => {
    const heading = row.kind !== previous;
    previous = row.kind;
    if (heading) bottom += 26;
    const y = bottom + 21;
    bottom += 44;
    return { ...row, y, heading };
  });
  const height = bottom + 14;
  const actor = (id: string | null) => data.actors.find((a) => a.actor_id === id);
  const span = (id: string | null) => data.spans.find((s) => s.span_id === id);
  const rowY = (key: string | undefined) => layout.find((r) => r.rowKey === key)?.y ?? height - 6;
  const actorY = (id: string | null) => rowY(actor(id)?.row_key);
  const endpoint = (id: string | null, at: string) => ({
    x: actor(id) ? x(at) : left,
    y: actorY(id),
  });
  const item = (id: string) =>
    [...data.handoffs, ...data.messages, ...data.signals].find((i) => i.item_id === id);
  const actorLabel = (id: string | null) => {
    const found = actor(id);
    return found ? timelineActorLabel(found, childExperiments) : "Unknown actor";
  };
  const title = (id: string): string =>
    (actor(id) ? actorLabel(id) : undefined) ??
    (span(id)
      ? span(id)!.kind === "report"
        ? "Report"
        : span(id)!.kind === "attempt"
          ? `Attempt ${span(id)!.attempt ?? 1}`
          : `Turn ${
              data.spans
                .filter((s) => s.actor_id === span(id)!.actor_id && s.kind !== "report")
                .sort((a, b) => a.started_at.localeCompare(b.started_at))
                .findIndex((s) => s.span_id === id) + 1
            }`
      : data.messages.some((m) => m.item_id === id)
        ? "Message"
        : (data.handoffs.find((h) => h.item_id === id)?.kind ??
          data.signals.find((s) => s.item_id === id)?.event ??
          id));
  const dim = (ids: string[]) =>
    related && !ids.some((id) => related.has(id)) ? " roster-dim" : "";
  const close = () => {
    setCard(null);
    if (trigger.current instanceof SVGElement || trigger.current instanceof HTMLElement)
      trigger.current.focus();
  };
  function choose(id: string, anchor?: Element) {
    if (suppress.current && anchor instanceof SVGElement) {
      suppress.current = false;
      return;
    }
    setSelected(id);
    if (anchor && host.current) {
      const a = (anchor.querySelector("[data-pop-anchor]") ?? anchor).getBoundingClientRect(),
        h = host.current.getBoundingClientRect();
      trigger.current = anchor;
      setCard({
        id,
        x: Math.max(8, Math.min(a.left - h.left, h.width - 430)),
        y: a.bottom - h.top + 8,
      });
    } else setCard(null);
  }

  function labelHit(id: string | undefined, label: string, children: ReactNode) {
    return id ? hit(id, label, children) : <g aria-label={label}>{children}</g>;
  }

  function hit(id: string, label: string, children: ReactNode, extra = "") {
    return (
      <g
        key={id}
        role="button"
        tabIndex={0}
        aria-label={label}
        aria-pressed={selected === id}
        data-roster-id={id}
        className={`roster-hit${dim([id])} ${extra}`}
        onClick={(e) => {
          e.stopPropagation();
          choose(id, e.currentTarget);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            suppress.current = false;
            choose(id, e.currentTarget);
          }
        }}
      >
        {children}
      </g>
    );
  }
  const refButton = (id: string, label = title(id)) => (
    <button
      className="roster-ref"
      type="button"
      key={id}
      onClick={(e) => {
        e.stopPropagation();
        // Anchor the card at the item in the chart, not at this link.
        choose(id, host.current?.querySelector(`[data-roster-id="${id}"]`) ?? e.currentTarget);
      }}
    >
      {label}
    </button>
  );
  useEffect(() => {
    setWindow(null);
    setSelected(null);
    setCard(null);
  }, [apiBase, episodeId]);
  useEffect(() => {
    if (!host.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setWidth(Math.max(760, entry.contentRect.width)),
    );
    observer.observe(host.current);
    return () => observer.disconnect();
  }, []);
  const opened = card ? item(card.id) : undefined;
  const textRef = opened && "text_ref" in opened ? opened.text_ref : null;
  useEffect(() => {
    if (!textRef) return;
    let cancelled = false;
    setText({});
    void fetchTimelineText(apiBase, episodeId, textRef).then(
      (result) => {
        if (!cancelled) setText({ body: result.body });
      },
      (error) => {
        if (!cancelled)
          setText({ error: error instanceof Error ? error.message : "Unable to load text." });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [apiBase, episodeId, textRef]);
  useEffect(() => {
    if (!card) return;
    pop.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const outside = (e: PointerEvent) => {
      if (!pop.current?.contains(e.target as Node) && !trigger.current?.contains(e.target as Node))
        setCard(null);
    };
    document.addEventListener("keydown", key);
    document.addEventListener("pointerdown", outside);
    return () => {
      document.removeEventListener("keydown", key);
      document.removeEventListener("pointerdown", outside);
    };
  }, [card]);
  const timeAt = (clientX: number) => {
    const rect = svg.current!.getBoundingClientRect();
    return view[0] + ((clientX - rect.left - left) / track) * (view[1] - view[0]);
  };
  useEffect(() => {
    const el = svg.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        setWindow(zoomWindow(view, bounds, timeAt(e.clientX), Math.exp(e.deltaY * 0.005)));
        setCard(null);
      } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault();
        setWindow(
          panWindow(
            view,
            bounds,
            ((e.shiftKey ? e.deltaY || e.deltaX : e.deltaX) / track) * (view[1] - view[0]),
          ),
        );
        setCard(null);
      }
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  });
  const ticks: number[] = [];
  const step =
    [15, 30, 60, 120, 240, 480, 1440, 2880, 10080]
      .map((n) => n * 60000)
      .find((n) => (view[1] - view[0]) / n < track / 65) ?? 604800000;
  for (let t = Math.ceil(view[0] / step) * step; t <= view[1]; t += step) ticks.push(t);
  let tickLabelEnd = -Infinity;
  const dates = [view[0]];
  const day = new Date(view[0]);
  day.setHours(24, 0, 0, 0);
  while (day.getTime() < view[1]) {
    dates.push(day.getTime());
    day.setDate(day.getDate() + 1);
  }
  const live = data.members.some((m) => !m.ended_at);
  const labels = new Map<string, number>();
  const selectedMark = data.marks.find((m) => m.item_id === selected);
  const selectedActor = selected ? actor(selected) : undefined,
    selectedSpan = selected ? span(selected) : undefined;
  const wake = wakes.find((w) => w.span.span_id === selectedSpan?.span_id);
  const child = childExperiments.find(
    (e) =>
      e.episode.episode_id ===
      (selectedActor ?? actor(selectedSpan?.actor_id ?? null))?.links.episode_id,
  );
  const duration = (s: EpisodeTimelineSpan) => {
    const seconds = Math.max(
      0,
      Math.round(
        (Date.parse(s.finished_at ?? data.generated_at) - Date.parse(s.started_at)) / 1000,
      ),
    );
    return seconds < 60 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
  };
  return (
    <section className="episode-roster" aria-label="Episode timeline">
      <div className="roster-summary">
        {summary.actors.map((g) => (
          <div key={g.kind}>
            <strong>
              {g.total} {groups[g.kind]}
            </strong>
            {g.kind !== "human" && (
              <span>
                {Object.entries(g.outcomes)
                  .map(([key, n]) => `${n} ${key}`)
                  .join(" · ")}
              </span>
            )}
          </div>
        ))}
        <div>
          <strong>
            {summary.handoffs} hand-offs · {summary.totalMessages} messages
          </strong>
          <span>
            {Object.entries(summary.messages)
              .filter(([, n]) => n)
              .map(([key, n]) => `${n} ${disposition[key as keyof typeof disposition]}`)
              .join(" · ")}
          </span>
        </div>
      </div>
      {data.truncated && <p>Counts cover the shown items.</p>}
      <div className="roster-toolbar" aria-label="Timeline zoom">
        <button
          aria-label="Zoom out"
          onClick={() => setWindow(zoomWindow(view, bounds, (view[0] + view[1]) / 2, 1.6))}
        >
          −
        </button>
        <button
          aria-label="Zoom in"
          onClick={() => setWindow(zoomWindow(view, bounds, (view[0] + view[1]) / 2, 0.625))}
        >
          +
        </button>
        <button onClick={() => setWindow(null)} aria-pressed={!window}>
          Whole run
        </button>
        <button onClick={() => setWindow(clampWindow([bounds[0], bounds[0] + 90 * 60000], bounds))}>
          First 90 min
        </button>
        {selected && (
          <button
            onClick={() => {
              setSelected(null);
              setCard(null);
            }}
          >
            Clear selection
          </button>
        )}
      </div>
      <div className="roster-chart-host" ref={host}>
        <div
          className="roster-scroll"
          onScroll={(e) => {
            setScrollLeft(e.currentTarget.scrollLeft);
            setCard(null);
          }}
        >
          <svg
            ref={svg}
            width={width}
            height={height}
            role="group"
            aria-label="Agent roster chart"
            onDoubleClick={(e) => {
              setWindow(zoomWindow(view, bounds, timeAt(e.clientX), e.shiftKey ? 1.6 : 0.625));
              setCard(null);
            }}
            onPointerDown={(e) => {
              if (e.button !== 0) return;
              suppress.current = false;
              pointers.current.set(e.pointerId, e.clientX);
              drag.current = { x: e.clientX, view: [...view], moved: false };
              if (pointers.current.size === 2)
                pinch.current = Math.abs(
                  [...pointers.current.values()][0] - [...pointers.current.values()][1],
                );
            }}
            onPointerMove={(e) => {
              if (!pointers.current.has(e.pointerId)) return;
              pointers.current.set(e.pointerId, e.clientX);
              if (pointers.current.size === 2) {
                const [a, b] = [...pointers.current.values()],
                  distance = Math.abs(a - b);
                if (pinch.current && distance > 0)
                  setWindow(
                    zoomWindow(view, bounds, timeAt((a + b) / 2), pinch.current / distance),
                  );
                pinch.current = distance;
                suppress.current = true;
                setCard(null);
                return;
              }
              if (!drag.current) return;
              const delta = e.clientX - drag.current.x;
              if (Math.abs(delta) > 5) {
                drag.current.moved = true;
                suppress.current = true;
                svg.current?.setPointerCapture(e.pointerId);
                setCard(null);
              }
              if (drag.current.moved)
                setWindow(
                  panWindow(
                    drag.current.view,
                    bounds,
                    (-delta / track) * (drag.current.view[1] - drag.current.view[0]),
                  ),
                );
            }}
            onPointerUp={(e) => {
              pointers.current.delete(e.pointerId);
              pinch.current = null;
              drag.current = null;
            }}
            onPointerCancel={(e) => {
              pointers.current.delete(e.pointerId);
              drag.current = null;
              pinch.current = null;
            }}
          >
            <defs>
              <marker
                id={`${uid}-arrow`}
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="6"
                markerHeight="6"
                orient="auto"
              >
                <path d="M0 0 L8 4 L0 8Z" fill="context-stroke" />
              </marker>
              <clipPath id={`${uid}-track`}>
                <rect x={left - 10} width={width - left + 10} height={height} />
              </clipPath>
            </defs>
            {dates
              .filter((t, i) => i === 0 || x(t) - x(dates[i - 1]) > 70)
              .map((t) => (
                <text key={t} x={x(t)} y={13} className="roster-tick">
                  {new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                </text>
              ))}
            {ticks.flatMap((t) => {
              const label = clock(t),
                labelWidth = label.length * 7;
              const startAligned = x(t) - left < labelWidth / 2;
              const labelLeft = startAligned ? x(t) : x(t) - labelWidth / 2;
              if (labelLeft < tickLabelEnd + 8) return [];
              tickLabelEnd = labelLeft + labelWidth;
              return [
                <text
                  key={t}
                  x={x(t)}
                  y={31}
                  textAnchor={startAligned ? "start" : "middle"}
                  className="roster-tick"
                >
                  {label}
                </text>,
              ];
            })}
            {layout
              .filter((r) => r.heading)
              .map((r) => (
                <line
                  key={r.rowKey}
                  x1={left}
                  x2={width}
                  y1={r.y - 24}
                  y2={r.y - 24}
                  className="roster-rule"
                />
              ))}
            <g clipPath={`url(#${uid}-track)`}>
              {live && visible(data.generated_at) && (
                <text x={x(data.generated_at) + 4} y={43} className="roster-tick">
                  now
                </text>
              )}
              {live && visible(data.generated_at) && (
                <line
                  x1={x(data.generated_at)}
                  x2={x(data.generated_at)}
                  y1={40}
                  y2={height}
                  className="roster-now"
                >
                  <title>now</title>
                </line>
              )}
              {data.actors
                .filter((a) => a.started_by_span_id && a.started_at && visible(a.started_at))
                .map((a) => {
                  const s = span(a.started_by_span_id);
                  return (
                    <line
                      key={a.actor_id}
                      x1={s ? x(a.started_at!) : left}
                      x2={x(a.started_at!)}
                      y1={s ? actorY(s.actor_id) : height - 6}
                      y2={actorY(a.actor_id)}
                      className={`roster-start${dim([a.actor_id, a.started_by_span_id!])}`}
                    />
                  );
                })}
              {data.actors
                .filter(
                  (a) => a.kind !== "human" && a.started_at && visible(a.started_at, a.ended_at),
                )
                .map((a) =>
                  hit(
                    a.actor_id,
                    `${actorLabel(a.actor_id)} ${timelineOutcome(a)}`,
                    <>
                      {a.kind === "experiment" || a.kind === "watcher" ? (
                        <rect
                          x={x(a.started_at!)}
                          y={actorY(a.actor_id) - 6}
                          width={Math.max(4, x(a.ended_at ?? data.generated_at) - x(a.started_at!))}
                          height={12}
                          rx={3}
                          className={`roster-rail roster-${tone(a.outcome ?? (a.ended_at ? null : "running"))}`}
                        />
                      ) : (
                        a.kind !== "human" && (
                          <line
                            x1={x(a.started_at!)}
                            x2={x(a.ended_at ?? data.generated_at)}
                            y1={actorY(a.actor_id)}
                            y2={actorY(a.actor_id)}
                            className="roster-rule"
                            strokeDasharray={a.kind === "worker" ? undefined : "3 3"}
                          />
                        )
                      )}
                      {a.kind === "experiment" && visible(a.ended_at ?? data.generated_at) && (
                        <text
                          x={x(a.ended_at ?? data.generated_at) + 6}
                          y={actorY(a.actor_id) + 4}
                          className={`roster-badge roster-${tone(a.outcome ?? "running")}`}
                        >
                          {timelineOutcome(a)}
                          {a.outcome === "exhausted" &&
                            (() => {
                              const budget = childExperiments.find(
                                (c) => c.episode.episode_id === a.links.episode_id,
                              )?.episode.budget;
                              return budget
                                ? ` ${budget.invocations_used}/${budget.invocation_ceiling}`
                                : "";
                            })()}
                        </text>
                      )}
                    </>,
                  ),
                )}
              {[...data.spans]
                .sort((a, b) => a.started_at.localeCompare(b.started_at))
                .filter((s) => visible(s.started_at, s.finished_at))
                .map((s) => {
                  const xx = x(s.started_at),
                    w = Math.max(4, x(s.finished_at ?? data.generated_at) - xx),
                    yy = actorY(s.actor_id),
                    key = actor(s.actor_id)?.row_key ?? s.actor_id,
                    last = labels.get(key) ?? -Infinity,
                    center = xx + w / 2,
                    show = center - last >= (s.kind === "report" ? 38 : 20);
                  if (show) labels.set(key, center);
                  return hit(
                    s.span_id,
                    `${title(s.span_id)} · ${s.status}`,
                    <>
                      <rect
                        x={xx}
                        y={yy - 9}
                        width={w}
                        height={18}
                        rx={2}
                        className={`roster-block roster-${tone(actor(s.actor_id)?.outcome === "exhausted" && !["failed", "interrupted"].includes(s.status) ? "exhausted" : s.status)}`}
                      />
                      {show && (
                        <text x={center} y={yy - 13} textAnchor="middle" className="roster-number">
                          {s.kind === "report"
                            ? "Report"
                            : title(s.span_id).replace(/^(Turn|Attempt) /, "")}
                        </text>
                      )}
                      <title>{s.headline ?? s.error ?? s.status}</title>
                    </>,
                  );
                })}
              {data.marks
                .filter((m) => m.kind === "stop_requested" && visible(m.at))
                .map((m) =>
                  hit(
                    m.item_id,
                    m.kind.replaceAll("_", " "),
                    <>
                      <rect
                        x={x(m.at) - 4}
                        y={actorY(m.actor_id) - 5}
                        width={9}
                        height={10}
                        className="roster-mark"
                      />
                      <text x={x(m.at) + 9} y={actorY(m.actor_id) + 4} className="roster-tick">
                        stop requested
                      </text>
                    </>,
                  ),
                )}
              {data.signals.map((s) => {
                const target = span(s.landed_span_id),
                  start = s.recorded_at,
                  end =
                    s.landing === "woke"
                      ? (target?.started_at ?? s.landed_at ?? start)
                      : (s.landed_at ?? start);
                if (!visible(start, end)) return null;
                const ax = layout.some((r) => r.rowKey === s.source_row_key) ? x(start) : left,
                  ay = rowY(s.source_row_key),
                  bx = target ? x(end) : right,
                  by = target ? actorY(target.actor_id) : height - 6,
                  mx = s.landed_span_id ? (ax + bx) / 2 : ax,
                  my = s.landed_span_id ? (ay + by) / 2 : ay;
                return hit(
                  s.item_id,
                  `${s.kind} ${s.event}`,
                  <>
                    {s.landed_span_id && (
                      <line
                        x1={ax}
                        y1={ay}
                        x2={bx}
                        y2={by}
                        className={`roster-signal roster-${tone(/failed|exhausted/.test(s.event) ? "failed" : (s.state ?? s.event))}`}
                        strokeDasharray={s.landing === "woke" ? "5 4" : "1 4"}
                      />
                    )}
                    {s.kind === "watcher" ? (
                      <>
                        <circle cx={mx} cy={my} r={6} className="roster-signal-icon" />
                        <circle cx={mx} cy={my} r={2} fill="currentColor" />
                      </>
                    ) : (
                      <path d={`M${mx} ${my - 6} l5 6 l-5 6 l-5 -6Z`} fill="currentColor" />
                    )}
                    <circle data-pop-anchor="true" cx={mx} cy={my} r={12} fill="transparent" />
                  </>,
                  `roster-${tone(/failed|exhausted/.test(s.event) ? "failed" : (s.state ?? s.event))}`,
                );
              })}
              {data.handoffs
                .filter((h) => visible(h.at))
                .map((h) => {
                  const s = span(h.from_span_id),
                    a = actor(h.to_actor_id),
                    ax = s ? x(h.at) : left,
                    bx = a ? x(h.at) : right,
                    ay = s ? actorY(s.actor_id) : height - 6,
                    by = actorY(h.to_actor_id),
                    mx = (ax + bx) / 2,
                    my = (ay + by) / 2;
                  return hit(
                    h.item_id,
                    `Hand-off ${h.kind}: ${h.preview}`,
                    <>
                      <line x1={ax} y1={ay} x2={bx} y2={by} className="roster-start" />
                      <rect
                        x={mx - 6}
                        y={my - 8}
                        width={12}
                        height={16}
                        rx={1}
                        data-pop-anchor="true"
                        className="roster-document"
                      />
                      <path
                        d={`M${mx - 3} ${my - 3} h6 M${mx - 3} ${my} h6 M${mx - 3} ${my + 3} h4`}
                        className="roster-start"
                      />
                      <circle cx={mx} cy={my} r={13} fill="transparent" />
                    </>,
                  );
                })}
              {data.messages
                .filter((m) => visible(m.sent_at, m.delivered_at ?? m.sent_at))
                .map((m) => {
                  const from = endpoint(m.from_actor_id, m.sent_at),
                    to = endpoint(m.to_actor_id, m.delivered_at ?? m.sent_at);
                  const bad = ["failed_attempt", "undelivered"].includes(m.disposition),
                    undelivered = m.disposition === "undelivered";
                  if (m.sent_span_id && !span(m.sent_span_id)) from.x = left;
                  if (m.delivered_span_id && !span(m.delivered_span_id)) to.x = right;
                  const endY = to.y - (undelivered ? 12 : 0);
                  return (
                    <g key={m.item_id}>
                      <g className={dim([m.item_id])} pointerEvents="none">
                        <path
                          d={`M${from.x} ${from.y} C${from.x} ${(from.y + endY) / 2} ${to.x} ${(from.y + endY) / 2} ${to.x} ${endY}`}
                          className={`roster-message ${bad ? "roster-danger" : ""}`}
                          strokeDasharray={undelivered ? "4 3" : undefined}
                          markerEnd={undelivered ? undefined : `url(#${uid}-arrow)`}
                        />
                        {m.disposition === "failed_attempt" && (
                          <circle cx={to.x} cy={to.y} r={10} className="roster-failure-ring" />
                        )}
                        {undelivered && (
                          <>
                            <path
                              d={`M${to.x - 4} ${endY - 4} l8 8 M${to.x + 4} ${endY - 4} l-8 8`}
                              className="roster-cross"
                            />
                            <text x={to.x + 9} y={endY + 4} className="roster-undelivered">
                              not delivered
                            </text>
                          </>
                        )}
                      </g>
                      {hit(
                        m.item_id,
                        `Message from ${actorLabel(m.from_actor_id)} to ${actorLabel(m.to_actor_id)}`,
                        <>
                          <rect
                            x={from.x - 12}
                            y={from.y - 12}
                            width={24}
                            height={24}
                            fill="transparent"
                          />
                          <rect
                            x={from.x - 7}
                            y={from.y - 5}
                            width={14}
                            height={10}
                            rx={1}
                            data-pop-anchor="true"
                            className={`roster-envelope ${bad ? "roster-danger" : ""}`}
                          />
                          <path
                            d={`M${from.x - 7} ${from.y - 5} l7 6 l7 -6`}
                            className="roster-envelope-fold"
                          />
                          <title>{disposition[m.disposition]}</title>
                        </>,
                      )}
                    </g>
                  );
                })}
            </g>
            <g transform={`translate(${scrollLeft},0)`}>
              <rect width={left - 10} height={height} className="roster-label-surface" />
              {layout.map((r) => (
                <g key={r.rowKey}>
                  {r.heading && (
                    <>
                      <text x={8} y={r.y - 30} className="roster-group">
                        {groups[r.kind]}
                      </text>
                      <line x1={0} x2={left} y1={r.y - 24} y2={r.y - 24} className="roster-rule" />
                    </>
                  )}
                  {labelHit(
                    r.actors.at(-1)?.actor_id,
                    r.label,
                    <>
                      <rect
                        x={0}
                        y={r.y - 20}
                        width={left - 12}
                        height={40}
                        rx={5}
                        className="roster-label-bg"
                      />
                      <text x={8} y={r.y - 2}>
                        {r.label.length > 25 ? r.label.slice(0, 24) + "…" : r.label}
                      </text>
                      {r.subtitle && (
                        <text x={8} y={r.y + 14} className="roster-tick">
                          {r.subtitle.length > 27 ? r.subtitle.slice(0, 26) + "…" : r.subtitle}
                        </text>
                      )}
                      <title>{`${r.label}${r.subtitle ? ` · ${r.subtitle}` : ""}`}</title>
                    </>,
                  )}
                </g>
              ))}
            </g>
          </svg>
        </div>
        {card && (
          <div
            ref={pop}
            role="dialog"
            aria-label={title(card.id)}
            tabIndex={-1}
            className="roster-popover"
            style={{ left: card.x, top: card.y }}
          >
            <header>
              <strong>{title(card.id)}</strong>
              <button aria-label="Close" onClick={close}>
                ×
              </button>
            </header>
            {opened ? (
              <>
                {"disposition" in opened && (
                  <p>
                    {actorLabel(opened.from_actor_id)} → {actorLabel(opened.to_actor_id)} ·{" "}
                    {disposition[opened.disposition]} · {stamp(opened.sent_at)}
                  </p>
                )}
                {"from_span_id" in opened && (
                  <p>
                    {title(opened.from_span_id)} → {title(opened.to_actor_id)} · {stamp(opened.at)}
                  </p>
                )}
                {"armed_at" in opened && opened.armed_at && (
                  <p>
                    Armed {stamp(opened.armed_at)}
                    {opened.armed_span_id && <> · {title(opened.armed_span_id)}</>}
                  </p>
                )}
                {"landing" in opened && (
                  <p>
                    {opened.landing ?? opened.state} ·{" "}
                    {stamp(opened.landed_at ?? opened.recorded_at)}
                  </p>
                )}
                {"payload" in opened ? (
                  <pre>{JSON.stringify(opened.payload, null, 2)}</pre>
                ) : text.error ? (
                  <p role="alert">{text.error}</p>
                ) : text.body === undefined ? (
                  <p role="status">Loading text…</p>
                ) : (
                  <pre>{text.body}</pre>
                )}
              </>
            ) : (
              <>
                {selectedMark && (
                  <p>
                    {selectedMark.kind.replaceAll("_", " ")} · {stamp(selectedMark.at)}
                    {selectedMark.by_span_id && <> · {refButton(selectedMark.by_span_id)}</>}
                  </p>
                )}
                {selectedActor && (
                  <>
                    <dl>
                      <dt>Kind</dt>
                      <dd>{selectedActor.kind}</dd>
                      {selectedActor.subtitle && (
                        <>
                          <dt>Node</dt>
                          <dd>{selectedActor.subtitle}</dd>
                        </>
                      )}
                      <dt>Outcome</dt>
                      <dd>{timelineOutcome(selectedActor)}</dd>
                      {selectedActor.started_at && (
                        <>
                          <dt>Started</dt>
                          <dd>{stamp(selectedActor.started_at)}</dd>
                        </>
                      )}
                      {selectedActor.ended_at && (
                        <>
                          <dt>Ended</dt>
                          <dd>{stamp(selectedActor.ended_at)}</dd>
                        </>
                      )}
                      {selectedActor.started_by_span_id && (
                        <>
                          <dt>Started by</dt>
                          <dd>{refButton(selectedActor.started_by_span_id)}</dd>
                        </>
                      )}
                    </dl>
                    {child && onOpenExperimentEntry && (
                      <button onClick={() => onOpenExperimentEntry(child)}>Open Experiment</button>
                    )}
                    {data.spans
                      .filter((s) => s.actor_id === selectedActor.actor_id)
                      .map((s) => (
                        <div key={s.span_id}>
                          {refButton(s.span_id)} · {stamp(s.started_at)} · {s.status} ·{" "}
                          {duration(s)}
                          {s.invocation_number !== null && <> · inv {s.invocation_number}</>}
                          {s.headline && <p>{s.headline}</p>}
                          {s.error && <p className="roster-error">{s.error}</p>}
                        </div>
                      ))}
                  </>
                )}
                {selectedSpan && (
                  <>
                    <p>
                      {stamp(selectedSpan.started_at)} · {duration(selectedSpan)} ·{" "}
                      {selectedSpan.status}
                      {selectedSpan.invocation_number !== null && (
                        <> · invocation {selectedSpan.invocation_number}</>
                      )}
                    </p>
                    {wake ? (
                      <>
                        <p>{wake.cause}</p>
                        {wake.landed.length > 0 && (
                          <>
                            <h4>Landed</h4>
                            {wake.landed.map((i) => (
                              <div key={i.item_id}>{refButton(i.item_id, i.label)}</div>
                            ))}
                          </>
                        )}
                        {wake.actions.length > 0 && <h4>Did</h4>}
                        {wake.actions.map((i) => (
                          <div key={i.item_id}>
                            {i.at && <time dateTime={i.at}>{clock(Date.parse(i.at))} · </time>}
                            {i.icon ? (
                              <>
                                {i.label}{" "}
                                <button
                                  type="button"
                                  className="roster-item-icon"
                                  aria-label={i.label}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    choose(i.item_id, e.currentTarget);
                                  }}
                                >
                                  {i.icon === "document" ? (
                                    <svg
                                      width="12"
                                      height="16"
                                      viewBox="0 0 12 16"
                                      aria-hidden="true"
                                    >
                                      <rect x="1" y="1" width="10" height="14" rx="1" />
                                      <path d="M3 5h6 M3 8h6 M3 11h4" />
                                    </svg>
                                  ) : (
                                    "✉"
                                  )}
                                </button>
                              </>
                            ) : (
                              refButton(i.item_id, i.label)
                            )}
                          </div>
                        ))}
                      </>
                    ) : (
                      <p>{selectedSpan.cause}</p>
                    )}
                    {selectedSpan.headline && <blockquote>{selectedSpan.headline}</blockquote>}
                    {selectedSpan.error && <p className="roster-error">{selectedSpan.error}</p>}
                    {child && onOpenExperimentEntry && (
                      <button onClick={() => onOpenExperimentEntry(child)}>Open Experiment</button>
                    )}
                    {selectedSpan.kind !== "report" && (
                      <button onClick={() => onInspectTask(selectedSpan.task_id)}>
                        Inspect task
                      </button>
                    )}
                  </>
                )}
                {!wake && (
                  <div className="roster-related">
                    {[...(related ?? [])]
                      .filter((id) => id !== selected && (actor(id) || span(id) || item(id)))
                      .map((id) => refButton(id))}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
