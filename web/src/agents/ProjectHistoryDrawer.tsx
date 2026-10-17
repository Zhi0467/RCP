import { Clock3, ExternalLink, GitFork, MoreHorizontal, RotateCcw, X } from "lucide-react";
import { useRef, useState } from "react";
import { taskKindLabel, taskStatusLabel } from "./agentTasks";
import type { AgentTask, RevisionSummary } from "../core/types";
import { EpisodeReportLink } from "../experiments/EpisodeReportLink";

interface Props {
  projectId: string;
  summaries: RevisionSummary[];
  tasks: AgentTask[];
  loading: boolean;
  error: string | null;
  onInspectTask: (taskId: string) => void;
  episodeReportHref: (episodeId: string) => string;
  onClose: () => void;
  onFork?: (revision: number, requestId: string) => Promise<void>;
  forkDisabled?: boolean;
}

type HistoryEntry =
  | { kind: "revision"; summary: RevisionSummary }
  | { kind: "episode"; episodeId: string; summaries: RevisionSummary[] };

export function ProjectHistoryDrawer({
  projectId,
  summaries,
  tasks,
  loading,
  error,
  onInspectTask,
  episodeReportHref,
  onClose,
  onFork,
  forkDisabled,
}: Props) {
  const historyEntries = groupEpisodeRevisions(summaries);

  return (
    <div
      className="drawer-scrim"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <aside className="detail-drawer run-inspector" aria-label="Project history">
        <header>
          <h2>Project history</h2>
          <button className="icon-button" aria-label="Close project history" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="run-inspector-body">
          <nav className="run-history" aria-label="Agent tasks">
            <span className="eyebrow">Agent tasks</span>
            {tasks.map((task) => (
              <button
                data-task-id={task.operation_id}
                key={task.operation_id}
                onClick={() => onInspectTask(task.operation_id)}
                type="button"
              >
                <span className={`run-state-dot ${task.status}`} />
                <span className="run-history-copy">
                  <strong>
                    {taskKindLabel(task.kind)} · attempt {task.attempt}
                  </strong>
                  <span className="run-history-meta">
                    {taskStatusLabel(task)} · {formatTimestamp(task.created_at)}
                  </span>
                  {task.degradation && (
                    <span className="run-history-degraded">{task.degradation}</span>
                  )}
                </span>
              </button>
            ))}
            {tasks.length === 0 && <span className="quiet-empty">No Agent tasks yet.</span>}
          </nav>

          <div className="run-inspector-detail">
            <section aria-label="Project revision summaries">
              <h4>Project revisions</h4>
              {loading ? (
                <div className="quiet-empty" role="status">
                  Loading project revisions…
                </div>
              ) : error ? (
                <div className="quiet-empty" role="alert">
                  {error}
                </div>
              ) : historyEntries.length > 0 ? (
                <ol className="run-events">
                  {historyEntries.map((entry) =>
                    entry.kind === "episode" ? (
                      <EpisodeRevisionGroup
                        projectId={projectId}
                        episodeId={entry.episodeId}
                        summaries={entry.summaries}
                        episodeReportHref={episodeReportHref}
                        onFork={onFork}
                        forkDisabled={forkDisabled}
                        key={`episode:${entry.episodeId}`}
                      />
                    ) : (
                      <RevisionItem
                        summary={entry.summary}
                        onFork={onFork}
                        forkDisabled={forkDisabled}
                        key={`revision:${entry.summary.to_revision}`}
                      />
                    ),
                  )}
                </ol>
              ) : (
                <div className="quiet-empty">No project revisions yet.</div>
              )}
            </section>
          </div>
        </div>
      </aside>
    </div>
  );
}

function EpisodeRevisionGroup({
  projectId,
  episodeId,
  summaries,
  episodeReportHref,
  onFork,
  forkDisabled,
}: {
  projectId: string;
  episodeId: string;
  summaries: RevisionSummary[];
  episodeReportHref: Props["episodeReportHref"];
  onFork: Props["onFork"];
  forkDisabled: Props["forkDisabled"];
}) {
  const [reportOpenError, setReportOpenError] = useState<string | null>(null);
  const newest = summaries[0];
  const episode = newest.episode;
  const report = episode?.report;
  const labelId = `history-episode-${newest.to_revision}`;
  const authorizer = newest.authorized_by?.display_name ?? "Unattributed";

  return (
    <li className="history-campaign-group">
      <Clock3 size={12} />
      <section aria-labelledby={labelId}>
        <header className="history-campaign-header">
          <div>
            <h5 id={labelId}>
              {episode
                ? `${episodeModeLabel(episode.mode)} episode · ${episodeStateLabel(episode)}`
                : "Episode no longer recorded"}
            </h5>
            <p className="history-campaign-meta">
              Authorized by {authorizer} ·{" "}
              <time dateTime={newest.created_at}>{formatTimestamp(newest.created_at)}</time> ·{" "}
              {summaries.length} {summaries.length === 1 ? "revision" : "revisions"}
            </p>
          </div>
          {report && episode && (
            <EpisodeReportLink
              className="button compact secondary"
              href={episodeReportHref(episodeId)}
              aria-label={`Open the ${report.ending} episode report`}
              projectId={projectId}
              episodeId={episodeId}
              onOpenError={setReportOpenError}
            >
              <ExternalLink size={12} /> Open report
            </EpisodeReportLink>
          )}
        </header>
        {reportOpenError && (
          <div className="history-campaign-error" role="alert">
            {reportOpenError}
          </div>
        )}
        <ol className="history-campaign-revisions">
          {summaries.map((summary) => (
            <RevisionItem
              summary={summary}
              onFork={onFork}
              forkDisabled={forkDisabled}
              key={summary.to_revision}
            />
          ))}
        </ol>
      </section>
    </li>
  );
}

