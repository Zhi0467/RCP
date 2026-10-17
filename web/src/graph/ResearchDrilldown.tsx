import { ChevronRight, Search } from "lucide-react";
import { useMemo, useState, type CSSProperties, type ReactNode } from "react";

import type { GraphNode, GraphState, TrustView } from "../core/types";
import type { ProjectViewportRef } from "../projects/projectTabs";
import { useEvidenceFlags } from "./EvidenceFlagContext";
import { EmptyState, ViewHeading } from "./GraphViews";
import { projectNodes } from "./graphProjection";
import { humanize, nodeTypeLabel } from "./nodePresentation";
import {
  buildResearchDrilldown,
  type ExperimentLane,
  type HypothesisPage,
  type HypothesisSummary,
  type QuestionSummary,
  type ResearchDrilldown as Drilldown,
  type Verdict,
  type VerdictTally,
  type WeighedEvidence,
} from "./researchProjection";

/** One step of the drill-down trail; list steps open a Hypothesis's collapsed boxes. */
export type ResearchStep =
  | { kind: "question"; id: string }
  | { kind: "hypothesis"; id: string }
  | { kind: "inputs"; id: string }
  | { kind: "evidence"; id: string }
  | { kind: "results"; id: string; experimentId: string };

const NO_CHANGED_NODES: ReadonlySet<string> = new Set();
const VERDICTS: Verdict[] = ["for", "against", "unclear"];

export function ResearchDrilldown({
  graph,
  trustView,
  onSelectNode,
  changedNodeIds = NO_CHANGED_NODES,
  trailRef,
}: {
  graph: GraphState;
  trustView: TrustView;
  onSelectNode: (node: GraphNode) => void;
  /** Main nodes changed since the viewer's digest mark; empty on other targets. */
  changedNodeIds?: ReadonlySet<string>;
  /** Session-scoped trail, owned by the shell so it survives leaving the view. */
  trailRef: ProjectViewportRef<ResearchStep[]>;
}) {
  const nodes = useMemo(
    () => projectNodes(Object.values(graph.nodes), trustView),
    [graph.nodes, trustView],
  );
  const drilldown = useMemo(
    () => buildResearchDrilldown(nodes, Object.values(graph.edges)),
    [nodes, graph.edges],
  );
  // The shell swaps trailRef when the project or graph target changes.
  const [stored, setStored] = useState(() => ({ ref: trailRef, trail: trailRef.current ?? [] }));
  const trail = validTrail(
    stored.ref === trailRef ? stored.trail : (trailRef.current ?? []),
    drilldown,
  );
  const go = (next: ResearchStep[]) => {
    trailRef.current = next;
    setStored({ ref: trailRef, trail: next });
  };
  const push = (step: ResearchStep) => go([...trail, step]);
  const step = trail.at(-1);
  const card = (node: GraphNode, props: Omit<CardProps, "node" | "onClick" | "changed"> = {}) => (
    <ResearchNodeCard
      key={node.id}
      node={node}
      onClick={() => onSelectNode(node)}
      changed={changedNodeIds.has(node.id)}
      {...props}
    />
  );
  const hidden = Object.values(graph.nodes).length - nodes.length;

  if (!step) {
    return (
      <section className="view-panel research-view">
        <ViewHeading
          title="Research"
          aside={
            hidden > 0
              ? `${hidden} hidden`
              : `${drilldown.roots.length} question${drilldown.roots.length === 1 ? "" : "s"}`
          }
        />
        {drilldown.roots.length === 0 && drilldown.unplaced.length === 0 ? (
          <EmptyState icon={<Search size={20} />} title="No research structure" />
        ) : (
          <>
            <div className="research-card-grid">
              {drilldown.roots.map((item) => (
                <QuestionCard
                  key={item.node.id}
                  item={item}
                  changed={changedNodeIds.has(item.node.id)}
                  onOpen={() => push({ kind: "question", id: item.node.id })}
                />
              ))}
            </div>
            {drilldown.unplaced.length > 0 && (
              <section className="research-unconnected">
                <header>
                  <strong>Not yet connected</strong>
                  <span>{drilldown.unplaced.length}</span>
                </header>
                <div>{drilldown.unplaced.map((node) => card(node, { compact: true }))}</div>
              </section>
            )}
          </>
        )}
      </section>
    );
  }

  const hypothesis = step.kind === "question" ? null : drilldown.hypotheses.get(step.id)!;
  return (
    <section className="view-panel research-view">
      <ResearchTrail trail={trail} drilldown={drilldown} onGo={go} />
      {step.kind === "question" && (
        <QuestionPanel
          drilldown={drilldown}
          id={step.id}
          changedNodeIds={changedNodeIds}
          card={card}
          onSelectNode={onSelectNode}
          onOpen={push}
        />
      )}
      {step.kind === "hypothesis" && hypothesis && (
        <HypothesisPanel
          page={hypothesis}
          card={card}
          onSelectNode={onSelectNode}
          onOpen={push}
          onOpenQuestion={(id) => go([{ kind: "question", id }])}
          currentQuestionId={
            [...trail].reverse().find((item) => item.kind === "question")?.id ?? null
          }
        />
      )}
      {step.kind === "inputs" && hypothesis && (
        <div className="research-card-grid">
          {hypothesis.inputs.nodes.map((node) =>
            card(node, {
              badge: node.status ? (
                <span className="research-chip">{humanize(node.status)}</span>
              ) : undefined,
            }),
          )}
        </div>
      )}
      {step.kind === "evidence" && hypothesis && (
        <EvidenceList evidence={hypothesis.evidence} card={card} />
      )}
      {step.kind === "results" && hypothesis && (
        <EvidenceList
          evidence={
            hypothesis.experiments.find((lane) => lane.node.id === step.experimentId)?.evidence ??
            []
          }
          card={card}
        />
      )}
    </section>
  );
}

