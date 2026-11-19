import { ResourceRuntime } from "./resourceRuntime";
import { createRoot } from "react-dom/client";
import { ApprovalCardStack, dockStackCards } from "../../src/agents/ApprovalCardStack";
import { EpisodePlanCardView } from "../../src/agents/EpisodePlanCard";
import "../../src/styles.css";

const profile = { provider: "codex", model: "m", reasoning: "low", run_on: "local" };
const ready = (provider: string) => ({
  provider,
  label: provider,
  installed: true,
  authenticated: true,
  models: [],
});
const node = {
  id: "exp/one",
  type: "experiment",
  title: "Study",
  updated_rev: 1,
  invocation_ceiling: 30,
  design: "Compare",
  completion_criteria: ["Done"],
};
const snapshot = {
  run_on: "local",
  graph: { nodes: { [node.id]: node } },
  repositories: [],
  project_truth_scope: [],
  agent_profiles: { node_chat: profile, orchestrator: profile },
  provider_readiness: { local: { codex: ready("codex"), claude: ready("claude") } },
};
const questions = ["experiment_start", "auto_research"].map((kind, index) => ({
  question_id: `q${index}`,
  operation_id: "op",
  created_at: new Date().toISOString(),
  state: "pending",
  can_answer: true,
  card: {
    state: "pending",
    pinned_sha256: "pin",
    action: {
      kind,
      project_id: "p",
      node_id: node.id,
      goal: "Initial goal",
      starting_instruction: "Initial instruction",
      invocation_ceiling: 4,
    },
  },
}));
window.planDecisions = [];

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <ApprovalCardStack
      stackKey="draft-test"
      cards={dockStackCards(questions, [], [])}
      onDecided={() => undefined}
      onResolved={() => undefined}
      onAnswered={() => undefined}
      onChanged={async () => undefined}
      renderPlanCard={(question) => (
        <EpisodePlanCardView
          question={question}
          snapshot={snapshot}
          staged={() => false}
          onPlanChanged={() => undefined}
          onSettled={() => undefined}
          resolve={async (id, decision) => {
            window.planDecisions.push({ id, decision });
            return { outcome: "approved" };
          }}
        />
      )}
    />
  </ResourceRuntime>,
);
