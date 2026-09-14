import { useEffect, useId, useMemo, useState } from "react";
import { loadEpisodeMessages } from "../api";
import { MarkdownAnswer } from "../chatMarkdown";
import { experimentBoardHref, experimentBoardRouteToken } from "../experimentBoard";
import {
  EpisodeTimeline as TimelineModel,
  type TimelineRenderConfig,
  type TimelineRow,
} from "../timeline";
import type { EpisodeTimelineEvent, ExperimentLoopIndexEntry, GraphTargetRef } from "../types";

export function EpisodeTimeline({
  events,
  apiBase,
  episodeId,
  truncated = false,
  config,
  onInspectTask,
  childExperiments = [],
  onOpenExperimentEntry,
  graphTarget,
}: {
  events: EpisodeTimelineEvent[];
  apiBase: string;
  episodeId: string;
  truncated?: boolean;
  config?: TimelineRenderConfig;
  graphTarget?: GraphTargetRef;
  onInspectTask: (taskId: string) => void;
  childExperiments?: ExperimentLoopIndexEntry[];
  onOpenExperimentEntry?: (entry: ExperimentLoopIndexEntry) => void;
}) {
  const id = useId();
  const timeline = useMemo(() => new TimelineModel(events, config), [events, config]);
  const [foldState, setFoldState] = useState(new Set<string>());
  const [bodies, setBodies] = useState<Record<string, string>>({});
  const [mailError, setMailError] = useState<string | null>(null);
  const lanes = timeline.lanes();
  const openMail = timeline.events.filter(
    (event) => event.kind === "mail" && !timeline.isFolded(event.event_id, foldState),
  );
  // Mail belongs to the chain member that received it, which is not always the
  // displayed episode; each owning episode's collection is read once.
  const missingMailEpisodes = [
    ...new Set(
      openMail
        .filter((event) => event.links.message_id && bodies[event.links.message_id] === undefined)
        .map((event) => event.links.episode_id ?? episodeId),
    ),
  ];
  const missingMail = missingMailEpisodes.length > 0;

  useEffect(() => {
    setBodies({});
    setFoldState(new Set());
    setMailError(null);
  }, [apiBase, episodeId]);
  useEffect(() => {
    if (!missingMail) return;
    let cancelled = false;
    setMailError(null);
    void Promise.all(missingMailEpisodes.map((owner) => loadEpisodeMessages(apiBase, owner)))
      .then((collections) => {
        if (cancelled) return;
        const fullBodies = {
          ...bodies,
          ...Object.fromEntries(
            collections.flat().map((message) => [message.message_id, message.body]),
          ),
        };
        setBodies(fullBodies);
        if (
          openMail.some(
            (event) => event.links.message_id && fullBodies[event.links.message_id] === undefined,
          )
        ) {
          setMailError("The full message is unavailable.");
        }
      })
      .catch((error: unknown) => {
        if (!cancelled)
          setMailError(error instanceof Error ? error.message : "Unable to load messages.");
      });
    return () => {
      cancelled = true;
    };
    // One bulk read when an uncached message is opened; event refreshes can add new mail.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, episodeId, missingMail, events]);

  function renderRow(row: TimelineRow) {
    const { event } = row;
    const rule = timeline.rule(event);
    const folded = timeline.isFolded(event.event_id, foldState);
    const foldable = rule.fold !== "never";
    const detailId = `${id}-${event.event_id}`;
    const taskId = event.links.task_id;
    const child = childExperiments.find((entry) =>
      event.links.episode_id
        ? entry.episode.episode_id === event.links.episode_id
        : entry.node.id === event.links.control_node_id,
    );
    const isTask =
      (["turn", "wake", "retry"].includes(event.kind) || event.kind === "child") && taskId;
    const childHref = child
      ? experimentBoardHref(child.project_id, experimentBoardRouteToken(child))
      : event.kind === "child" &&
          event.links.episode_id &&
          event.links.control_node_id &&
          graphTarget
        ? experimentBoardHref(decodeURIComponent(apiBase.split("/").at(-1) ?? ""), {
            experiment_id: event.links.control_node_id,
            episode_id: event.links.episode_id,
            graph_target: graphTarget,
            parent_episode_id: episodeId,
          })
        : null;
    const toggle = () =>
      setFoldState((previous) => {
        const next = new Set(previous);
        if (next.has(event.event_id)) next.delete(event.event_id);
        else next.add(event.event_id);
        return next;
      });
    const heading = (
      <>
        <span className="episode-timeline-glyph" aria-hidden="true">
          {rule.glyph}
        </span>
        <strong>{event.title}</strong>
        <span className="episode-timeline-actor">
          {event.actor.member?.display_name ?? event.actor.label}
        </span>
        <time dateTime={event.at}>{formatTimestamp(event.at)}</time>
        {event.status && (
          <span className={`status-pill ${event.status} episode-timeline-tone-${rule.tone}`}>
            {event.status.replaceAll("_", " ")}
          </span>
        )}
      </>
    );
    return (
      <li
        key={event.event_id}
        className={`episode-timeline-row episode-timeline-side-${lanes.indexOf(rule.lane) % 2 ? "right" : "left"}`}
        data-event-id={event.event_id}
        data-lane={rule.lane}
      >
        <article className={`episode-timeline-card episode-timeline-tone-${rule.tone}`}>
          <header>
            {isTask ? (
              <button
                type="button"
                className="episode-timeline-heading"
                onClick={() => onInspectTask(taskId)}
              >
                {heading}
              </button>
            ) : childHref ? (
              <a
                className="episode-timeline-heading"
                href={childHref}
                onClick={(click) => {
                  if (
                    child &&
                    onOpenExperimentEntry &&
                    click.button === 0 &&
                    !click.metaKey &&
                    !click.ctrlKey &&
                    !click.shiftKey &&
                    !click.altKey
                  ) {
                    click.preventDefault();
                    onOpenExperimentEntry(child);
                  }
                }}
              >
                {heading}
              </a>
            ) : (
              <div className="episode-timeline-heading">{heading}</div>
            )}
            {foldable && (
              <button
                type="button"
                className="episode-timeline-fold"
                aria-label={`${folded ? "Expand" : "Collapse"} ${event.title}`}
                aria-expanded={!folded}
                aria-controls={detailId}
                onClick={toggle}
              >
                {folded ? "+" : "−"}
              </button>
            )}
          </header>
          {(event.cause || event.provenance === "unknown") && (
            <div className="episode-timeline-tags">
              {event.cause && <span>{event.cause}</span>}
              {event.provenance === "unknown" && (
                <span className="episode-timeline-unknown">provenance unknown</span>
              )}
            </div>
          )}
          {folded && event.kind === "mail" && (
            <button
              type="button"
              className="episode-timeline-preview"
              aria-expanded={false}
              aria-controls={detailId}
              onClick={toggle}
            >
              {event.detail?.split("\n")[0]}
            </button>
          )}
          {!folded && (
            <div className="episode-timeline-detail" id={detailId}>
              {event.kind === "mail" ? (
                <>
                  <div className="chat-markdown">
                    <MarkdownAnswer
                      text={
                        (event.links.message_id && bodies[event.links.message_id]) ??
                        event.detail ??
                        ""
                      }
                    />
                  </div>
                  {event.links.message_id && bodies[event.links.message_id] === undefined && (
                    <span role={mailError ? "alert" : "status"}>
                      {mailError ?? "Loading message…"}
                    </span>
                  )}
                </>
              ) : (
                event.detail && <p>{event.detail}</p>
              )}
            </div>
          )}
        </article>
        {row.children.length > 0 && (
          <ol className="episode-timeline-children">{row.children.map(renderRow)}</ol>
        )}
      </li>
    );
  }
  return (
    <section className="episode-timeline" aria-label="Episode timeline">
      <header className="episode-timeline-header">
        <h3>Timeline</h3>
        {truncated && <span>Earlier events omitted</span>}
      </header>
      <div className="episode-timeline-lanes" aria-label="Timeline lanes">
        {lanes.map((lane) => (
          <span key={lane} data-lane={lane}>
            {lane}
          </span>
        ))}
      </div>
      <ol className="episode-timeline-axis">{timeline.rows().map(renderRow)}</ol>
    </section>
  );
}

function formatTimestamp(value: string): string {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}
