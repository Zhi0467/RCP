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
import { useState } from "react";
import { ApiError } from "../core/api";
import { errorMessage } from "../core/errors";
import type { AgentQuestion, ApprovalCard, ApprovalCardKind } from "../core/types";
import { approvalCardDetails, approvalCardExpired, approvalCardTitle } from "./agentThreadModel";
import { resolveApprovalCard } from "./agentThreadsApi";

const ICONS: Record<ApprovalCardKind, LucideIcon> = {
  proposal: Lightbulb,
  truth_scope: LockKeyhole,
  auto_research: Rocket,
  merge_branch: GitMerge,
  merge_episode: GitMerge,
};

/**
 * A protected act the agent asked for. Only this tap resolves it: Approve runs the
 * human route as the member, and Decline answers the question. Neither is a tool.
 */
export function ApprovalCardView({
  question,
  card,
  projectName,
  onResolved,
}: {
  question: AgentQuestion;
  card: ApprovalCard;
  projectName: string | null;
  onResolved: () => void;
}) {
  const [details, setDetails] = useState(false);
  const [busy, setBusy] = useState<"approve" | "decline" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const Icon = ICONS[card.kind];
  const answered = question.state === "answered" || question.state === "dismissed";
  const expired = !answered && approvalCardExpired(card, Date.now());
  const pending = !answered && !expired;
  const running = card.state === "running" || busy !== null;
  const state = answered ? (question.answer ?? "answered") : expired ? "expired" : "pending";
  const decide = async (decision: "approve" | "decline") => {
    setBusy(decision);
    setError(null);
    try {
      const result = await resolveApprovalCard(question.question_id, {
        decision,
        pinned_sha256: card.pinned_sha256,
      });
      if (result.outcome === "failed" || result.outcome === "stale")
        setError(result.reason ?? null);
    } catch (failure) {
      // Another tap took the card; its outcome arrives with the refresh.
      if (!(failure instanceof ApiError && failure.code === "card_taken"))
        setError(errorMessage(failure));
    } finally {
      setBusy(null);
      onResolved();
    }
  };
  return (
    <section
      className={`approval-card${details ? " open" : ""}`}
      data-state={state.split(":")[0]}
      aria-label={approvalCardTitle(card)}
    >
      <div className="approval-head">
        <span className="approval-icon" aria-hidden="true">
          <Icon size={pending ? 16 : 14} />
        </span>
        <div className="approval-title">
          <strong>{approvalCardTitle(card)}</strong>
          {projectName && pending && (
            <a
              className="entity-chip project"
              href={`#/projects/${encodeURIComponent(card.action.project_id)}`}
            >
              <Files size={11} aria-hidden="true" />
              {projectName}
            </a>
          )}
        </div>
        <span className="question-state">
          {pending ? "Needs you" : answered ? question.answer : "Expired"}
        </span>
      </div>
      {details && (
        <div className="approval-details">
          {question.question && <p>{question.question}</p>}
          <dl>
            {approvalCardDetails(card).map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
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
            aria-expanded={details}
            onClick={() => setDetails(!details)}
          >
            See details
            <ChevronRight size={14} aria-hidden="true" />
          </button>
          <button
            className="button secondary"
            type="button"
            disabled={running}
            onClick={() => void decide("decline")}
          >
            {busy === "decline" && <LoaderCircle className="spin" size={14} />}
            Decline
          </button>
          <button
            className="button primary"
            type="button"
            disabled={running}
            onClick={() => void decide("approve")}
          >
            {(busy === "approve" || card.state === "running") && (
              <LoaderCircle className="spin" size={14} />
            )}
            Approve
          </button>
        </div>
      )}
    </section>
  );
}
