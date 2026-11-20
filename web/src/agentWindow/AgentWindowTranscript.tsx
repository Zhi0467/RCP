import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  ChevronRight,
  History,
  Inbox,
  LoaderCircle,
  Play,
  RotateCcw,
  TriangleAlert,
} from "lucide-react";
import {
  localClock,
  timeDividerLabel,
  turnCommands,
  turnSteps,
  turnStepsSummary,
} from "../agents/agentThreadModel";
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
  GraphNode,
  GraphUpdateRecovery,
  GraphUpdateResult,
  TurnOutcome,
} from "../core/types";
import type { GlossaryIndex } from "../graph/glossary";
import { ReplyLinkButton } from "../core/ReplyLinkButton";
import { CHAT_USER_MESSAGE_COLLAPSE_THRESHOLD, THREAD_TIME_GAP_MINUTES } from "../core/uiConstants";
import { MarkdownAnswer } from "../core/chatMarkdown";
import { BrowserTurnNotice } from "../core/BrowserControls";
import { ArtifactCommentQuotes, artifactCommentLead } from "../chat/ArtifactCommentQuotes";
import { AnswerCommentButton, type AnswerComments } from "../chat/answerComments";
import { SentAttachments } from "../chat/SentAttachments";
import { CompactionDivider } from "../chat/AgentStatusStrip";
import { compactionAnchors } from "../chat/agentStatusModel";
import { questionTranscript } from "../chat/questions";
import { isPlanCardQuestion } from "../agents/experimentPlanModel";
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
  /** Each answer offers its keyboard Comment, staged in the composer; a project chat's
   *  replies take shared threads instead. */
  comments?: AnswerComments;
  compactionByLine: ReturnType<typeof compactionAnchors>;
  continuedTaskIds: Set<string>;
  latestGraphReceiptLineIds: Set<string>;
  renderQuestion?: (question: AgentQuestion) => ReactNode;
  /** Card questions the owner shows elsewhere; their typed answers stay hidden here too. */
  shownElsewhere?: ReadonlySet<string>;
  /** Shows each position's cards as one unit; without it each asked card uses `renderQuestion`. */
  renderCards?: (entry: AgentWindowCardsEntry) => ReactNode;
  /** An Experiment plan card, drawn inside its turn's answer so reply comments reach its text. */
  renderPlanCard?: (question: AgentQuestion) => ReactNode;
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
  /** What the owner ends a line with, such as the cards its turn produced. */
  lineTrail?: (line: TaskTranscriptLine) => ReactNode;
  stopControl?: ReactNode;
  footer?: ReactNode;
  /** A project chat's `main` or `branch:<id>`; its replies then offer a share link. */
  replyLinkGraphTarget?: string;
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
const NONE_ELSEWHERE: ReadonlySet<string> = new Set();
const NO_PLANS: ReadonlyMap<string, AgentQuestion[]> = new Map();

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
  shownElsewhere = NONE_ELSEWHERE,
  renderCards,
  renderPlanCard,
  lineLead,
  lineClassName,
  humanLead,
  humanBody,
  agentBody,
  triggerChip = watcherChip,
  browserNotices = true,
  agentProgress,
  lineTrail,
  stopControl,
  footer,
  replyLinkGraphTarget,
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
  const plansByLine = renderPlanCard
    ? planCardPlacement(transcript, questions, shownElsewhere)
    : NO_PLANS;
  const entries = agentWindowEntries(
    transcript,
    questions,
    plansByLine.size
      ? new Set([...shownElsewhere, ...[...plansByLine.values()].flat().map((q) => q.question_id)])
      : shownElsewhere,
  );
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
        if (entry.kind === "cards") {
          const plans = renderPlanCard ? entry.asked.filter(isPlanCardQuestion) : [];
          const asked = entry.asked.filter((question) => !plans.includes(question));
          return (
            <Fragment key={entry.key}>
              {asked.length > 0 &&
                (renderCards
                  ? renderCards({ ...entry, asked })
                  : asked.map((question) => (
                      <Fragment key={question.question_id}>{renderQuestion?.(question)}</Fragment>
                    )))}
              {plans.map((question) => (
                <Fragment key={question.question_id}>{renderPlanCard?.(question)}</Fragment>
              ))}
            </Fragment>
          );
        }
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
        const plans = plansByLine.get(line.lineId) ?? [];
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
              className={`agent-chat-view-line ${lineClassName?.(line) ?? line.role}${failedTurn ? " unsent" : ""}`}
              data-source={line.source ?? undefined}
              data-task-id={line.taskId}
              data-time-shown={timeShownIds.has(line.lineId) ? "" : undefined}
              onClick={shownAt ? (event) => toggleTime(line.lineId, event.target) : undefined}
            >
              {lineLead?.(line, task)}
              {browserNotices && (
                <BrowserTurnNotice status={line.browserStatus} turnId={line.taskId} />
              )}
              {showModes && line.role === "human" && line.mode === "discuss" && (
                <span className="chat-turn-mode discuss">Discuss</span>
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
                    activeLineTask && (
                      <InlineTaskProgress
                        task={activeLineTask}
                        control={activeLineTask === relatedTasks.at(-1) ? stopControl : null}
                      />
                    )
                  )
                ) : (
                  (line.text || plans.length > 0) && (
                    <>
                      {agentProgress?.(line, task)}
                      <div className="chat-markdown chat-annotatable-answer">
                        {line.text && (
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
                        )}
                        {plans.map((question) => (
                          <Fragment key={question.question_id}>
                            {renderPlanCard?.(question)}
                          </Fragment>
                        ))}
                      </div>
                      {/* Outside the annotatable wrapper: a selection clamp or the
                        keyboard flow must never stage this diagnostic as answer text. */}
                      {artifacts.repositoryFileErrors.get(messageId) && (
                        <strong className="chat-repository-file-error" role="alert">
                          {artifacts.repositoryFileErrors.get(messageId)}
                        </strong>
                      )}
                      {!readOnly && comments && <AnswerCommentButton comments={comments} />}
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
                        <span className="agent-chat-view-text">{line.text}</span>
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
                    <SentAttachments
                      attachments={line.attachments}
                      projectId={endpoints.projectId!}
                      now={expiryClock}
                    />
                    {agentProgress ? (
                      agentProgress(line, task)
                    ) : pausedLineTask ? (
                      <InlinePausedTask
                        task={pausedLineTask}
                        disabled={readOnly}
                        onResume={() => onResumeTask?.(pausedLineTask)}
                        onRetry={() => onRetryTask?.(pausedLineTask)}
                      />
                    ) : null}
                  </>
                ))
              ) : pausedLineTask ? null : (
                <span className="agent-chat-view-text">{line.text}</span>
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
              {lineTrail?.(line)}
              {replyLinkGraphTarget &&
                endpoints.projectId &&
                line.role === "agent" &&
                !line.running &&
                line.text && (
                  <ReplyLinkButton
                    projectId={endpoints.projectId}
                    graphTarget={replyLinkGraphTarget}
                    operationId={line.taskId}
                  />
                )}
              {shownAt && line.trigger !== "schedule" && (
                <time className="chat-line-time" dateTime={shownAt.toISOString()}>
                  {localClock(shownAt)}
                </time>
              )}
            </div>
            {line.role === "human" &&
              !agentProgress &&
              !pausedLineTask &&
              (activeLineTask || (pendingLine && !failedTurn)) && (
                <div className="agent-chat-view-line agent">
                  <InlineTaskProgress
                    task={activeLineTask}
                    control={activeLineTask === relatedTasks.at(-1) ? stopControl : null}
                  />
                </div>
              )}
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
  'a, button, input, textarea, select, summary, label, [role="button"], [class*="chip"], .chat-input-reference, .approval-card';

/** A tap on a line shows its time, unless it lands on a link, control or chip, or selects text. */
export function lineTapShowsTime(
  target: Pick<Element, "closest">,
  selection: Pick<Selection, "isCollapsed" | "toString"> | null,
): boolean {
  if (target.closest(TAP_IGNORED)) return false;
  return !selection || selection.isCollapsed || !selection.toString();
}

/**
 * Plan cards by the line that shows them: the last finished answer of the turn that
 * raised each. A card whose turn has no answer here keeps the ordinary card place,
 * still drawn as a plan card.
 */
export function planCardPlacement(
  lines: TaskTranscriptLine[],
  questions: AgentQuestion[],
  shownElsewhere: ReadonlySet<string> = NONE_ELSEWHERE,
): Map<string, AgentQuestion[]> {
  const placed = new Map<string, AgentQuestion[]>();
  const byAge = [...questions].sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const question of byAge) {
    if (!isPlanCardQuestion(question) || shownElsewhere.has(question.question_id)) continue;
    const owner = lines
      .filter(
        (line) =>
          line.taskId === question.operation_id &&
          line.role === "agent" &&
          line.source !== "voice" &&
          !line.running,
      )
      .at(-1);
    if (owner) placed.set(owner.lineId, [...(placed.get(owner.lineId) ?? []), question]);
  }
  return placed;
}

