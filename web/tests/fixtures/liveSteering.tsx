import { useState } from "react";
import { createRoot } from "react-dom/client";

import { NodeChat } from "../../src/components/NodeChat";
import "../../src/styles.css";

const profile = {
  provider: "codex",
  model: "",
  runtime: "app-server",
  reasoning: "medium",
  run_on: "local",
};
const project = {
  id: "project",
  name: "Project",
  agent_profiles: { project_chat: profile },
  provider_readiness: {
    local: {
      codex: {
        provider: "codex",
        label: "Codex",
        installed: true,
        authenticated: true,
        models: [],
      },
    },
  },
  repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
  project_truth_scope: ["repo"],
  state_repository: "repo",
  machines: [{ alias: "local", host: null }],
};
const initialTask = {
  operation_id: "task",
  project_id: "project",
  kind: "project_chat",
  request: {
    chat_id: "steering-chat",
    message: "Original prompt",
    mode: "discuss",
    trigger: "human",
  },
  created_at: "2026-09-05T12:00:00Z",
  updated_at: "2026-09-05T12:00:00Z",
  status: "running",
  active: true,
  attempt: 2,
  steer_visible: true,
  can_steer: true,
  steer_turn_id: "provider-turn",
  steer_unavailable_reason: null,
  elapsed_seconds: 5,
  estimate_seconds: 30,
  progress: 0.1,
  phase: "running",
  status_label: "Running",
  runtime_id: "app-server",
  runtime_label: "Codex app-server",
};

function Fixture() {
  const [task, setTask] = useState(initialTask);
  const [history, setHistory] = useState([]);
  const [generation, setGeneration] = useState(0);
  Object.assign(window, {
    setSteeringFixture: (updates: object) => setTask((current) => ({ ...current, ...updates })),
    reopenSteeringFixture: (messages: never[]) => {
      setHistory(messages);
      setGeneration((value) => value + 1);
    },
  });
  return (
    <main style={{ height: "100vh", padding: 24 }}>
      <NodeChat
        key={generation}
        project={project as never}
        runScope={["repo"]}
        tasks={[task] as never}
        historyMessages={history}
        chatId="steering-chat"
        presentation="workspace"
        onStartTask={async () => {
          throw new Error("An ordinary turn must not start");
        }}
        onInspectTask={() => undefined}
        onOpenInbox={() => undefined}
        onRepairGraphUpdate={async () => undefined}
        onNewSession={() => undefined}
        onClose={() => undefined}
        onResumeTask={() => undefined}
        onRetryTask={() => undefined}
        onRefreshTask={async () => task as never}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
