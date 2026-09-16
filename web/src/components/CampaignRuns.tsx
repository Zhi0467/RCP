import { graphViewHash } from "../graphTarget";
import { taskRetryLabel } from "../agentTasks";
import {
  ChevronDown,
  CirclePause,
  ExternalLink,
  LoaderCircle,
  Network,
  Play,
  RotateCcw,
  Send,
  Square,
  Telescope,
} from "lucide-react";
import { useEffect, useId, useMemo, useState, type Ref } from "react";
import {
  episodeEndingLabel,
  episodeProjection,
  episodeReportPreviewUrl,
  episodeTaskRows,
  formatTokenCount,
} from "../campaigns";
import { MarkdownAnswer } from "../chatMarkdown";
import { fetchEpisodeTimeline } from "../api";
import { EpisodeTimeline } from "./EpisodeTimeline";
import type {
  AgentTask,
  Episode,
  EpisodeTimelineResponse,
  ExperimentLoopIndexEntry,
} from "../types";
import { EpisodeReportLink } from "./EpisodeReportLink";
import {
  EpisodeArchiveButton,
  EpisodeAuthor,
  type ArchiveEpisodeAction,
} from "./EpisodeRunControls";

export function AutoResearchEpisodeCard({
  episode,
  initiallyExpanded,
  selected = false,
  detailRef,
  busyAction,
  taskActionId,
  childExperiments = [],
  onOpenExperimentEntry,
  onInspectTask,
  onStop,
  onMerge,
  onContinue,
  onSendMessage,
  onOperateTask,
  onSwitchProvider,
  onArchive,
}: {
  episode: Episode;
  initiallyExpanded: boolean;
  selected?: boolean;
  detailRef?: Ref<HTMLDivElement>;
  busyAction: string | null;
  taskActionId: string | null;
  childExperiments?: ExperimentLoopIndexEntry[];
  onOpenExperimentEntry: (entry: ExperimentLoopIndexEntry) => void;
  onInspectTask: (operationId: string) => void;
  onStop: (episodeId: string) => Promise<void>;
  onMerge: (episodeId: string) => Promise<void>;
  onContinue: (episodeId: string, invocationCeiling: number) => Promise<void>;
  onSendMessage: (episodeId: string, body: string) => Promise<void>;
  onOperateTask: (task: AgentTask, action: "pause" | "resume" | "retry") => Promise<void>;
  onSwitchProvider: (task: AgentTask) => void;
  onArchive: ArchiveEpisodeAction;
}) {
  const detailId = useId();
  const chain = episode.chain;
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const [additionalTurns, setAdditionalTurns] = useState("");
  const [message, setMessage] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const mergeSnapshot = JSON.stringify(episode.graph_branch);
  const [mergeError, setMergeError] = useState<{ snapshot: string; message: string } | null>(null);
  const taskRows = useMemo(() => episodeTaskRows(episode), [episode]);
  const apiBase = `/api/projects/${encodeURIComponent(episode.project_id)}`;
  const [timeline, setTimeline] = useState<EpisodeTimelineResponse | null>(null);
  const [timelineRefresh, setTimelineRefresh] = useState(0);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  // Turn progress updates task rows without touching the episode's own timestamp.
  const taskSignature = episode.tasks
    .map((task) => `${task.operation_id}:${task.status}:${task.updated_at}`)
    .join("|");
  const projection = useMemo(
    () =>
      episodeProjection(
        episode,
        taskRows.map(({ task }) => task),
      ),
    [episode, taskRows],
  );
  const { recommendation, taskControl } = projection;
  const parsedAdditionalTurns = Number(additionalTurns);
  const continuationIsValid =
    additionalTurns.trim().length > 0 &&
    Number.isSafeInteger(parsedAdditionalTurns) &&
    parsedAdditionalTurns >= 1;
  const stopBusy = busyAction === `stop:${episode.episode_id}`;
  const mergeBusy = busyAction === `merge:${episode.episode_id}`;
  const continueBusy = busyAction === `continue:${episode.episode_id}`;
  const messageBusy = busyAction === `message:${episode.episode_id}`;
  const controlTaskBusy = taskControl !== null && taskActionId === taskControl.task.operation_id;
  const anotherActionBusy = busyAction !== null || taskActionId !== null;
  const episodeTimestamp = formatTimestamp(episode.created_at);
  const showStop = episode.can_stop || stopBusy;

  useEffect(() => {
    if (!expanded) return;
    let cancelled = false;
    void fetchEpisodeTimeline(apiBase, episode.episode_id).then(
      (response) => {
        if (!cancelled) {
          setTimeline(response);
          setTimelineError(null);
        }
      },
      (error) => {
        if (!cancelled) setTimelineError(error instanceof Error ? error.message : String(error));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [apiBase, episode.episode_id, episode.updated_at, expanded, taskSignature, timelineRefresh]);

  useEffect(() => {
    if (initiallyExpanded) setExpanded(true);
  }, [initiallyExpanded]);

  useEffect(() => {
    if (episode.can_continue) setExpanded(true);
  }, [episode.can_continue]);

  useEffect(() => {
    setMergeError(null);
  }, [mergeSnapshot]);

  const submitContinuation = async () => {
    if (!continuationIsValid || anotherActionBusy) return;
    setLocalError(null);
    try {
      await onContinue(episode.episode_id, parsedAdditionalTurns);
      setAdditionalTurns("");
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    }
  };

  const submitMessage = async () => {
    const body = message.trim();
    if (!body || anotherActionBusy) return;
    setLocalError(null);
    try {
      await onSendMessage(episode.episode_id, body);
      setTimelineRefresh((value) => value + 1);
      setMessage("");
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    }
  };

  const operateControlTask = async () => {
    if (anotherActionBusy || !taskControl) return;
    setLocalError(null);
    try {
      await onOperateTask(taskControl.task, taskControl.kind);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    }
  };

  const mergeToMain = async () => {
    if (!episode.graph_branch || anotherActionBusy) return;
    setMergeError(null);
    try {
      await onMerge(episode.episode_id);
    } catch (error) {
      setMergeError({
        snapshot: mergeSnapshot,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  };

  return (
    <article
      className={`campaign-run ${projection.health}`}
      data-episode-id={episode.episode_id}
      data-selected={selected || undefined}
    >
      <span className="campaign-state-rail" aria-hidden="true" />
      <div className="campaign-run-heading">
        <button
          className="campaign-run-toggle"
          type="button"
          aria-label={`${expanded ? "Collapse" : "Expand"} auto-research episode started ${episodeTimestamp}`}
          aria-expanded={expanded}
          aria-controls={detailId}
          onClick={() => setExpanded((current) => !current)}
        />
        <span className="campaign-run-identity">
          <strong className="campaign-run-title">
            <Telescope size={14} aria-hidden="true" />
            <span>Auto-research</span>
          </strong>
          <span className="campaign-run-meta">
            <span className={`status-pill ${projection.health}`}>{projection.healthLabel}</span>
            <time dateTime={episode.created_at}>{episodeTimestamp}</time>
            <EpisodeAuthor author={episode.authorized_by} />
            {chain.length > 1 && (
              <span
                className="campaign-run-chain"
                title={chain.map((member) => compactIdentity(member.episode_id)).join(" → ")}
              >
                Continued {chain.length - 1} {chain.length === 2 ? "time" : "times"}
              </span>
            )}
          </span>
        </span>
        <EpisodeBudgetMeter episode={episode} />
        <span className="campaign-run-time">
          <EpisodeArchiveButton
            episode={episode}
            disabled={anotherActionBusy}
            onArchive={onArchive}
          />
          <ChevronDown size={15} aria-hidden="true" />
        </span>
      </div>
      {expanded && (
        <div className="campaign-run-detail" id={detailId} tabIndex={-1} ref={detailRef}>
          {chain.length > 1 && (
            <ol className="campaign-run-chain-members" aria-label="Continuation chain">
              {chain.map((member) => (
                <li
                  key={member.episode_id}
                  data-episode-id={member.episode_id}
                  aria-current={member.episode_id === episode.episode_id ? "true" : undefined}
                >
                  <span>
                    {member.invocations_used} of {member.invocation_ceiling} turns
                  </span>
                  <span>
                    {member.ending
                      ? episodeEndingLabel(member.ending)
                      : member.episode_id === episode.episode_id
                        ? "Current"
                        : "Unsettled"}
                  </span>
                  {member.report && member.episode_id !== episode.episode_id && (
                    <EpisodeReportLink
                      className="campaign-run-chain-report"
                      aria-label={`Open ${episodeEndingLabel(member.report.ending)} report from ${formatTimestamp(member.report.created_at, true)}`}
                      projectId={episode.project_id}
                      episodeId={member.episode_id}
                      href={episodeReportPreviewUrl(episode.project_id, member.episode_id)}
                      onOpenError={setLocalError}
                    >
                      Report
                    </EpisodeReportLink>
                  )}
                </li>
              ))}
            </ol>
          )}
          <div className="campaign-run-actions">
            <div
              className={`campaign-run-health ${projection.health}`}
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              <strong>{projection.healthLabel}</strong>
            </div>
            {episode.graph_branch && (
              <a
                className="button primary compact"
                href={graphViewHash(episode.project_id, {
                  kind: "branch",
                  branch_id: episode.graph_branch.branch_id,
                })}
              >
                <Network size={13} /> Open graph
              </a>
            )}
            {episode.report && episode.wrapup_state === "ready" && (
              <div className="campaign-report-actions">
                <EpisodeReportLink
                  className={`button compact ${recommendation.kind === "open_report" ? "primary" : "secondary"}`}
                  href={episodeReportPreviewUrl(episode.project_id, episode.episode_id)}
                  aria-label={`Open ${episodeEndingLabel(episode.report.ending)} report from ${formatTimestamp(episode.report.created_at, true)}`}
                  projectId={episode.project_id}
                  episodeId={episode.episode_id}
                  onOpenError={setLocalError}
                >
                  <ExternalLink size={12} /> Open report
                </EpisodeReportLink>
              </div>
            )}
            {taskControl && (
              <button
                className={`button compact ${
                  taskControl.kind === "resume" ? "primary" : "secondary"
                }`}
                type="button"
                disabled={anotherActionBusy}
                onClick={() => void operateControlTask()}
              >
                {controlTaskBusy ? (
                  <LoaderCircle className="spin" size={12} />
                ) : taskControl.kind === "pause" ? (
                  <CirclePause size={12} />
                ) : taskControl.kind === "resume" ? (
                  <Play size={12} />
                ) : (
                  <RotateCcw size={12} />
                )}
                {controlTaskBusy
                  ? `${episodeActionLabel(taskControl.kind, taskControl.task)}…`
                  : episodeActionLabel(taskControl.kind, taskControl.task)}
              </button>
            )}
            {taskControl?.kind === "retry" && taskControl.canSwitchProvider && (
              <button
                className="button compact"
                type="button"
                disabled={anotherActionBusy || controlTaskBusy}
                onClick={() => onSwitchProvider(taskControl.task)}
              >
                Switch provider…
              </button>
            )}
            {episode.can_continue && (
              <form
                className="campaign-reauthorize"
                onSubmit={(event) => {
                  event.preventDefault();
                  void submitContinuation();
                }}
              >
                <input
                  type="number"
                  min={1}
                  step={1}
                  inputMode="numeric"
                  aria-label="Turns to add"
                  value={additionalTurns}
                  disabled={anotherActionBusy}
                  onChange={(event) => setAdditionalTurns(event.target.value)}
                />
                <button
                  className="button primary compact"
                  type="submit"
                  disabled={!continuationIsValid || anotherActionBusy}
                >
                  {continueBusy && <LoaderCircle className="spin" size={12} />}
                  {continueBusy
                    ? "Adding turns…"
                    : continuationIsValid
                      ? `Add ${parsedAdditionalTurns} turns`
                      : "Add turns"}
                </button>
              </form>
            )}
            {showStop && (
              <button
                className="button secondary compact campaign-stop"
                type="button"
                disabled={anotherActionBusy}
                onClick={() => {
                  setLocalError(null);
                  void onStop(episode.episode_id).catch((error) => {
                    setLocalError(error instanceof Error ? error.message : String(error));
                  });
                }}
              >
                {stopBusy ? <LoaderCircle className="spin" size={12} /> : <Square size={11} />}
                {stopBusy ? "Stopping…" : "Stop"}
              </button>
            )}
          </div>

          <div className={`campaign-run-recommendation ${recommendation.kind}`}>
            <span className="eyebrow">Recommended next step</span>
            <strong>{recommendation.label}</strong>
          </div>

          {episode.graph_branch && (
            <section
              className={`campaign-graph-branch ${episode.graph_branch.merge_state}`}
              aria-label="Episode graph branch"
            >
              <header>
                <span className="campaign-branch-identity">
                  <span className="eyebrow">Graph branch</span>
                  <strong title={episode.graph_branch.branch_id}>
                    {compactIdentity(episode.graph_branch.branch_id)}
                  </strong>
                </span>
                <span className={`status-pill branch-${episode.graph_branch.merge_state}`}>
                  {branchMergeStateLabel(episode.graph_branch.merge_state)}
                </span>
              </header>
              <div className="campaign-branch-heads">
                <GraphHeadFact label="Base on main" head={episode.graph_branch.base_head} />
                <span className="campaign-branch-head-path" aria-hidden="true" />
                <GraphHeadFact label="Branch head" head={episode.graph_branch.head} />
                {episode.graph_branch.latest_successful_merge && (
                  <GraphHeadFact
                    label={
                      episode.graph_branch.latest_successful_merge.outcome === "committed"
                        ? "Merged on main"
                        : "Main unchanged"
                    }
                    head={episode.graph_branch.latest_successful_merge.result_main_head}
                  />
                )}
              </div>
              {(episode.graph_branch.merge_state === "needs_action" ||
                episode.graph_branch.merge_state === "failed") &&
                episode.graph_branch.merge_diagnostic && (
                  <div
                    className={`campaign-branch-diagnostic ${episode.graph_branch.merge_state}`}
                    role={episode.graph_branch.merge_state === "failed" ? "alert" : "status"}
                  >
                    {episode.graph_branch.merge_diagnostic}
                  </div>
                )}
              <button
                className="button primary compact campaign-branch-merge"
                type="button"
                disabled={anotherActionBusy}
                onClick={() => void mergeToMain()}
              >
                {mergeBusy ? <LoaderCircle className="spin" size={12} /> : <Network size={12} />}
                {mergeBusy ? "Starting merge…" : "Merge to main"}
              </button>
              {mergeError?.snapshot === mergeSnapshot && (
                <div className="campaign-branch-diagnostic" role="alert">
                  {mergeError.message}
                </div>
              )}
            </section>
          )}

          {episode.starting_instruction && (
            <div className="campaign-starting-instruction">
              <span className="field-label">Starting instruction</span>
              <MarkdownAnswer text={episode.starting_instruction} />
            </div>
          )}

          {timelineError && (
            <div className="campaign-run-error" role="alert">
              {timelineError}
            </div>
          )}
          {timeline?.episode_id === episode.episode_id && (
            <EpisodeTimeline
              events={timeline.events}
              apiBase={apiBase}
              episodeId={episode.episode_id}
              graphTarget={episode.graph_target}
              truncated={timeline.truncated}
              onInspectTask={onInspectTask}
              childExperiments={childExperiments}
              onOpenExperimentEntry={onOpenExperimentEntry}
            />
          )}
          {episode.can_message && (
            <form
              className="campaign-message-composer"
              onSubmit={(event) => {
                event.preventDefault();
                void submitMessage();
              }}
            >
              <textarea
                rows={2}
                aria-label="Message orchestrator"
                value={message}
                disabled={anotherActionBusy}
                onChange={(event) => setMessage(event.target.value)}
              />
              <button
                className="button primary compact"
                type="submit"
                disabled={!message.trim() || anotherActionBusy}
              >
                {messageBusy ? <LoaderCircle className="spin" size={12} /> : <Send size={12} />}
                Send
              </button>
            </form>
          )}
          {/* A stopped episode reaches here with no report, no report error, and no
              ending diagnostic, because the projection withholds all three. There is
              nothing left for this view to suppress. */}
          {(localError ||
            (episode.wrapup_state === "failed" ? episode.wrapup_error : null) ||
            episode.ending_diagnostic) && (
            <div className="campaign-run-error" role="alert">
              {localError ||
                (episode.wrapup_state === "failed"
                  ? `Report generation error: ${episode.wrapup_error || "The report could not be generated."}`
                  : episode.ending_diagnostic)}
            </div>
          )}
        </div>
      )}
    </article>
  );
}

function GraphHeadFact({ label, head }: { label: string; head: Episode["graph_base_head"] }) {
  if (!head) return null;
  return (
    <span className="campaign-branch-head">
      <span>{label}</span>
      <strong>r{head.revision}</strong>
      <code title={head.transition_id ?? undefined}>
        {head.transition_id ? compactIdentity(head.transition_id) : "origin"}
      </code>
    </span>
  );
}

export function branchMergeStateLabel(
  state: NonNullable<Episode["graph_branch"]>["merge_state"],
): string {
  switch (state) {
    case "unmerged":
      return "Unmerged";
    case "running":
      return "Merge running";
    case "merged":
      return "Merged";
    case "needs_action":
      return "Merge needs action";
    case "failed":
      return "Merge failed";
  }
}

function compactIdentity(value: string): string {
  return value.length <= 18 ? value : `${value.slice(0, 8)}\u2026${value.slice(-6)}`;
}

export function EpisodeBudgetMeter({ episode }: { episode: Episode }) {
  const budget = episode.budget;
  const usedPercent = Math.min(100, (budget.invocations_used / budget.invocation_ceiling) * 100);
  const label = `${budget.invocations_used} of ${budget.invocation_ceiling} operational invocations used; ${budget.observed_input_tokens} observed input tokens and ${budget.observed_generated_tokens} observed generated tokens`;
  return (
    <span
      className="campaign-budget-meter"
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={budget.invocation_ceiling}
      aria-valuenow={budget.invocations_used}
    >
      <span className="campaign-budget-copy">
        <strong>
          {budget.invocations_used} / {budget.invocation_ceiling} invocations
        </strong>
        <span>
          {formatTokenCount(budget.observed_input_tokens)} input ·{" "}
          {formatTokenCount(budget.observed_generated_tokens)} generated
        </span>
      </span>
      <span className="campaign-budget-track" aria-hidden="true">
        <span className="campaign-budget-spent" style={{ width: `${usedPercent}%` }} />
      </span>
    </span>
  );
}

function formatTimestamp(value: string, includeSeconds = false): string {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    ...(includeSeconds ? { second: "2-digit" } : {}),
  }).format(parsed);
}

function episodeActionLabel(action: "pause" | "resume" | "retry", task: AgentTask): string {
  if (task.can_collect && action !== "pause") return taskRetryLabel(task);
  if (action === "pause") return "Pause";
  if (action === "resume") return "Resume";
  return "Retry";
}
