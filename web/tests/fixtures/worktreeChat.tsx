import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NodeChat } from "../../src/components/NodeChat";
import type { AgentTask, AgentTaskRequest, ProjectSnapshot } from "../../src/types";
import "../../src/styles.css";

const profile = {
  provider: "codex",
  model: "",
  reasoning: "medium",
  run_on: "local",
  permissions: {},
};
const project = {
  id: "project",
  name: "Project",
  agent_profiles: { node_chat: profile, project_chat: profile },
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
} as unknown as ProjectSnapshot;

const requests: AgentTaskRequest[] = [];
Object.assign(window, { worktreeRequests: requests });

function Fixture() {
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [chatId, setChatId] = useState("worktree-chat");
  return (
    <main style={{ height: "100vh", padding: 24 }}>
      <button onClick={() => setChatId("other-chat")}>Switch chat</button>
      <NodeChat
        project={project}
        runScope={["repo"]}
        tasks={tasks}
        chatId={chatId}
        presentation="workspace"
        onStartTask={async (kind, request) => {
          requests.push(request);
          const task = {
            operation_id: `task-${requests.length}`,
            project_id: "project",
            kind,
            request,
            status: "succeeded",
            active: false,
            settled: true,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            native_session_id: "session-one",
            result: { answers: [] },
          } as unknown as AgentTask;
          setTasks((current) => [...current, task]);
          return task;
        }}
        onInspectTask={() => undefined}
        onOpenInbox={() => undefined}
        onRepairGraphUpdate={async () => undefined}
        onNewSession={() => undefined}
        onClose={() => undefined}
        onResumeTask={() => undefined}
        onRetryTask={() => undefined}
        onRefreshTask={async () => tasks[0]}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
