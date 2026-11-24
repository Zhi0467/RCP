import { ResourceRuntime } from "./resourceRuntime";
import { useEffect } from "react";
import { createRoot } from "react-dom/client";

import { AgentChatView } from "../../src/chat/AgentChatView";
import { useArtifactViewerTarget } from "../../src/artifacts/artifactViewerModel";
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

function OpenedArtifacts() {
  const target = useArtifactViewerTarget();
  useEffect(() => {
    if (target?.kind !== "artifact") return;
    const fixture = window as unknown as { opened: string[] };
    fixture.opened = [...(fixture.opened ?? []), target.artifactId];
  }, [target]);
  return null;
}

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <main style={{ height: "100vh" }}>
      <AgentChatView
        graphTarget={{ kind: "main" }}
        project={project as never}
        tasks={[]}
        chatId="chat"
        presentation="workspace"
        onStartTask={async () => undefined as never}
        onInspectTask={() => undefined}
        onOpenInbox={() => undefined}
        onRepairGraphUpdate={async () => undefined}
        onNewSession={() => undefined}
        onClose={() => undefined}
        onResumeTask={() => undefined}
        onRetryTask={() => undefined}
        onRefreshTask={async () => undefined as never}
      />
    </main>
    <OpenedArtifacts />
  </ResourceRuntime>,
);
