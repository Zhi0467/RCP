import { ResourceRuntime } from "./resourceRuntime";
import { createRoot } from "react-dom/client";
import { ApprovalCardStack, dockStackCards } from "../../src/agents/ApprovalCardStack";
import { episode as base } from "./campaigns.mjs";
import "../../src/styles.css";

const ended = new Date().toISOString();
const episode = {
  ...base,
  mode: "experiment_loop",
  control_node_id: "exp/one",
  health: "stopped",
  can_stop: false,
  can_continue: true,
  task_control: null,
  ending: "human_pause",
  ended_at: ended,
  report: { report_id: "r", ending: "human_pause", created_at: ended },
  budget: { ...base.budget, invocations_remaining: 3 },
};
window.continued = [];

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <div className="approval-dock">
      <ApprovalCardStack
        stackKey="dock-run"
        cards={dockStackCards(
          [],
          [
            {
              episode,
              title: "A long Experiment title that wraps beside its state",
              link: "#/run",
              questions: [],
            },
            {
              episode: { ...base, episode_id: "episode/beta", tasks: [] },
              title: "A running Auto-research branch that asks nothing",
              link: "#/running",
              questions: [],
            },
          ],
          [],
        )}
        onDecided={() => undefined}
        onResolved={() => undefined}
        onAnswered={() => undefined}
        onChanged={async () => undefined}
      />
    </div>
  </ResourceRuntime>,
);