/** Keep the longest prefix of the trail that still names live records. */
function validTrail(trail: ResearchStep[], drilldown: Drilldown): ResearchStep[] {
  const valid: ResearchStep[] = [];
  for (const step of trail) {
    const live =
      step.kind === "question"
        ? drilldown.questions.has(step.id)
        : step.kind === "results"
          ? Boolean(
              drilldown.hypotheses
                .get(step.id)
                ?.experiments.some((lane) => lane.node.id === step.experimentId),
            )
          : drilldown.hypotheses.has(step.id);
    if (!live) break;
    valid.push(step);
  }
  return valid;
}

function stepLabel(step: ResearchStep, drilldown: Drilldown): string {
  if (step.kind === "question") return drilldown.questions.get(step.id)!.node.title;
  const page = drilldown.hypotheses.get(step.id)!;
  if (step.kind === "hypothesis") return page.node.title;
  if (step.kind === "inputs") return "Inputs";
  if (step.kind === "evidence") return "Evidence";
  return page.experiments.find((lane) => lane.node.id === step.experimentId)!.node.title;
}

function ResearchTrail({
  trail,
  drilldown,
  onGo,
}: {
  trail: ResearchStep[];
  drilldown: Drilldown;
  onGo: (trail: ResearchStep[]) => void;
}) {
  return (
    <nav className="research-trail" aria-label="Research trail">
      <button type="button" onClick={() => onGo([])}>
        Questions
      </button>
      {trail.map((step, index) => (
        <span className="research-trail-step" key={`${step.kind}:${step.id}:${index}`}>
          <ChevronRight size={13} aria-hidden="true" />
          {index === trail.length - 1 ? (
            <span aria-current="page">{stepLabel(step, drilldown)}</span>
          ) : (
            <button type="button" onClick={() => onGo(trail.slice(0, index + 1))}>
              {stepLabel(step, drilldown)}
            </button>
          )}
        </span>
      ))}
    </nav>
  );
}

type CardRenderer = (
  node: GraphNode,
  props?: Omit<CardProps, "node" | "onClick" | "changed">,
) => ReactNode;

function QuestionPanel({
  drilldown,
  id,
  changedNodeIds,
  card,
  onSelectNode,
  onOpen,
}: {
  drilldown: Drilldown;
  id: string;
  changedNodeIds: ReadonlySet<string>;
  card: CardRenderer;
  onSelectNode: (node: GraphNode) => void;
  onOpen: (step: ResearchStep) => void;
}) {
  const page = drilldown.questions.get(id)!;
  return (
    <>
      <FocusTitle node={page.node} onSelectNode={onSelectNode} />
      {page.subquestions.length > 0 && (
        <ResearchSection label="Sub-questions">
          {page.subquestions.map((item) => (
            <QuestionCard
              key={item.node.id}
              item={item}
              changed={changedNodeIds.has(item.node.id)}
              onOpen={() => onOpen({ kind: "question", id: item.node.id })}
            />
          ))}
        </ResearchSection>
      )}
      {page.hypotheses.length > 0 && (
        <ResearchSection label="Hypotheses">
          {page.hypotheses.map((item) => (
            <HypothesisCard
              key={item.node.id}
              item={item}
              changed={changedNodeIds.has(item.node.id)}
              onOpen={() => onOpen({ kind: "hypothesis", id: item.node.id })}
            />
          ))}
        </ResearchSection>
      )}
      {page.loose.length > 0 && (
        <ResearchSection label="Not under a hypothesis">
          {page.loose.map((node) => card(node, { compact: true }))}
        </ResearchSection>
      )}
    </>
  );
}

