import type { Edge, GraphNode } from "../core/types";

/** How one Evidence node bears on one Hypothesis. */
export type Verdict = "for" | "against" | "unclear";
export type VerdictTally = Record<Verdict, number>;

const VERDICT_BY_RELATION: Record<string, Verdict> = {
  supports: "for",
  weakens: "against",
  refutes: "against",
  contradicts: "against",
  inconclusive: "unclear",
};
const ACTIVE_EXPERIMENT_STATUSES = new Set([
  "designing",
  "implementing",
  "debugging",
  "running",
  "analyzing",
]);
const OPEN_DECISION_STATUSES = new Set(["open", "ready", "revisit"]);

/** What a question card shows without opening it. */
export interface ResearchRollup {
  activeExperiments: number;
  openBlockers: number;
  /** Every record the card stands for, so the view can count digest changes. */
  memberIds: string[];
}

export interface QuestionSummary {
  node: GraphNode;
  rollup: ResearchRollup;
}

export interface HypothesisSummary {
  node: GraphNode;
  experimentCount: number;
  tally: VerdictTally;
  /** Every question this Hypothesis answers; it appears under each. */
  questionIds: string[];
}

export interface QuestionPage {
  node: GraphNode;
  subquestions: QuestionSummary[];
  hypotheses: HypothesisSummary[];
  /** Decisions, Blockers, and Experiments that belong to the question but test no Hypothesis. */
  loose: GraphNode[];
}

export interface WeighedEvidence {
  node: GraphNode;
  /** Null when the Evidence does not bear on the Hypothesis on screen. */
  verdict: Verdict | null;
}

export interface ExperimentLane {
  node: GraphNode;
  active: boolean;
  blocked: boolean;
  evidence: WeighedEvidence[];
  tally: VerdictTally;
}

export interface HypothesisInputs {
  /** Decisions governing, and Blockers stopping, the Hypothesis's Experiments. */
  nodes: GraphNode[];
  openDecisions: number;
  openBlockers: number;
}

export interface HypothesisPage {
  node: GraphNode;
  questions: GraphNode[];
  inputs: HypothesisInputs;
  experiments: ExperimentLane[];
  /** All Evidence bearing on the Hypothesis, including Evidence from no listed Experiment. */
  evidence: WeighedEvidence[];
  tally: VerdictTally;
}

export interface ResearchDrilldown {
  roots: QuestionSummary[];
  questions: Map<string, QuestionPage>;
  hypotheses: Map<string, HypothesisPage>;
  /** Records no question reaches. */
  unplaced: GraphNode[];
}

/**
 * Decompose the research graph into question cards, question pages, and
 * Hypothesis pages. The graph is not a tree: a Hypothesis, Experiment, or
 * Evidence node appears on every page whose relations reach it.
 */
