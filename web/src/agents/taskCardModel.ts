import type {
  AgentQuestion,
  ApprovalCard,
  DockEpisode,
  RunArtifactEntry,
  RunPresentation,
  RunTone,
} from "../core/types";

type SettledResult = NonNullable<ApprovalCard["result"]>;

export type TaskCardPhase =
  | "needs_you"
  | "parked"
  | "starting"
  | "running"
  | "running_here"
  | "ran_here"
  | "ended"
  | "settled";

/** What one task card shows: the run's published display once it has a run, else the card's own state. */
export interface TaskCardView {
  phase: TaskCardPhase;
  /** The plan still takes the member's edits and decision. */
  open: boolean;
  /** The shell's `data-state`: `pending` while the card is drawn at full size. */
  state: string;
  /** The full card's badge: a run's state with its turns. */
  label: string;
  /** A compact row's badge: the state in a few words. */
  badge: string;
  tone: RunTone | null;
}

export function settledLabel(result: SettledResult): string {
  if (result.outcome === "failed")
    return [result.outcome, result.reason].filter(Boolean).join(": ");
  if (result.outcome === "declined" && result.rejected === false)
    return `${result.outcome} · Proposal still pending: ${result.reason ?? ""}`;
  return result.outcome;
}

function capitalized(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function cardView(card: ApprovalCard): TaskCardView {
  const shown = (
    phase: TaskCardPhase,
    state: string,
    label: string,
    open = false,
  ): TaskCardView => ({ phase, open, state, label, badge: label, tone: null });
  if (card.state === "expired") return shown("settled", "expired", "Expired");
  if (card.state === "running_here")
    return shown("running_here", "pending", "Running in this chat");
  if (card.state === "ran_here") return shown("ran_here", "run_here", "Ran in this chat");
  if (card.result)
    return shown("settled", card.result.outcome, capitalized(settledLabel(card.result)));
  if (card.state === "running") return shown("starting", "pending", "Starting");
  return card.state === "parked"
    ? shown("parked", "pending", "Parked", true)
    : shown("needs_you", "pending", "Needs you", true);
}

/**
 * The one projection every task card renders. A run's display is the server's
 * `RunPresentation` as published; before a run, the card's own server state decides.
 */
export function taskCardView(
  card?: ApprovalCard | null,
  run?: RunPresentation | null,
): TaskCardView {
  if (run)
    return {
      phase: run.phase,
      open: false,
      state: "pending",
      label: run.state_line,
      badge: run.label,
      tone: run.tone,
    };
  if (card) return cardView(card);
  return { phase: "settled", open: false, state: "answered", label: "", badge: "", tone: null };
}

/** The dock run a started card names, after its continuations. */
export function cardRun(
  card: ApprovalCard | null | undefined,
  episodes: readonly DockEpisode[],
): DockEpisode | null {
  const id = card?.current_episode_id ?? card?.started_episode_id;
  return id ? (episodes.find((run) => run.episode.episode_id === id) ?? null) : null;
}

/** A started card waits for the run list, so its first paint is the run, not its own state. */
export function cardAwaitsRun(
  card: ApprovalCard | null | undefined,
  runsLoading: boolean,
): boolean {
  return runsLoading && Boolean(card?.current_episode_id ?? card?.started_episode_id);
}

/** The card whose Start created this run, when the chat shows it. */
export function runCard(
  run: DockEpisode,
  questions: readonly AgentQuestion[],
): AgentQuestion | null {
  const id = run.episode.episode_id;
  return (
    questions.find(
      (question) =>
        question.card?.current_episode_id === id || question.card?.started_episode_id === id,
    ) ?? null
  );
}

/** The turn a plan card renders in: the one that last revised it, else the one that raised it. */
export function cardTurnId(question: AgentQuestion): string {
  return question.card?.revised_operation_id ?? question.operation_id;
}

export function isParkedPlan(question: AgentQuestion): boolean {
  return taskCardView(question.card).phase === "parked";
}

export const PANE_SECTIONS = ["running", "parked", "ended", "artifacts"] as const;
export type PaneSection = (typeof PANE_SECTIONS)[number];

export interface AgentPaneContent {
  running: DockEpisode[];
  parked: AgentQuestion[];
  ended: DockEpisode[];
  artifacts: RunArtifactEntry[];
}

/** The pane's four lists. Parked plans are newest first; runs keep the server's order. */
export function agentPaneContent(
  episodes: readonly DockEpisode[],
  questions: readonly AgentQuestion[],
  artifacts: RunArtifactEntry[],
): AgentPaneContent {
  return {
    running: episodes.filter((run) => run.episode.presentation.section === "running"),
    parked: questions
      .filter(isParkedPlan)
      .sort((a, b) => (b.card?.shelved_at ?? "").localeCompare(a.card?.shelved_at ?? "")),
    ended: episodes.filter((run) => run.episode.presentation.section === "ended"),
    artifacts,
  };
}

/** The header button's count: what is running, parked, or made. */
export function agentPaneCount(content: AgentPaneContent): number {
  return content.running.length + content.parked.length + content.artifacts.length;
}

const FOLD_KEY = "rcp:agent-pane:folded:";

export function paneSectionFolded(section: PaneSection): boolean {
  try {
    return localStorage.getItem(FOLD_KEY + section) === "1";
  } catch {
    return false;
  }
}

export function rememberPaneSectionFolded(section: PaneSection, folded: boolean): void {
  try {
    if (folded) localStorage.setItem(FOLD_KEY + section, "1");
    else localStorage.removeItem(FOLD_KEY + section);
  } catch {
    return;
  }
}