function HypothesisPanel({
  page,
  card,
  onSelectNode,
  onOpen,
  onOpenQuestion,
  currentQuestionId,
}: {
  page: HypothesisPage;
  card: CardRenderer;
  onSelectNode: (node: GraphNode) => void;
  onOpen: (step: ResearchStep) => void;
  onOpenQuestion: (id: string) => void;
  currentQuestionId: string | null;
}) {
  const others = page.questions.filter((question) => question.id !== currentQuestionId);
  const id = page.node.id;
  return (
    <>
      <FocusTitle node={page.node} onSelectNode={onSelectNode}>
        {page.node.status && <span className="research-chip">{humanize(page.node.status)}</span>}
        {page.evidence.length > 0 && (
          <button
            type="button"
            className="research-chip-button"
            onClick={() => onOpen({ kind: "evidence", id })}
          >
            <TallyChips tally={page.tally} />
          </button>
        )}
        {others.map((question) => (
          <button
            type="button"
            className="research-chip also"
            key={question.id}
            title={question.title}
            onClick={() => onOpenQuestion(question.id)}
          >
            Also under {question.title}
          </button>
        ))}
      </FocusTitle>
      <div
        className="research-lanes"
        style={{ "--lane-rows": Math.max(1, page.experiments.length) } as CSSProperties}
      >
        {page.inputs.nodes.length > 0 ? (
          <button
            type="button"
            className="research-hyper research-lane-inputs"
            onClick={() => onOpen({ kind: "inputs", id })}
          >
            <strong>Inputs {page.inputs.nodes.length}</strong>
            <span className="research-chips">
              {page.inputs.openBlockers > 0 && (
                <span className="research-chip against">{page.inputs.openBlockers} blocking</span>
              )}
              {page.inputs.openDecisions > 0 && (
                <span className="research-chip open">{page.inputs.openDecisions} undecided</span>
              )}
            </span>
          </button>
        ) : (
          <span className="research-hyper research-lane-inputs empty">—</span>
        )}
        {page.experiments.length === 0 && <span className="research-stage-empty">—</span>}
        {page.experiments.map((lane) => (
          <ExperimentRow
            key={lane.node.id}
            lane={lane}
            card={card}
            onOpenResults={() => onOpen({ kind: "results", id, experimentId: lane.node.id })}
          />
        ))}
      </div>
    </>
  );
}

function ExperimentRow({
  lane,
  card,
  onOpenResults,
}: {
  lane: ExperimentLane;
  card: CardRenderer;
  onOpenResults: () => void;
}) {
  return (
    <>
      {card(lane.node, {
        className: `research-lane-experiment ${lane.blocked ? "blocked" : ""}`,
        badge: lane.node.status ? (
          <span
            className={`research-chip ${lane.blocked ? "against" : lane.active ? "active" : ""}`}
          >
            {humanize(lane.node.status)}
          </span>
        ) : undefined,
      })}
      {lane.evidence.length > 0 ? (
        <button
          type="button"
          className="research-hyper research-lane-results"
          onClick={onOpenResults}
        >
          <strong>Evidence {lane.evidence.length}</strong>
          <TallyChips tally={lane.tally} />
        </button>
      ) : (
        <span className="research-hyper research-lane-results empty">—</span>
      )}
    </>
  );
}

function EvidenceList({ evidence, card }: { evidence: WeighedEvidence[]; card: CardRenderer }) {
  return (
    <div className="research-card-grid">
      {evidence.map((item) =>
        card(item.node, {
          badge: item.verdict ? (
            <span className={`research-chip ${item.verdict}`}>{item.verdict}</span>
          ) : undefined,
        }),
      )}
    </div>
  );
}

function QuestionCard({
  item,
  changed,
  onOpen,
}: {
  item: QuestionSummary;
  changed: boolean;
  onOpen: () => void;
}) {
  const { node } = item;
  const question = typeof node.question === "string" ? node.question.trim() : "";
  const motivation = typeof node.motivation === "string" ? node.motivation.trim() : "";
  const restatesTitle =
    question.replace(/[?.\s]+$/, "").toLowerCase() ===
    node.title.replace(/[?.\s]+$/, "").toLowerCase();
  return (
    <ResearchNodeCard node={node} onClick={onOpen} changed={changed} className="question-card">
      {question && !restatesTitle && <span className="research-card-text">{question}</span>}
      {motivation && <span className="research-card-text secondary">{motivation}</span>}
      <span className="research-chips">
        {item.subquestionCount > 0 && (
          <span className="research-chip">{plural(item.subquestionCount, "sub-question")}</span>
        )}
        {(item.hypothesisCount > 0 || item.subquestionCount === 0) && (
          <span className="research-chip">
            {plural(item.hypothesisCount, "hypothesis", "hypotheses")}
          </span>
        )}
        <OpenChips decisions={item.openDecisions} blockers={item.openBlockers} />
      </span>
    </ResearchNodeCard>
  );
}

