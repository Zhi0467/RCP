import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { File, History, Inbox, LoaderCircle, Play, RotateCcw, TriangleAlert } from "lucide-react";
import { localClock, timeDividerLabel } from "../agents/agentThreadModel";
import {
  isActiveTask,
  isNewestOfTurnClass,
  pausedTaskText,
  taskKindLabel,
  type TaskTranscriptLine,
} from "../agents/agentTasks";
import type {
  AgentArtifactDescriptor,
  AgentQuestion,
  AgentTask,
  ArtifactEditOutcome,
  ConversationMode,
  GraphNode,
  GraphUpdateRecovery,
  GraphUpdateResult,
  ProposalCardView,
  TurnOutcome,
} from "../core/types";
import type { GlossaryIndex } from "../graph/glossary";
import { CHAT_USER_MESSAGE_COLLAPSE_THRESHOLD, THREAD_TIME_GAP_MINUTES } from "../core/uiConstants";
import { MarkdownAnswer } from "../core/chatMarkdown";
import { ReferenceChip } from "../core/ReferenceChip";
import { sourceReference } from "../core/projectReferences";
import { BrowserTurnNotice } from "../core/BrowserControls";
import { ArtifactCommentQuotes, artifactCommentLead } from "../chat/ArtifactCommentQuotes";
import { AnswerCommentButton, type AnswerComments } from "../chat/answerComments";
import { formatBytes } from "../chat/composerAttachments";
import { CompactionDivider } from "../chat/AgentStatusStrip";
import { compactionAnchors } from "../chat/agentStatusModel";
import { questionTranscript } from "../chat/questions";
import { turnOutcomeChip } from "../chat/turnOutcome";
import type { AgentWindowEndpoints } from "./agentWindowEndpoints";
import type { useAgentWindowArtifacts } from "./useAgentWindowArtifacts";
import type { PendingAgentWindowTurn } from "./useAgentWindowLines";
import { AgentWindowResend } from "./AgentWindowResend";

interface Props {
  transcript: TaskTranscriptLine[];
  tasks: AgentTask[];
  questions: AgentQuestion[];
  endpoints: AgentWindowEndpoints;
  artifacts: ReturnType<typeof useAgentWindowArtifacts>;
  comments: AnswerComments;
  compactionByLine: ReturnType<typeof compactionAnchors>;
  continuedTaskIds: Set<string>;
  latestGraphReceiptLineIds: Set<string>;
  renderQuestion?: (question: AgentQuestion) => ReactNode;
  /** Cards that the owner derives itself; placed by their raising turn. */
  proposalCards?: ProposalCardView[];
  /** Shows each position's cards as one unit; without it each asked card uses `renderQuestion`. */
  renderCards?: (entry: AgentWindowCardsEntry) => ReactNode;
  lineLead?: (line: TaskTranscriptLine, task: AgentTask | undefined) => ReactNode;
  lineClassName?: (line: TaskTranscriptLine) => string;
  humanLead?: (line: TaskTranscriptLine) => ReactNode;
  humanBody?: (line: TaskTranscriptLine) => ReactNode | undefined;
  /** An agent-side line its owner renders itself, such as a structured wake report. */
  agentBody?: (line: TaskTranscriptLine) => ReactNode | undefined;
  /** What started a wake; a board chat names only watchers. */
  triggerChip?: (line: TaskTranscriptLine) => ReactNode;
  /** A window that shows browser loss once elsewhere keeps it off each turn. */
  browserNotices?: boolean;
  agentProgress?: (line: TaskTranscriptLine, task: AgentTask | undefined) => ReactNode;
  footer?: ReactNode;
  showModes?: boolean;
  nodes?: Readonly<Record<string, GraphNode>>;
  glossaryIndex?: GlossaryIndex;
  onOpenNode?: (nodeId: string) => void;
  pendingTurn?: PendingAgentWindowTurn | null;
  onResend?: () => void;
  submitting?: boolean;
  readOnly?: boolean;
  graphChangesDisabled?: boolean;
  reviewPending?: boolean;
  repairingTaskId?: string | null;
  repairErrors?: Map<string, string>;
  onResumeTask?: (task: AgentTask) => void;
  onRetryTask?: (task: AgentTask) => void;
  onInspectTask?: (taskId: string) => void;
  onOpenInbox?: () => void;
  onRepairGraphUpdate?: (taskId: string, action?: GraphUpdateRecovery) => Promise<void>;
}
const NO_NODES: Readonly<Record<string, GraphNode>> = {};
const NO_ERRORS = new Map<string, string>();
const NO_PROPOSAL_CARDS: ProposalCardView[] = [];

