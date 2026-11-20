import { ResourceRuntime, store } from "./resourceRuntime";
import { useState } from "react";
import { createRoot } from "react-dom/client";

import { api } from "../../src/core/api";
import { AgentChatView } from "../../src/chat/AgentChatView";
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
  agent_profiles: { project_chat: profile, node_chat: profile },
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
const nodeChat = new URLSearchParams(location.search).has("node");
const initialTask = {
  operation_id: "task",
  project_id: "project",
  kind: nodeChat ? "node_chat" : "project_chat",
  request: {
    chat_id: "steering-chat",
    ...(nodeChat ? { node_id: "test-node" } : {}),
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
  can_pause: true,
  current_chat_session_id: "native-session",
  steer_action_label: "Steer running turn",
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
    refreshQuestions: (projectId: string, version: string) =>
      store.observe(projectId, [{ name: "questions", version }]),
    setSteeringFixture: (updates: object) => setTask((current) => ({ ...current, ...updates })),
    reopenSteeringFixture: (messages: never[]) => {
      setHistory(messages);
      setGeneration((value) => value + 1);
    },
  });
  return (
    <main style={{ height: "100vh", padding: 24 }}>
      <AgentChatView
        graphTarget={{ kind: "main" }}
        key={generation}
        project={project as never}
        node={nodeChat ? ({ id: "test-node", title: "Test node" } as never) : null}
        tasks={[task] as never}
        historyMessages={history}
        chatId="steering-chat"
        presentation="workspace"
        onStartTask={(kind, request, key) =>
          api(`/api/projects/project/tasks/${kind}`, {
            method: "POST",
            body: JSON.stringify(request),
            headers: { "Idempotency-Key": key! },
          })
        }
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

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Fixture />
  </ResourceRuntime>,
);