function RevisionItem({
  summary,
  onFork,
  forkDisabled,
}: {
  summary: RevisionSummary;
  onFork: Props["onFork"];
  forkDisabled: Props["forkDisabled"];
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const requestId = useRef<string | null>(null);
  const fork = async () => {
    if (!onFork || busy || forkDisabled) return;
    requestId.current ??= crypto.randomUUID();
    setBusy(true);
    setFailure(null);
    try {
      await onFork(summary.to_revision, requestId.current);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li
      className={failure ? "info history-fork-failed" : "info"}
      data-revision={summary.to_revision}
    >
      <Clock3 size={12} />
      <div>
        <div className="history-revision-heading">
          <p>
            <strong>
              Revision {summary.from_revision} to revision {summary.to_revision}
            </strong>
          </p>
          {onFork &&
            (failure ? (
              <button
                className="icon-button"
                aria-label="Retry fork"
                title={failure}
                disabled={busy || forkDisabled}
                onClick={() => void fork()}
              >
                <RotateCcw size={14} />
              </button>
            ) : (
              <>
                <button
                  className="icon-button history-fork-action"
                  aria-label="Fork from here"
                  title="Fork from here"
                  disabled={busy || forkDisabled}
                  onClick={() => void fork()}
                >
                  <GitFork size={14} />
                </button>
                <details
                  className="history-revision-menu"
                  onKeyDown={(event) => {
                    if (event.key === "Escape") event.currentTarget.open = false;
                  }}
                >
                  <summary className="icon-button" aria-label="Revision actions">
                    <MoreHorizontal size={16} />
                  </summary>
                  <div>
                    <button
                      className="button compact secondary"
                      disabled={busy || forkDisabled}
                      onClick={(event) => {
                        event.currentTarget.closest("details")?.removeAttribute("open");
                        void fork();
                      }}
                    >
                      <GitFork size={14} /> Fork from here
                    </button>
                  </div>
                </details>
              </>
            ))}
        </div>
        {summary.sentences.map((sentence, index) => (
          <p key={`${summary.to_revision}:${index}`}>{sentence}</p>
        ))}
        <time dateTime={summary.created_at}>
          {revisionKindLabel(summary.kind)} · {revisionAttribution(summary)} ·{" "}
          {formatTimestamp(summary.created_at)}
        </time>
        {summary.producer === "agent" && summary.authorized_by && (
          <p className="history-attribution-detail">
            {summary.profile === "orchestrator" ? "Orchestrator" : "Ordinary"} Agent task
            {summary.task_id ? ` · ${summary.task_id}` : ""}
          </p>
        )}
      </div>
    </li>
  );
}

function groupEpisodeRevisions(summaries: RevisionSummary[]): HistoryEntry[] {
  const newestFirst = [...summaries].sort((left, right) => right.to_revision - left.to_revision);
  const byEpisode = new Map<string, RevisionSummary[]>();
  for (const summary of newestFirst) {
    if (!summary.episode_id) continue;
    const group = byEpisode.get(summary.episode_id) ?? [];
    group.push(summary);
    byEpisode.set(summary.episode_id, group);
  }

  const seenEpisodes = new Set<string>();
  const entries: HistoryEntry[] = [];
  for (const summary of newestFirst) {
    if (!summary.episode_id) {
      entries.push({ kind: "revision", summary });
      continue;
    }
    if (seenEpisodes.has(summary.episode_id)) continue;
    seenEpisodes.add(summary.episode_id);
    entries.push({
      kind: "episode",
      episodeId: summary.episode_id,
      summaries: byEpisode.get(summary.episode_id) ?? [summary],
    });
  }
  return entries;
}

function episodeModeLabel(mode: NonNullable<RevisionSummary["episode"]>["mode"]): string {
  return mode === "auto_research" ? "Auto-research" : "Experiment";
}

function episodeStateLabel(episode: NonNullable<RevisionSummary["episode"]>): string {
  return episode.state_label;
}

function revisionAttribution(summary: RevisionSummary): string {
  if (summary.producer === "system") return "RCP";
  if (summary.authorized_by) return summary.authorized_by.display_name;
  const role = summary.author ? capitalize(summary.author) : capitalize(summary.producer);
  return `${role} · Unattributed`;
}

function revisionKindLabel(kind: RevisionSummary["kind"]): string {
  if (kind === "experiment_loop") return "Experiment loop";
  return capitalize(kind);
}

function capitalize(value: string): string {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}
