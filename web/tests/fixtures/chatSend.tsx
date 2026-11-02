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

// Another tab's turn, admitted with a server time after this browser's send time.
history.push({
  message_id: "other-tab",
  operation_id: "other-tab",
  role: "user",
  text: "From another tab",
  timestamp: new Date(Date.now() + 60_000).toISOString(),
  mode: "discuss",
  trigger: "human",
  attachments: [],
});

type StartCall = { message: string; requestId: string | undefined };

// `?fresh`: a chat that has never run. Its start is held, and the app's store
// shows the send as a task under the same pending id, as beginChatSend does.
const fresh = new URLSearchParams(window.location.search).has("fresh");

function Fixture() {
  const surface = new URLSearchParams(window.location.search).get("archived");
  const [archived, setArchived] = useState(Boolean(surface));
  const [tasks, setTasks] = useState<object[]>([]);
  const [failNext, setFailNext] = useState(true);
  const fixture = window as unknown as {
    startCalls: StartCall[];
    refreshChatFixture: () => void;
    failHeldStart: () => void;
  };
  fixture.startCalls ??= [];
  // A poll that returns the same tasks still hands the chat a new list.
  fixture.refreshChatFixture = () => setTasks((current) => current.map((task) => ({ ...task })));
  return (
    <main style={{ height: "100vh", padding: 24 }}>
      <NodeChat
        graphTarget={{ kind: "main" }}
        project={project as never}
        tasks={tasks as never}
        historyMessages={fresh ? undefined : (history as never)}
        chatId="send-chat"
        presentation={surface === "floating" ? "floating" : "workspace"}
        fixedConversation={surface === "run"}
        archived={archived}
        onRestore={() => setArchived(false)}
        readOnly={archived && surface === "orchestrator"}
        allowArtifactComments={true}
        readOnlyNotice={<button>Orchestrator send</button>}
        onStartTask={async (_kind, request, requestId) => {
          fixture.startCalls.push({ message: String(request.message), requestId });
          if (fresh) {
            setTasks([
              {
                operation_id: `pending-${requestId}`,
                project_id: "project",
                kind: "project_chat",
                request,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
                status_message: "Sending",
                active: true,
                queued: true,
              },
            ]);
            // A lost request keeps the store's task, as beginChatSend does.
            await new Promise((_resolve, reject) => {
              fixture.failHeldStart = () => reject(new TypeError("Load failed"));
            });
          }
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
