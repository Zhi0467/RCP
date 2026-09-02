import { AlertTriangle, ArrowRight, Check, X } from "lucide-react";
import type { GlossaryIndex } from "../glossary";
import { proposalApprovalConflict, type HumanDraft, type ProposalDecision } from "../humanDraft";
import { type GraphNode, type GraphState, type Proposal, type ProposalActionLine } from "../types";
import { GlossaryText } from "./GlossaryText";

interface ProposalJudgmentSectionProps {
  proposals: Proposal[];
  graph: GraphState;
  glossaryIndex: GlossaryIndex;
  draft: HumanDraft | null;
  mutationsDisabled?: boolean;
  proposalActions: Record<string, ProposalActionLine[]>;
  onDecision: (proposal: Proposal, decision: ProposalDecision | null) => void;
}

interface AttentionRailProps {
  decisions: GraphNode[];
  blockers: GraphNode[];
  onSelectNode: (nodeId: string) => void;
}

export function ProposalJudgmentSection({
  proposals,
  graph,
  glossaryIndex,
  draft,
  mutationsDisabled = false,
  proposalActions,
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
        const proposedAction = proposalActions[proposal.id];
        if (!proposedAction) {
          throw new Error(`Attention projection is missing Proposal action ${proposal.id}.`);
        }
        const approvalConflict = proposalApprovalConflict(draft, graph, proposal.id);
        const conflictingTitles = approvalConflict?.proposalIds.map(
          (proposalId) => graph.proposals[proposalId]?.title ?? proposalId,
        );
        const approvalConflictText = conflictingTitles?.length
          ? `Approval conflicts with staged approval: ${conflictingTitles.join(", ")}.`
          : undefined;
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
                <dt>Proposed action</dt>
                <dd>
                  {proposedAction.map((line, index) => (
                    <div key={`${line.label ?? "action"}-${index}`}>
                      {line.label && <strong>{line.label}: </strong>}
                      <GlossaryText text={line.text} glossaryIndex={glossaryIndex} />
                    </div>
                  ))}
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
                aria-describedby={approvalConflict ? `proposal-conflict-${proposal.id}` : undefined}
                disabled={mutationsDisabled || (!approved && Boolean(approvalConflict))}
                title={approvalConflictText}
                onClick={() => onDecision(proposal, approved ? null : "approved")}
              >
                <Check size={14} />
                Approve
              </button>
              {approvalConflictText && (
                <span className="eyebrow" id={`proposal-conflict-${proposal.id}`} role="status">
                  {approvalConflictText}
                </span>
              )}
            </div>
          </article>
        );
      })}
    </section>
  );
}

export function AttentionRail({ decisions, blockers, onSelectNode }: AttentionRailProps) {
  const total = decisions.length + blockers.length;
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

      {decisions.map((decision) => (
        <button
          className={`attention-item decision${decision.draft_touched ? " draft-touched" : ""}`}
          key={decision.id}
          onClick={() => onSelectNode(decision.id)}
        >
          <strong>{decision.title}</strong>
          <span className={`decision-attention-status ${decision.status}`}>
            {decision.status === "revisit" ? "Revisit" : "Ready"}
          </span>
        </button>
      ))}

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
