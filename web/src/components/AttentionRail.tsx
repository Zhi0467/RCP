import { AlertTriangle, ArrowRight, Check, X } from "lucide-react";
import type { GlossaryIndex } from "../glossary";
import type { HumanDraft, ProposalDecision } from "../humanDraft";
import type { GraphNode, Proposal } from "../types";
import { GlossaryText } from "./GlossaryText";

interface ProposalJudgmentSectionProps {
  proposals: Proposal[];
  glossaryIndex: GlossaryIndex;
  draft: HumanDraft | null;
  mutationsDisabled?: boolean;
  onDecision: (proposal: Proposal, decision: ProposalDecision | null) => void;
}

interface AttentionRailProps {
  decisions: GraphNode[];
  blockers: GraphNode[];
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
        const proposedAction = proposalAction(proposal);
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
                  <GlossaryText text={proposedAction} glossaryIndex={glossaryIndex} />
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

function proposalAction(proposal: Proposal): string {
  const operations = Array.isArray(proposal.ops) ? proposal.ops : [];
  const update = operations.find((operation) => operation.op === "update_nodes");
  const nodes = update?.nodes;
  const node = Array.isArray(nodes) && nodes.length === 1 ? asRecord(nodes[0]) : null;
  const changes = node ? asRecord(node.changes) : null;
  const selectedOption = changes && stringValue(changes.selected_option);
  const status = changes && stringValue(changes.status);

  if (selectedOption && status === "decided") {
    return `Choose “${selectedOption}” and mark the decision decided.`;
  }
  if (selectedOption) return `Choose “${selectedOption}” for the decision.`;
  if (status) return `Change the target status to “${status}”.`;
  return proposal.card.decision_needed || "Review the stored proposal action.";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
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