/** Shared turn bodies; owners supply their questions, progress, and controls. */
export function AgentWindowTranscript({
  transcript,
  tasks: relatedTasks,
  questions,
  endpoints,
  artifacts,
  comments,
  compactionByLine,
  continuedTaskIds,
  latestGraphReceiptLineIds,
  renderQuestion,
  proposalCards = NO_PROPOSAL_CARDS,
  renderCards,
  lineLead,
  lineClassName,
  humanLead,
  humanBody,
  agentBody,
  triggerChip = watcherChip,
  browserNotices = true,
  agentProgress,
  footer,
  showModes = true,
  nodes = NO_NODES,
  glossaryIndex,
  onOpenNode,
  pendingTurn = null,
  onResend,
  submitting = false,
  readOnly = false,
  graphChangesDisabled = false,
  reviewPending = false,
  repairingTaskId = null,
  repairErrors = NO_ERRORS,
  onResumeTask,
  onRetryTask,
  onInspectTask,
  onOpenInbox,
  onRepairGraphUpdate,
}: Props) {
  const [expandedHumanMessageIds, setExpandedHumanMessageIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [expiryClock, setExpiryClock] = useState(() => Date.now());
  const [timeShownIds, setTimeShownIds] = useState<Set<string>>(() => new Set());
  const relatedActive = relatedTasks.some(isActiveTask);
  const taskById = useMemo(
    () => new Map(relatedTasks.map((task) => [task.operation_id, task])),
    [relatedTasks],
  );
  const toggleHumanMessage = (messageId: string) =>
    setExpandedHumanMessageIds((current) => {
      const next = new Set(current);
      if (next.has(messageId)) next.delete(messageId);
      else next.add(messageId);
      return next;
    });
  useEffect(() => {
    if (!transcript.some((line) => line.attachments?.length)) return;
    const timer = window.setInterval(() => setExpiryClock(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, [transcript]);
  const entries = agentWindowEntries(transcript, questions, proposalCards);
  const dividers = timeDividers(entries);
  const now = new Date();
  const toggleTime = (lineId: string, target: EventTarget) => {
    if (!(target instanceof Element) || !lineTapShowsTime(target, window.getSelection())) return;
    setTimeShownIds((current) => {
      const next = new Set(current);
      if (next.has(lineId)) next.delete(lineId);
      else next.add(lineId);
      return next;
    });
  };
  return (
    <>
      {entries.map((entry) => {
        if (entry.kind === "cards")
          return renderCards ? (
            <Fragment key={entry.key}>{renderCards(entry)}</Fragment>
          ) : (
            entry.asked.map((question) => (
              <Fragment key={question.question_id}>{renderQuestion?.(question)}</Fragment>
            ))
          );
        if (entry.kind === "question")
          return (
            <Fragment key={entry.question.question_id}>{renderQuestion?.(entry.question)}</Fragment>
          );
        const line = entry.line;
        const messageId = line.lineId;
        const task = taskById.get(line.taskId);
        const activeLineTask = task && !line.steering && isActiveTask(task) ? task : null;
        const pausedLineTask =
          !line.steering &&
          task?.paused &&
          task.can_resume &&
          !continuedTaskIds.has(task.operation_id)
            ? task
            : null;
        // Only the newest turn's error row carries Retry; the header shows no second one.
        const retryLineTask =
          line.role === "error" &&
          task?.can_retry &&
          !task.can_resume &&
          isNewestOfTurnClass(task, relatedTasks)
            ? task
            : null;
        const pendingLine = pendingTurn?.clientId === line.taskId;
        const failedTurn = pendingLine && pendingTurn?.error ? pendingTurn : null;
        const collapsible =
          line.role === "human" && line.text.length > CHAT_USER_MESSAGE_COLLAPSE_THRESHOLD;
        const expanded = expandedHumanMessageIds.has(messageId);
        const artifactComments = line.artifactComments;
        const commentLead = artifactComments
          ? artifactCommentLead(line.text, artifactComments)
          : null;
        const compaction = compactionByLine.get(line.lineId);
        const ownBody = line.role === "agent" ? agentBody?.(line) : undefined;
        const divider = dividers.get(line.lineId);
        const shownAt = lineTime(line);
        return (
          <Fragment key={line.lineId}>
            {compaction?.placement === "before" && (
              <CompactionDivider compactions={compaction.compactions} />
            )}
            {divider && (
              <div className="chat-time-divider">
                <time dateTime={divider.toISOString()}>{timeDividerLabel(divider, now)}</time>
              </div>
            )}
            <div
              className={`node-chat-line ${lineClassName?.(line) ?? line.role}${failedTurn ? " unsent" : ""}`}
              data-source={line.source ?? undefined}
              data-task-id={line.taskId}
              data-time-shown={timeShownIds.has(line.lineId) ? "" : undefined}
              onClick={shownAt ? (event) => toggleTime(line.lineId, event.target) : undefined}
            >
              {lineLead?.(line, task)}
              {browserNotices && (
                <BrowserTurnNotice status={line.browserStatus} turnId={line.taskId} />
              )}
              {showModes && line.role === "human" && line.mode && (
                <span className={`chat-turn-mode ${line.mode}`}>{modeLabel(line.mode)}</span>
              )}
              {failedTurn && (
                <AgentWindowResend
                  error={failedTurn.error!}
                  disabled={submitting}
                  onResend={() => onResend?.()}
                />
              )}
              {triggerChip(line)}
              {ownBody !== undefined ? (
                ownBody
              ) : line.role === "agent" ? (
                line.running ? (
                  agentProgress ? (
                    agentProgress(line, activeLineTask ?? undefined)
                  ) : (
                    activeLineTask && <InlineTaskProgress task={activeLineTask} />
                  )
                ) : (
                  line.text && (
                    <>
                      {agentProgress?.(line, task)}
                      <div className="chat-markdown chat-annotatable-answer">
                        <MarkdownAnswer
                          text={line.text}
                          nodes={nodes}
                          glossaryIndex={glossaryIndex}
                          onOpenNode={onOpenNode}
                          onOpenRepositoryFileLink={(href) =>
                            void artifacts.openRepositoryFile(messageId, line.taskId, href)
                          }
                          renderEmbed={(src, alt) =>
                            artifacts.renderInlineArtifact(line.taskId, line.artifacts, src, alt)
                          }
                        />
                      </div>
                      {/* Outside the annotatable wrapper: a selection clamp or the
                        keyboard flow must never stage this diagnostic as answer text. */}
                      {artifacts.repositoryFileErrors.get(messageId) && (
                        <strong className="chat-repository-file-error" role="alert">
                          {artifacts.repositoryFileErrors.get(messageId)}
                        </strong>
                      )}
                      {!readOnly && <AnswerCommentButton comments={comments} />}
                    </>
                  )
                )
              ) : line.role === "human" ? (
                (humanBody?.(line) ?? (
                  <>
                    <div
                      className={`chat-human-message${collapsible && !expanded ? " collapsed" : ""}`}
                    >
                      {humanLead?.(line)}
                      {artifactComments && commentLead !== null ? (
                        <ArtifactCommentQuotes
                          lead={commentLead}
                          comments={artifactComments}
                          onOpen={() => endpoints.openArtifactPanel?.(artifactComments.artifact_id)}
                        />
                      ) : (
                        <span className="node-chat-text">{line.text}</span>
                      )}
                    </div>
                    {collapsible && (
                      <button
                        type="button"
                        className="chat-message-toggle"
                        aria-expanded={expanded}
                        onClick={() => toggleHumanMessage(messageId)}
                      >
                        {expanded ? "See less" : "See more"}
                      </button>
                    )}
                    {line.steering && (
                      <div className="chat-steering-receipt" role="status">
                        <strong>{line.steering.label}</strong>
                        {line.steering.reason && <span>{line.steering.reason}</span>}
                      </div>
                    )}
                    {line.attachments?.some((attachment) => attachment.reference) && (
                      <div className="chat-input-references">
                        {line.attachments.map((attachment) =>
                          attachment.reference ? (
                            <ReferenceChip
                              key={attachment.attachment_id}
                              projectId={endpoints.projectId!}
                              reference={{
                                ...sourceReference(attachment.reference),
                                label: attachment.name,
                              }}
                              version={
                                attachment.reference.graph_head
                                  ? `r${attachment.reference.graph_head.revision}`
                                  : attachment.reference.version?.slice(0, 7)
                              }
                            />
                          ) : null,
                        )}
                      </div>
                    )}
                    {line.attachments?.map((attachment) => {
                      if (attachment.reference) return null;
                      const expired = Date.parse(attachment.expires_at) <= expiryClock;
                      return (
                        <div
                          className={`chat-input-attachment${expired ? " expired" : ""}`}
                          key={attachment.attachment_id}
                        >
                          <File size={14} />
                          <span>
                            <strong>{attachment.name}</strong>
                            <small>
                              {attachment.media_type} · {formatBytes(attachment.size)}
                            </small>
                          </span>
                          {expired && <em>Expired</em>}
                        </div>
                      );
                    })}
                    {agentProgress ? (
                      agentProgress(line, task)
                    ) : pausedLineTask ? (
                      <InlinePausedTask
                        task={pausedLineTask}
                        disabled={readOnly}
                        onResume={() => onResumeTask?.(pausedLineTask)}
                        onRetry={() => onRetryTask?.(pausedLineTask)}
                      />
                    ) : activeLineTask ? (
                      <InlineTaskProgress task={activeLineTask} />
                    ) : failedTurn ? null : pendingLine ? (
                      <InlineTaskProgress task={null} />
                    ) : null}
                  </>
                ))
              ) : pausedLineTask ? null : (
                <span className="node-chat-text">{line.text}</span>
              )}
              {retryLineTask && onRetryTask && (
                <button
                  type="button"
                  className="icon-button chat-error-retry"
                  aria-label="Retry"
                  title="Retry"
                  disabled={readOnly}
                  onClick={() => onRetryTask?.(retryLineTask)}
                >
                  <RotateCcw size={14} />
                </button>
              )}
              {artifacts.renderArtifacts(line)}
              {line.artifactOmissions && (
                <p className="chat-artifact-omissions" role="status">
                  {line.artifactOmissions.discovery_failed
                    ? "Artifact discovery failed"
                    : `Artifacts omitted: ${Object.entries(line.artifactOmissions)
                        .filter(([, count]) => typeof count === "number" && count > 0)
                        .map(([reason, count]) => `${reason.replaceAll("_", " ")}: ${count}`)
                        .join("; ")}`}
                </p>
              )}
              {/* A published version shows itself; every outcome still tells new from updated. */}
              {line.artifactEditOutcomes
                ?.filter((outcome) => outcome.status !== "published")
                .map((outcome) => (
                  <p key={outcome.artifact_id} className="chat-artifact-omissions" role="status">
                    {artifactEditOutcomeText(
                      outcome,
                      line.artifactEditOutcomes ?? [],
                      line.artifacts ?? [],
                    )}
                  </p>
                ))}
              {line.role === "agent" && (line.graphUpdate || line.turnOutcome) && (
                <div className="chat-turn-receipts">
                  {line.turnOutcome && <TurnOutcomeChip outcome={line.turnOutcome} />}
                  {line.graphUpdate && (
                    <GraphUpdateReceipt
                      update={line.graphUpdate}
                      taskId={line.taskId}
                      repairBusy={repairingTaskId === line.taskId}
                      repairDisabled={
                        !onRepairGraphUpdate ||
                        readOnly ||
                        graphChangesDisabled ||
                        relatedActive ||
                        submitting ||
                        reviewPending
                      }
                      repairContinued={continuedTaskIds.has(line.taskId)}
                      latestReceipt={latestGraphReceiptLineIds.has(line.lineId)}
                      canApplyAgain={taskById.get(line.taskId)?.can_apply_again === true}
                      repairError={repairErrors.get(line.taskId) ?? null}
                      onInspectTask={onInspectTask}
                      onOpenInbox={onOpenInbox}
                      onRepair={
                        onRepairGraphUpdate
                          ? () => void onRepairGraphUpdate(line.taskId)
                          : undefined
                      }
                      onApplyAgain={
                        onRepairGraphUpdate
                          ? () => void onRepairGraphUpdate(line.taskId, "apply_again")
                          : undefined
                      }
                    />
                  )}
                </div>
              )}
              {shownAt && line.trigger !== "schedule" && (
                <time className="chat-line-time" dateTime={shownAt.toISOString()}>
                  {localClock(shownAt)}
                </time>
              )}
            </div>
            {compaction?.placement === "after" && (
              <CompactionDivider compactions={compaction.compactions} />
            )}
          </Fragment>
        );
      })}
      {footer}
    </>
  );
}

function watcherChip(line: TaskTranscriptLine): ReactNode {
  return line.trigger === "watcher" ? (
    <span className="chat-turn-trigger watcher">Watcher</span>
  ) : null;
}

/** When a line happened; a line with no shown time of its own uses its timestamp. */
function lineTime(line: TaskTranscriptLine): Date | null {
  const value = line.shownAt === undefined ? line.timestamp : line.shownAt;
  const at = value ? new Date(value) : null;
  return at && Number.isFinite(at.getTime()) ? at : null;
}

/** The lines that open a new day or follow a quiet gap, with the time each divider shows.
 *  Cards, receipt-only lines and lines with no time are not counted. */
export function timeDividers(entries: ReturnType<typeof agentWindowEntries>): Map<string, Date> {
  const dividers = new Map<string, Date>();
  let previous: Date | null = null;
  for (const entry of entries) {
    if (entry.kind !== "line") continue;
    const line = entry.line;
    if (line.role === "agent" && !line.text && line.graphUpdate) continue;
    const at = lineTime(line);
    if (!at) continue;
    if (
      !previous ||
      at.toDateString() !== previous.toDateString() ||
      at.getTime() - previous.getTime() >= THREAD_TIME_GAP_MINUTES * 60_000
    )
      dividers.set(line.lineId, at);
    previous = at;
  }
  return dividers;
}

const TAP_IGNORED =
  'a, button, input, textarea, select, summary, label, [role="button"], [class*="chip"], .chat-input-reference';

/** A tap on a line shows its time, unless it lands on a link, control or chip, or selects text. */
export function lineTapShowsTime(
  target: Pick<Element, "closest">,
  selection: Pick<Selection, "isCollapsed" | "toString"> | null,
): boolean {
  if (target.closest(TAP_IGNORED)) return false;
  return !selection || selection.isCollapsed || !selection.toString();
}

/** The cards that share one transcript position; an owner may show them as one stack. */
export type AgentWindowCardsEntry = {
  kind: "cards";
  key: string;
  asked: AgentQuestion[];
  proposals: ProposalCardView[];
};

type PlacedCard = {
  kind: "card";
  turnId: string;
  at: string;
  card: { asked: AgentQuestion } | { proposal: ProposalCardView };
};

/** Cards remain inside their owning turn, even when settlement stamps both messages together. */
export function agentWindowEntries(
  lines: TaskTranscriptLine[],
  questions: AgentQuestion[],
  proposalCards: ProposalCardView[] = [],
) {
  const asked = questions.filter((question) => question.card);
  const cardIds = new Set(asked.map((question) => question.question_id));
  const visible = lines.filter(
    (line) => !(line.role === "human" && line.questionId && cardIds.has(line.questionId)),
  );
  const entries: Array<
    ReturnType<typeof questionTranscript<TaskTranscriptLine>>[number] | PlacedCard
  > = questionTranscript(
    visible,
    questions.filter((question) => !question.card),
  );
  const cards: PlacedCard[] = [
    ...asked.map((question) => ({
      kind: "card" as const,
      turnId: question.operation_id,
      at: question.created_at,
      card: { asked: question },
    })),
    ...proposalCards.map((proposal) => ({
      kind: "card" as const,
      turnId: proposal.turn_id,
      at: proposal.turn_created_at,
      card: { proposal },
    })),
  ];
  for (const card of cards.sort((a, b) => a.at.localeCompare(b.at))) {
    const ownerLines = entries.flatMap((entry, index) =>
      entry.kind === "line" && entry.line.taskId === card.turnId ? [index] : [],
    );
    const answers = ownerLines.filter((index) => {
      const entry = entries[index];
      return entry.kind === "line" && entry.line.role === "agent" && entry.line.source !== "voice";
    });
    const laterAnswer = answers.find((index) => {
      const entry = entries[index];
      return entry.kind === "line" && Date.parse(entry.line.timestamp) >= Date.parse(card.at);
    });
    // Reconstructed task answers carry the launch time rather than their completion time.
    let index = laterAnswer ?? answers[0] ?? (ownerLines.length ? ownerLines.at(-1)! + 1 : -1);
    if (index < 0)
      index = entries.findIndex(
        (entry) => entry.kind === "line" && Date.parse(entry.line.timestamp) > Date.parse(card.at),
      );
    if (index < 0) index = entries.length;
    while (index < entries.length) {
      const entry = entries[index];
      if (entry.kind !== "card" || entry.turnId !== card.turnId || entry.at > card.at) break;
      index += 1;
    }
    entries.splice(index, 0, card);
  }
  const keys = new Set<string>();
  const grouped: Array<
    ReturnType<typeof questionTranscript<TaskTranscriptLine>>[number] | AgentWindowCardsEntry
  > = [];
  for (const entry of entries) {
    if (entry.kind !== "card") {
      grouped.push(entry);
      continue;
    }
    let group = grouped.at(-1);
    if (group?.kind !== "cards") {
      let key = `cards:${entry.turnId}`;
      for (let n = 2; keys.has(key); n += 1) key = `cards:${entry.turnId}:${n}`;
      keys.add(key);
      group = { kind: "cards", key, asked: [], proposals: [] };
      grouped.push(group);
    }
    if ("asked" in entry.card) group.asked.push(entry.card.asked);
    else group.proposals.push(entry.card.proposal);
  }
  return grouped;
}
function InlineTaskProgress({ task }: { task: AgentTask | null }) {
  const label = task
    ? task.status_message || `${taskKindLabel(task.kind)} is running`
    : "Starting task";
  return (
    <>
      <span className="chat-task-live" role="status" aria-live="polite" aria-atomic="true">
        {label}
      </span>
      <details className="chat-task-activity">
        <summary>
          <LoaderCircle className="spin" size={12} />
          <span>Activity</span>
        </summary>
        <div className="chat-task-inline running">
          <span>{label}</span>
        </div>
      </details>
    </>
  );
}

function InlinePausedTask({
  task,
  disabled,
  onResume,
  onRetry,
}: {
  task: AgentTask;
  disabled: boolean;
  onResume: () => void;
  onRetry: () => void;
}) {
  return (
    <div className="chat-task-inline paused" role="status" aria-label="Agent task paused">
      <span>{pausedTaskText(task)}</span>
      <button
        type="button"
        className="button compact primary"
        disabled={disabled}
        onClick={onResume}
      >
        <Play size={12} /> Resume
      </button>
      <button
        type="button"
        className="button compact secondary"
        disabled={disabled}
        onClick={onRetry}
      >
        <RotateCcw size={12} /> Retry
      </button>
    </div>
  );
}

function TurnOutcomeChip({ outcome }: { outcome: TurnOutcome }) {
  const chip = turnOutcomeChip(outcome);
  return (
    <span className="chat-turn-outcome" data-outcome={chip.status}>
      {chip.label}
      {chip.reason ? ` · ${chip.reason}` : ""}
    </span>
  );
}

function GraphUpdateReceipt({
  update,
  taskId,
  repairBusy,
  repairDisabled,
  repairContinued,
  latestReceipt,
  canApplyAgain,
  repairError,
  onInspectTask,
  onOpenInbox,
  onRepair,
  onApplyAgain,
}: {
  update: GraphUpdateResult;
  taskId: string;
  repairBusy: boolean;
  repairDisabled: boolean;
  repairContinued: boolean;
  latestReceipt: boolean;
  canApplyAgain: boolean;
  repairError: string | null;
  onInspectTask?: (taskId: string) => void;
  onOpenInbox?: () => void;
  onRepair?: () => void;
  onApplyAgain?: () => void;
}) {
  if (update.status === "none") return null;
  const proposalCount = update.proposal_ids.length;
  const applied = (
    <>
      <History size={12} />
      {update.applied_revision === null
        ? "Graph updated"
        : `Graph updated · r${update.applied_revision}`}
    </>
  );
  const proposals = (
    <>
      <Inbox size={12} />
      {proposalCount} proposal{proposalCount === 1 ? "" : "s"} sent to Inbox
    </>
  );
  return (
    <div className={`chat-graph-receipt ${update.status}`}>
      <div className="chat-graph-receipt-actions">
        {update.status === "applied" &&
          (onInspectTask ? (
            <button type="button" onClick={() => onInspectTask(taskId)}>
              {applied}
            </button>
          ) : (
            <span>{applied}</span>
          ))}
        {update.status === "rejected" && (
          <strong>
            <TriangleAlert size={12} /> Graph update rejected
          </strong>
        )}
        {update.status === "unavailable" && (
          <strong>
            <TriangleAlert size={12} />{" "}
            {update.commit_status === "present"
              ? "Graph update committed, but canonical state became unreachable"
              : update.commit_status === "unknown"
                ? "Graph update may have been committed: canonical state unreachable"
                : "Graph update not applied: canonical state unreachable"}
          </strong>
        )}
        {update.status === "applied" &&
          proposalCount > 0 &&
          (onOpenInbox ? (
            <button type="button" onClick={onOpenInbox}>
              {proposals}
            </button>
          ) : (
            <span>{proposals}</span>
          ))}
        {onRepair &&
          latestReceipt &&
          update.status === "rejected" &&
          update.repairable &&
          !repairContinued && (
            <button type="button" disabled={repairBusy || repairDisabled} onClick={onRepair}>
              <RotateCcw className={repairBusy ? "spin" : undefined} size={12} />
              Repair graph update
            </button>
          )}
        {onApplyAgain && latestReceipt && update.status === "unavailable" && canApplyAgain && (
          <button type="button" disabled={repairBusy || repairDisabled} onClick={onApplyAgain}>
            <RotateCcw className={repairBusy ? "spin" : undefined} size={12} />
            Apply again
          </button>
        )}
      </div>
      {update.change_summary.length > 0 && (
        <ul className="chat-graph-change-summary">
          {update.change_summary.map((item, index) => (
            <li key={`${index}:${item}`}>{item}</li>
          ))}
        </ul>
      )}
      {(update.status === "rejected" || update.status === "unavailable") &&
        update.validation_messages.length > 0 && (
          <ul className="chat-graph-validation">
            {update.validation_messages.map((item, index) => (
              <li key={`${index}:${item}`}>{item}</li>
            ))}
          </ul>
        )}
      {repairError && (
        <strong className="chat-graph-repair-error" role="alert">
          {repairError}
        </strong>
      )}
    </div>
  );
}

const ARTIFACT_EDIT_FAILURES: Record<NonNullable<ArtifactEditOutcome["reason"]>, string> = {
  missing: "the file was removed",
  type_changed: "the file type changed",
  too_large: "the file is too large",
  unverified: "the edited file could not be verified",
};

/** Say why an artifact of this session has no new version under this reply. */
function artifactEditOutcomeText(
  outcome: ArtifactEditOutcome,
  outcomes: ArtifactEditOutcome[],
  artifacts: AgentArtifactDescriptor[],
): string {
  if (outcome.status === "forked")
    return "The artifact changed during this turn, so the edit was saved as a separate artifact.";
  if (outcome.status === "failed")
    return `Edit not published: ${outcome.reason ? ARTIFACT_EDIT_FAILURES[outcome.reason] : "it could not be read"}. The current version is kept.`;
  // Only artifacts this turn created; one it updated in place has its own outcome.
  const separate = artifacts.filter(
    (item) => !outcomes.some((other) => other.artifact_id === item.artifact_id),
  );
  return separate.length
    ? `Not edited in place; the current version is kept. ${separate.map((item) => item.name).join(", ")} is a separate artifact.`
    : "Not edited in place; the current version is kept.";
}

function modeLabel(mode: ConversationMode): "Discuss" | "Work" {
  return mode === "discuss" ? "Discuss" : "Work";
}