/** The cards that share one transcript position; an owner may show them as one stack. */
export type AgentWindowCardsEntry = {
  kind: "cards";
  key: string;
  asked: AgentQuestion[];
};

type PlacedCard = {
  kind: "card";
  turnId: string;
  at: string;
  question: AgentQuestion;
};

/** Cards remain inside their owning turn, even when settlement stamps both messages together. */
export function agentWindowEntries(
  lines: TaskTranscriptLine[],
  questions: AgentQuestion[],
  shownElsewhere: ReadonlySet<string> = NONE_ELSEWHERE,
) {
  const cardIds = new Set(
    questions.filter((question) => question.card).map((question) => question.question_id),
  );
  const asked = questions.filter(
    (question) => question.card && !shownElsewhere.has(question.question_id),
  );
  const visible = lines.filter(
    (line) => !(line.role === "human" && line.questionId && cardIds.has(line.questionId)),
  );
  const entries: Array<
    ReturnType<typeof questionTranscript<TaskTranscriptLine>>[number] | PlacedCard
  > = questionTranscript(
    visible,
    questions.filter((question) => !question.card),
  );
  const cards: PlacedCard[] = asked.map((question) => ({
    kind: "card" as const,
    turnId: question.operation_id,
    at: question.created_at,
    question,
  }));
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
  const grouped: Array<
    ReturnType<typeof questionTranscript<TaskTranscriptLine>>[number] | AgentWindowCardsEntry
  > = [];
  const groupsAfterLine = new Map<string, number>();
  let anchor = "start";
  for (const entry of entries) {
    if (entry.kind !== "card") {
      if (entry.kind === "line") anchor = entry.line.lineId;
      grouped.push(entry);
      continue;
    }
    let group = grouped.at(-1);
    if (group?.kind !== "cards") {
      const seen = groupsAfterLine.get(anchor) ?? 0;
      groupsAfterLine.set(anchor, seen + 1);
      group = { kind: "cards", key: `cards:${anchor}:${seen}`, asked: [] };
      grouped.push(group);
    }
    group.asked.push(entry.question);
  }
  return grouped;
}
function InlineTaskProgress({ task, control }: { task: AgentTask | null; control?: ReactNode }) {
  const running = task
    ? turnCommands(task)
        .filter((command) => command.seconds === null)
        .at(-1)
    : undefined;
  const label =
    running?.text ??
    (task ? task.status_message || `${taskKindLabel(task.kind)} is running` : "Starting task");
  return (
    <>
      {task && <TurnProgress task={task} live />}
      <span className="chat-task-live" role="status" aria-live="polite" aria-atomic="true">
        {label}
      </span>
      <span className="agent-turn-live">
        <LoaderCircle className="spin" size={12} aria-hidden="true" />
        <span aria-hidden="true">{label}</span>
        {control}
      </span>
    </>
  );
}

/** A turn's steps: open while it runs, folded under one line once it settles. */
export function TurnProgress({ task, live = false }: { task: AgentTask; live?: boolean }) {
  const steps = turnSteps(task);
  if (!steps.length) return null;
  const list = (
    <ol>
      {steps.map((step) =>
        step.kind === "message" ? (
          <li key={step.id} className="turn-message">
            <MarkdownAnswer text={step.text} />
          </li>
        ) : (
          <li key={step.id}>
            {step.text}
            {step.seconds !== null && <span>{step.seconds}s</span>}
          </li>
        ),
      )}
    </ol>
  );
  if (live) return <div className="chat-progress live">{list}</div>;
  return (
    <details className="chat-progress">
      <summary>
        <ChevronRight className="chev" size={12} aria-hidden="true" />
        {turnStepsSummary(steps)}
      </summary>
      {list}
    </details>
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
