import { createRoot } from "react-dom/client";

import { NodeChat } from "../../src/components/NodeChat";
import "../../src/styles.css";

const project = {
  agent_profiles: {
    seed: { provider: "codex", model: "", reasoning: "medium", run_on: "local", permissions: {} },
    node_chat: {
      provider: "codex",
      model: "",
      reasoning: "medium",
      run_on: "local",
      permissions: {},
    },
    project_chat: {
      provider: "codex",
      model: "",
      reasoning: "medium",
      run_on: "local",
      permissions: {},
    },
  },
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
  id: "project",
  name: "Project",
};

const historyMessages = [
  {
    message_id: "assistant-answer",
    operation_id: "answer-task",
    role: "assistant",
    text: "The reported improvement needs a stronger comparison and a variance estimate.",
    timestamp: "2026-09-02T12:00:00Z",
    native_session_id: null,
    provider: "codex",
    model: null,
    reasoning: null,
    execution_machine: "local",
    applied_revision: null,
    mode: "discuss",
    graph_update: null,
    trigger: "human",
  },
];

createRoot(document.getElementById("root")!).render(
  <main style={{ height: "100vh", padding: 24 }}>
    <NodeChat
      project={project as never}
      node={null}
      runScope={["repo"]}
      tasks={[]}
      historyMessages={historyMessages as never}
      chatId="viewport-regression"
      presentation="workspace"
      onStartTask={() => Promise.resolve()}
      onInspectTask={() => undefined}
      onOpenInbox={() => undefined}
      onRepairGraphUpdate={() => Promise.resolve()}
      onNewSession={() => undefined}
      onClose={() => undefined}
      onResumeTask={() => undefined}
      onRetryTask={() => undefined}
    />
  </main>,
);