function HypothesisCard({
  item,
  changed,
  onOpen,
}: {
  item: HypothesisSummary;
  changed: boolean;
  onOpen: () => void;
}) {
  const evidenceCount = VERDICTS.reduce((total, verdict) => total + item.tally[verdict], 0);
  return (
    <ResearchNodeCard
      node={item.node}
      onClick={onOpen}
      changed={changed}
      badge={
        item.questionIds.length > 1 ? (
          <span className="research-chips">
            <span className="research-chip also">
              +{plural(item.questionIds.length - 1, "question")}
            </span>
            <span className={`standing ${item.node.standing}`}>{item.node.standing}</span>
          </span>
        ) : undefined
      }
    >
      <span className="research-card-rows">
        <span className="research-card-row">
          <span>Inputs</span>
          <span className="research-chips">
            <span className="research-count">{item.inputCount}</span>
            <OpenChips decisions={item.openDecisions} blockers={item.openBlockers} />
          </span>
        </span>
        <span className="research-card-row">
          <span>Experiments</span>
          <span className="research-chips">
            <span className="research-count">{item.experimentCount}</span>
            {item.activeExperiments > 0 && (
              <span className="research-chip active">{item.activeExperiments} active</span>
            )}
          </span>
        </span>
        <span className="research-card-row">
          <span>Evidence</span>
          <span className="research-chips">
            <span className="research-count">{evidenceCount}</span>
            <TallyChips tally={item.tally} />
          </span>
        </span>
      </span>
    </ResearchNodeCard>
  );
}

function OpenChips({ decisions, blockers }: { decisions: number; blockers: number }) {
  return (
    <>
      {decisions > 0 && <span className="research-chip open">{decisions} undecided</span>}
      {blockers > 0 && <span className="research-chip against">{blockers} blocked</span>}
    </>
  );
}

function plural(count: number, word: string, many = `${word}s`): string {
  return `${count} ${count === 1 ? word : many}`;
}

function TallyChips({ tally }: { tally: VerdictTally }) {
  return (
    <>
      {VERDICTS.filter((verdict) => tally[verdict] > 0).map((verdict) => (
        <span className={`research-chip ${verdict}`} key={verdict}>
          {tally[verdict]} {verdict}
        </span>
      ))}
    </>
  );
}

function FocusTitle({
  node,
  onSelectNode,
  children,
}: {
  node: GraphNode;
  onSelectNode: (node: GraphNode) => void;
  children?: ReactNode;
}) {
  return (
    <header className="research-focus">
      <button type="button" className="research-focus-title" onClick={() => onSelectNode(node)}>
        <span>{nodeTypeLabel(node)}</span>
        <strong>{node.title}</strong>
      </button>
      {children && <div className="research-chips">{children}</div>}
    </header>
  );
}

function ResearchSection({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="research-section">
      <span className="research-stage-label">{label}</span>
      <div className="research-card-grid">{children}</div>
    </section>
  );
}

interface CardProps {
  node: GraphNode;
  onClick: () => void;
  compact?: boolean;
  changed?: boolean;
  /** Replaces the standing label in the card's top line. */
  badge?: ReactNode;
  className?: string;
  children?: ReactNode;
}

function ResearchNodeCard({
  node,
  onClick,
  compact = false,
  changed = false,
  badge,
  className = "",
  children,
}: CardProps) {
  const { nodeIds: flaggedNodeIds } = useEvidenceFlags();
  return (
    <button
      type="button"
      className={`research-node-card ${flaggedNodeIds.has(node.id) ? "has-evidence-flag" : ""} ${node.standing} ${node.draft_touched ? "draft-touched" : ""} ${compact ? "compact" : ""} ${changed ? "digest-changed" : ""} ${className}`}
      onClick={onClick}
    >
      <span className="research-node-topline">
        <span>{nodeTypeLabel(node)}</span>
        {badge ?? <span className={`standing ${node.standing}`}>{node.standing}</span>}
      </span>
      <strong>{node.title}</strong>
      {children}
    </button>
  );
}
