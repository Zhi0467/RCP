import { ResourceRuntime } from "./resourceRuntime";
import { useState } from "react";
import { createRoot } from "react-dom/client";

import { NodeChat } from "../../src/chat/NodeChat";
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

// A long settled history, so the transcript scrolls.
const history = Array.from({ length: 40 }, (_, index) => ({
  message_id: `message-${index}`,
  operation_id: `old-${Math.floor(index / 2)}`,
  role: index % 2 ? "assistant" : "user",
  text: `${index % 2 ? "Answer" : "Question"} ${index}\n\n${"A line of text. ".repeat(20)}`,
  timestamp: new Date(Date.UTC(2026, 8, 5, 10, index)).toISOString(),
  mode: "discuss",
  trigger: "human",
  attachments: [],
}));

type StartCall = { message: string; requestId: string | undefined };

function Fixture() {
  const [tasks, setTasks] = useState<object[]>([]);
  const [failNext, setFailNext] = useState(true);
  const fixture = window as unknown as {
    startCalls: StartCall[];
    refreshChatFixture: () => void;
  };
  fixture.startCalls ??= [];
  // A poll that returns the same tasks still hands the chat a new list.
  fixture.refreshChatFixture = () => setTasks((current) => current.map((task) => ({ ...task })));
  return (
    <main style={{ height: "100vh", padding: 24 }}>
      <NodeChat
        graphTarget={{ kind: "main" }}
        project={project as never}
        runScope={["repo"]}
        tasks={tasks as never}
        historyMessages={history as never}
        chatId="send-chat"
        presentation="workspace"
        onStartTask={async (_kind, request, requestId) => {
          fixture.startCalls.push({ message: String(request.message), requestId });
          if (failNext) {
            setFailNext(false);
            throw new TypeError("Load failed");
          }
          const task = {
            operation_id: "new-turn",
            project_id: "project",
            kind: "project_chat",
            request: { ...request, trigger: "human" },
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            status: "running",
            active: true,
            attempt: 1,
            steer_visible: true,
            can_steer: false,
            steer_unavailable_reason: "No live provider process owns this attempt.",
            steer_turn_id: null,
            status_message: "Starting",
          };
          setTasks([task]);
          return task as never;
        }}
        onInspectTask={() => undefined}
        onOpenInbox={() => undefined}
        onRepairGraphUpdate={async () => undefined}
        onNewSession={() => undefined}
        onClose={() => undefined}
        onResumeTask={() => undefined}
        onRetryTask={() => undefined}
        onRefreshTask={async () => tasks[0] as never}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Fixture />
  </ResourceRuntime>,
);