export function buildResearchDrilldown(nodes: GraphNode[], edges: Edge[]): ResearchDrilldown {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  // Index every relation once; the pages below read these maps instead of scanning edges.
  const outgoingIndex = new Map<string, GraphNode[]>();
  const incomingIndex = new Map<string, GraphNode[]>();
  const verdicts = new Map<string, Verdict>();
  const neighbourIds = new Map<string, string[]>();
  const append = <T>(index: Map<string, T[]>, key: string, value: T) => {
    const list = index.get(key);
    if (list) list.push(value);
    else index.set(key, [value]);
  };
  edges.forEach((edge) => {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target) return;
    append(outgoingIndex, `${source.id}\0${edge.relation}`, target);
    append(incomingIndex, `${target.id}\0${edge.relation}`, source);
    append(neighbourIds, source.id, target.id);
    append(neighbourIds, target.id, source.id);
    const verdict = VERDICT_BY_RELATION[edge.relation];
    const pair = `${source.id}\0${target.id}`;
    if (verdict && !verdicts.has(pair)) verdicts.set(pair, verdict);
  });
  outgoingIndex.forEach((list, key) => outgoingIndex.set(key, sortNodes(list)));
  incomingIndex.forEach((list, key) => incomingIndex.set(key, sortNodes(list)));
  const outgoing = (id: string, relation: string) => outgoingIndex.get(`${id}\0${relation}`) ?? [];
  const incoming = (id: string, relation: string) => incomingIndex.get(`${id}\0${relation}`) ?? [];
  const verdictOn = (evidenceId: string, hypothesisId: string): Verdict | null =>
    verdicts.get(`${evidenceId}\0${hypothesisId}`) ?? null;
  const isOpenBlocker = (node: GraphNode) => node.type === "blocker" && node.status === "open";

  const hypotheses = new Map<string, HypothesisPage>();
  nodes
    .filter((node) => node.type === "hypothesis")
    .forEach((hypothesis) => {
      const experiments = incoming(hypothesis.id, "tests")
        .filter((node) => node.type === "experiment")
        .map((experiment): ExperimentLane => {
          const evidence = outgoing(experiment.id, "produces").map((node) => ({
            node,
            verdict: verdictOn(node.id, hypothesis.id),
          }));
          return {
            node: experiment,
            active: ACTIVE_EXPERIMENT_STATUSES.has(experiment.status ?? ""),
            blocked: outgoing(experiment.id, "blocked_by").some(isOpenBlocker),
            evidence,
            tally: tallyOf(evidence),
          };
        });
      const decisions = experiments.flatMap((lane) => outgoing(lane.node.id, "governed_by"));
      const blockers = [...experiments.map((lane) => lane.node), ...decisions].flatMap((node) =>
        outgoing(node.id, "blocked_by"),
      );
      const inputNodes = uniqueNodes([...decisions, ...blockers]);
      const evidence = Object.keys(VERDICT_BY_RELATION)
        .flatMap((relation) => incoming(hypothesis.id, relation))
        .filter((node) => node.type === "evidence")
        .map((node) => ({ node, verdict: verdictOn(node.id, hypothesis.id) }));
      const uniqueEvidence = uniqueBy(evidence, (item) => item.node.id);
      hypotheses.set(hypothesis.id, {
        node: hypothesis,
        questions: incoming(hypothesis.id, "has_hypothesis").filter(
          (node) => node.type === "research_question",
        ),
        inputs: {
          nodes: inputNodes,
          openDecisions: inputNodes.filter(
            (node) => node.type === "decision" && OPEN_DECISION_STATUSES.has(node.status ?? ""),
          ).length,
          openBlockers: inputNodes.filter(isOpenBlocker).length,
        },
        experiments,
        evidence: uniqueEvidence,
        tally: tallyOf(uniqueEvidence),
      });
    });

  const hypothesisMembers = (page: HypothesisPage) => [
    page.node,
    ...page.inputs.nodes,
    ...page.experiments.flatMap((lane) => [lane.node, ...lane.evidence.map((item) => item.node)]),
    ...page.evidence.map((item) => item.node),
  ];

  const questionNodes = sortNodes(nodes.filter((node) => node.type === "research_question"));
  // An Experiment that tests no Hypothesis belongs to a question when it, or its
  // Evidence, touches one of the question's Decisions or Blockers.
  const freeExperimentsByAnchor = new Map<string, GraphNode[]>();
  sortNodes(nodes)
    .filter((node) => node.type === "experiment" && outgoing(node.id, "tests").length === 0)
    .forEach((experiment) => {
      const touched = new Set(
        [experiment.id, ...outgoing(experiment.id, "produces").map((item) => item.id)].flatMap(
          (id) => neighbourIds.get(id) ?? [],
        ),
      );
      touched.forEach((id) => append(freeExperimentsByAnchor, id, experiment));
    });
  const looseOf = (question: GraphNode): GraphNode[] => {
    const decisions = outgoing(question.id, "has_decision");
    const blockers = [question, ...decisions].flatMap((node) => outgoing(node.id, "blocked_by"));
    const anchors = [...decisions, ...blockers];
    const experiments = anchors.flatMap((node) => freeExperimentsByAnchor.get(node.id) ?? []);
    return uniqueNodes([...anchors, ...sortNodes(experiments)]);
  };

  const pages = new Map<string, QuestionPage>();
  const membersOf = (question: GraphNode, seen: Set<string>): GraphNode[] => {
    if (seen.has(question.id)) return [];
    seen.add(question.id);
    const page = pages.get(question.id)!;
    return [
      question,
      ...page.loose.flatMap((node) =>
        node.type === "experiment" ? [node, ...outgoing(node.id, "produces")] : [node],
      ),
      ...page.hypotheses.flatMap((item) => hypothesisMembers(hypotheses.get(item.node.id)!)),
      ...page.subquestions.flatMap((item) => membersOf(item.node, seen)),
    ];
  };
  const summarize = (question: GraphNode): QuestionSummary => ({
    node: question,
    rollup: { activeExperiments: 0, openBlockers: 0, memberIds: [] },
  });
  questionNodes.forEach((question) => {
    pages.set(question.id, {
      node: question,
      subquestions: outgoing(question.id, "has_subquestion")
        .filter((node) => node.type === "research_question")
        .map(summarize),
      hypotheses: outgoing(question.id, "has_hypothesis")
        .filter((node) => hypotheses.has(node.id))
        .map((node) => {
          const page = hypotheses.get(node.id)!;
          return {
            node,
            experimentCount: page.experiments.length,
            tally: page.tally,
            questionIds: page.questions.map((item) => item.id),
          };
        }),
      loose: looseOf(question),
    });
  });
  // Fill rollups once every page exists, so subquestion cards share the totals.
  const rollups = new Map(
    questionNodes.map((question) => {
      const members = uniqueNodes(membersOf(question, new Set()));
      const rollup: ResearchRollup = {
        activeExperiments: members.filter(
          (node) => node.type === "experiment" && ACTIVE_EXPERIMENT_STATUSES.has(node.status ?? ""),
        ).length,
        openBlockers: members.filter(isOpenBlocker).length,
        memberIds: members.map((node) => node.id),
      };
      return [question.id, rollup];
    }),
  );
  pages.forEach((page) =>
    page.subquestions.forEach((item) => (item.rollup = rollups.get(item.node.id)!)),
  );

  const hasParent = new Set(
    questionNodes.flatMap((question) =>
      pages.get(question.id)!.subquestions.map((item) => item.node.id),
    ),
  );
  const rootIds: string[] = questionNodes
    .filter((question) => !hasParent.has(question.id))
    .map((question) => question.id);
  // A question reachable only through a subquestion cycle still gets a card.
  const reached = new Set(rootIds.flatMap((id) => rollups.get(id)!.memberIds));
  questionNodes.forEach((question) => {
    if (reached.has(question.id)) return;
    rootIds.push(question.id);
    rollups.get(question.id)!.memberIds.forEach((id) => reached.add(id));
  });

  return {
    roots: rootIds.map((id) => ({ node: byId.get(id)!, rollup: rollups.get(id)! })),
    questions: pages,
    hypotheses,
    unplaced: sortNodes(nodes.filter((node) => !reached.has(node.id))),
  };
}

function tallyOf(evidence: WeighedEvidence[]): VerdictTally {
  const tally: VerdictTally = { for: 0, against: 0, unclear: 0 };
  evidence.forEach((item) => {
    if (item.verdict) tally[item.verdict] += 1;
  });
  return tally;
}

function uniqueBy<T>(items: T[], key: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const id = key(item);
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

function uniqueNodes(nodes: GraphNode[]): GraphNode[] {
  return uniqueBy(nodes, (node) => node.id);
}

function sortNodes(nodes: GraphNode[]): GraphNode[] {
  return [...nodes].sort(
    (left, right) =>
      (left.created_rev ?? 0) - (right.created_rev ?? 0) || left.id.localeCompare(right.id),
  );
}
