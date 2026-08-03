import { AlertTriangle, ArrowRight, Check, MessageSquareText, X } from "lucide-react";
import type { GlossaryIndex } from "../glossary";
import type { AmbiguityDecision, HumanDraft, ProposalDecision } from "../humanDraft";
import type { Ambiguity, GraphNode, Proposal } from "../types";
import { GlossaryText } from "./GlossaryText";

interface ProposalJudgmentSectionProps {
  proposals: Proposal[];
  glossaryIndex: GlossaryIndex;
  draft: HumanDraft | null;
  mutationsDisabled?: boolean;
  onDecision: (proposal: Proposal, decision: ProposalDecision | null) => void;
}

interface AttentionRailProps {
  ambiguities: Ambiguity[];
  blockers: GraphNode[];
  draft: HumanDraft | null;
  mutationsDisabled?: boolean;
  onAmbiguity: (ambiguity: Ambiguity, status: AmbiguityDecision | null) => void;
  onSelectNode: (nodeId: string) => void;
}

export function ProposalJudgmentSection({
  proposals,
  glossaryIndex,
  draft,
  mutationsDisabled = false,
  onDecision,
}: ProposalJudgmentSectionProps) {
  if (proposals.length === 0) return null;

  return (
    <section className="proposal-judgment-section" aria-label="Pending proposals">
      <header className="rail-heading proposal-section-heading">
        <h2>Pending proposals</h2>
        <span className="count-badge">{proposals.length}</span>
      </header>

      {proposals.map((proposal) => {
        const decision = draft?.proposals[proposal.id]?.decision;
        const approved = decision === "approved";
        const rejected = decision === "rejected";
        return (
          <article className={`proposal-card${decision ? " draft-touched" : ""}`} key={proposal.id}>
            <div className="proposal-topline">
              <span className="eyebrow">
                {decision ? `Pending · staged ${decision}` : "Pending proposal"}
              </span>
              <span className="mono">rev {proposal.base_rev}</span>
            </div>
            <h3>
              <GlossaryText text={proposal.title} glossaryIndex={glossaryIndex} />
            </h3>
            <dl className="card-brief">
              <div>
                <dt>The situation, cold</dt>
                <dd>
                  <GlossaryText
                    text={
                      proposal.card.situation_cold ||
                      "The agent did not supply a cold-readable summary."
                    }
                    glossaryIndex={glossaryIndex}
                  />
                </dd>
              </div>
              <div>
                <dt>Why you, why now</dt>
                <dd>
                  <GlossaryText
                    text={
                      proposal.card.why_human_now || "Human authority is required by the gate set."
                    }
                    glossaryIndex={glossaryIndex}
                  />
                </dd>
              </div>
              <div>
                <dt>If accepted</dt>
                <dd>
                  <GlossaryText
                    text={proposal.card.consequences || "Consequences were not made explicit."}
                    glossaryIndex={glossaryIndex}
                  />
                </dd>
              </div>
              <div>
                <dt>Decision needed</dt>
                <dd>
                  <GlossaryText
                    text={
                      proposal.card.decision_needed || "Approve or reject the stored operation."
                    }
                    glossaryIndex={glossaryIndex}
                  />
                </dd>
              </div>
            </dl>
            <div className="card-actions">
              <button
                className={`button judgment proposal-decision-toggle reject${rejected ? " selected disagree" : ""}`}
                aria-pressed={rejected}
                disabled={mutationsDisabled}
                onClick={() => onDecision(proposal, rejected ? null : "rejected")}
              >
                {rejected ? <Check size={14} /> : <X size={14} />}
                Reject
              </button>
              <button
                className={`button judgment proposal-decision-toggle approve${approved ? " selected agree" : ""}`}
                aria-pressed={approved}
                disabled={mutationsDisabled}
                onClick={() => onDecision(proposal, approved ? null : "approved")}
              >
                <Check size={14} />
                Approve
              </button>
            </div>
          </article>
        );
      })}
    </section>
  );
}

export function AttentionRail({
  ambiguities,
  blockers,
  draft,
  mutationsDisabled = false,
  onAmbiguity,
  onSelectNode,
}: AttentionRailProps) {
  const total = ambiguities.length + blockers.length;
  return (
    <aside className="attention-rail" aria-label="Needs your judgment">
      <header className="rail-heading">
        <h2>Needs your judgment</h2>
        <span className="count-badge">{total}</span>
      </header>

      {total === 0 && (
        <div className="quiet-empty">
          <Check size={16} />
          <strong>No other judgment queued</strong>
        </div>
      )}

      {ambiguities.map((ambiguity) => {
        const status = draft?.ambiguities[ambiguity.id]?.status;
        return (
          <article className={`ambiguity-card${status ? " draft-touched" : ""}`} key={ambiguity.id}>
            <button
              className="attention-item"
              onClick={() => {
                const nodeId = ambiguity.related_node_ids[0];
                if (nodeId) onSelectNode(nodeId);
              }}
            >
              <MessageSquareText size={15} />
              <strong>{ambiguity.question}</strong>
              <ArrowRight size={14} />
            </button>
            <div className="card-actions ambiguity-actions">
              <button
                className={`button judgment${status === "dismissed" ? " selected disagree" : ""}`}
                aria-pressed={status === "dismissed"}
                disabled={mutationsDisabled}
                onClick={() => onAmbiguity(ambiguity, status === "dismissed" ? null : "dismissed")}
              >
                <X size={13} /> Dismiss
              </button>
              <button
                className={`button judgment${status === "resolved" ? " selected agree" : ""}`}
                aria-pressed={status === "resolved"}
                disabled={mutationsDisabled}
                onClick={() => onAmbiguity(ambiguity, status === "resolved" ? null : "resolved")}
              >
                <Check size={13} /> Resolve
              </button>
            </div>
          </article>
        );
      })}

      {blockers.map((blocker) => (
        <button
          className={`attention-item blocker${blocker.draft_touched ? " draft-touched" : ""}`}
          key={blocker.id}
          onClick={() => onSelectNode(blocker.id)}
        >
          <AlertTriangle size={15} />
          <strong>{blocker.title}</strong>
          <ArrowRight size={14} />
        </button>
      ))}
    </aside>
  );
}
