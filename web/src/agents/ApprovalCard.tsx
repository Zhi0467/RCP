import {
  ChevronRight,
  Files,
  GitMerge,
  Lightbulb,
  LoaderCircle,
  LockKeyhole,
  Rocket,
  type LucideIcon,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { AskedTime } from "../chat/QuestionCard";
import { ApiError } from "../core/api";
import { errorMessage } from "../core/errors";
import { MAIN_GRAPH, projectViewHash } from "../core/graphTarget";
import type {
  AgentQuestion,
  ApprovalCard,
  ApprovalCardKind,
  ApprovalCardResult,
  ProposalCardView,
} from "../core/types";
import { approvalCardDetails, approvalCardTitle } from "./agentThreadModel";
import { decideProposalCard, resolveApprovalCard } from "./agentThreadsApi";

const ICONS: Record<ApprovalCardKind, LucideIcon> = {
  proposal: Lightbulb,
  truth_scope: LockKeyhole,
  auto_research: Rocket,
  merge_branch: GitMerge,
  merge_episode: GitMerge,
};

type Busy = "approve" | "decline" | null;

type SettledResult = NonNullable<ApprovalCard["result"]>;

function settledLabel(result: SettledResult): string {
  if (result.outcome === "failed")
    return [result.outcome, result.reason].filter(Boolean).join(": ");
  if (result.outcome === "declined" && result.rejected === false)
    return `${result.outcome} · Proposal still pending: ${result.reason ?? ""}`;
  return result.outcome;
}

/**
 * One approval card as the member sees it: open with its buttons, or settled to one
 * line. It decides nothing itself; its adapter runs each tap.
 */
export function ApprovalCardShell({
  icon: Icon,
  title,
  state,
  stateLabel,
  project,
  askedAt,
  details,
  error,
  busy,
  running,
  onApprove,
  onDecline,
  onLater,
}: {
  icon: LucideIcon;
  title: string;
  askedAt: string;
  /** "pending" while open, else the settled outcome. */
  state: string;
  stateLabel: string;
  project: { id: string; name: string } | null;
  details: ReactNode;
  error: string | null;
  busy: Busy;
  running: boolean;
  onApprove: () => void;
  onDecline: () => void;
  onLater?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const pending = state === "pending";
  return (
    <section
      className={`approval-card${open ? " open" : ""}`}
      tabIndex={-1}
      data-state={state}
      aria-label={title}
    >
      <div className="approval-head">
        <span className="approval-icon" aria-hidden="true">
          <Icon size={pending ? 16 : 14} />
        </span>
        <div className="approval-title">
          <strong>{title}</strong>
          {project && pending && (
            <a
              className="entity-chip project"
              href={`#/projects/${encodeURIComponent(project.id)}`}
            >
              <Files size={11} aria-hidden="true" />
              {project.name}
            </a>
          )}
        </div>
        <span className="question-state">
          {pending ? stateLabel : stateLabel.charAt(0).toUpperCase() + stateLabel.slice(1)}
        </span>
        <AskedTime at={askedAt} />
      </div>
      {open && <div className="approval-details">{details}</div>}
      {error && (
        <p className="approval-error" role="alert">
          {error}
        </p>
      )}
      {pending && (
        <div className="approval-actions">
          <button
            className="approval-see"
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            See details
            <ChevronRight size={14} aria-hidden="true" />
          </button>
          {onLater && (
            <button className="approval-later" type="button" onClick={onLater}>
              Later
            </button>
          )}
          <button className="button secondary" type="button" disabled={running} onClick={onDecline}>
            {busy === "decline" && <LoaderCircle className="spin" size={14} />}
            Decline
          </button>
          <button className="button primary" type="button" disabled={running} onClick={onApprove}>
            {(busy === "approve" || (running && busy === null)) && (
              <LoaderCircle className="spin" size={14} />
            )}
            Approve
          </button>
        </div>
      )}
    </section>
  );
}

/**
 * A protected act the agent asked for. Only this tap resolves it: Approve runs the
 * human route as the member, and Decline answers the question. Neither is a tool.
 */
export function AskedApprovalCard({
  question,
  card,
  projectName,
  onResolved,
  onLater,
}: {
  question: AgentQuestion;
  card: ApprovalCard;
  projectName: string | null;
  onResolved: (result: ApprovalCardResult | null) => void;
  onLater?: () => void;
}) {
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const answered = question.state === "answered" || question.state === "dismissed";
  // The server settles expiry; a device clock never decides it.
  const expired = !answered && card.state === "expired";
  const pending = !answered && !expired;
  const answer = answered && card.result ? settledLabel(card.result) : (question.answer ?? "");
  const state = answered
    ? (card.result?.outcome ?? question.answer?.split(":")[0] ?? "answered")
    : expired
      ? "expired"
      : "pending";
  const decide = async (decision: "approve" | "decline") => {
    setBusy(decision);
    setError(null);
    let result: ApprovalCardResult | null = null;
    try {
      result = await resolveApprovalCard(question.question_id, {
        decision,
        pinned_sha256: card.pinned_sha256,
      });
    } catch (failure) {
      // Another tap took the card; its outcome arrives with the refresh.
      if (!(failure instanceof ApiError && failure.code === "card_taken"))
        setError(errorMessage(failure));
    } finally {
      setBusy(null);
      onResolved(result);
    }
  };
  return (
    <ApprovalCardShell
      icon={ICONS[card.kind]}
      title={approvalCardTitle(card)}
      state={state}
      stateLabel={pending ? "Needs you" : answered ? answer : "Expired"}
      project={projectName ? { id: card.action.project_id, name: projectName } : null}
      askedAt={question.created_at}
      details={
        <>
          {question.question && <p>{question.question}</p>}
          <dl>
            {approvalCardDetails(card).map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </>
      }
      error={error}
      busy={busy}
      running={card.state === "running" || busy !== null}
      onApprove={() => void decide("approve")}
      onDecline={() => void decide("decline")}
      onLater={onLater}
    />
  );
}

const PROPOSAL_DETAILS: Array<[keyof ProposalCardView["proposal"]["card"], string]> = [
  ["situation_cold", "Situation"],
  ["why_human_now", "Why now"],
  ["consequences", "Consequences"],
  ["decision_needed", "Decision needed"],
];

/**
 * A pending Proposal a turn of this thread raised, drawn live by the server. A tap
 * decides it as the member, the same as an Inbox decision.
 */
export function ProposalApprovalCard({
  view,
  decided,
  showProject,
  onDecided,
  onChanged,
  onLater,
}: {
  view: ProposalCardView;
  decided: "approved" | "rejected" | null;
  showProject: boolean;
  onDecided: (status: "approved" | "rejected") => void;
  onChanged: () => void;
  onLater?: () => void;
}) {
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const title = `Approve Proposal: ${view.proposal.title}?`;
  if ((view.stale || gone) && !decided)
    return (
      <div className="approval-quiet" role="status" title={title} tabIndex={-1}>
        <span>Out of date</span>
        <a href={projectViewHash(view.project_id, MAIN_GRAPH, "attention")}>Open Inbox</a>
        {onLater && (
          <button className="approval-later" type="button" onClick={onLater}>
            Later
          </button>
        )}
      </div>
    );
  const decide = async (decision: "approve" | "decline") => {
    setBusy(decision);
    setError(null);
    const result = await decideProposalCard(
      view.project_id,
      view.proposal.id,
      decision === "approve" ? "approved" : "rejected",
    );
    setBusy(null);
    if (result.outcome === "decided") onDecided(result.status);
    else if (result.outcome === "gone") setGone(true);
    else if (result.outcome === "retry") setError("Try again");
    else if (result.outcome === "refused") setError(result.reason);
    onChanged();
  };
  return (
    <ApprovalCardShell
      icon={Lightbulb}
      title={title}
      state={decided ?? "pending"}
      stateLabel={decided ?? "Needs you"}
      project={showProject ? { id: view.project_id, name: view.project_name } : null}
      askedAt={view.turn_created_at}
      details={
        <>
          <dl className="approval-prose">
            {PROPOSAL_DETAILS.map(([field, label]) =>
              view.proposal.card?.[field] ? (
                <div key={field}>
                  <dt>{label}</dt>
                  <dd>{view.proposal.card[field]}</dd>
                </div>
              ) : null,
            )}
          </dl>
          {view.changes.length > 0 && (
            <ul className="approval-changes">
              {view.changes.map((change, index) => (
                <li key={`${index}:${change.text}`}>
                  {change.label && <strong>{change.label}</strong>}
                  {change.before != null && <span>{change.before} → </span>}
                  <span>{change.text}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      }
      error={error}
      busy={busy}
      running={busy !== null}
      onApprove={() => void decide("approve")}
      onDecline={() => void decide("decline")}
      onLater={onLater}
    />
  );
}
