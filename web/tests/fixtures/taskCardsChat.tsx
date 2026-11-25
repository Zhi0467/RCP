import { activateProject, ResourceRuntime } from "./resourceRuntime";
import { useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import { AgentChatView } from "../../src/chat/AgentChatView";
import { ArtifactViewer } from "../../src/artifacts/ArtifactViewer";
import { humanDraftStorageKey } from "../../src/graph/humanDraft";
import { taskProject, taskHistory, longTaskHistory } from "./taskCardsApi.mjs";
import "../../src/styles.css";

const long = new URLSearchParams(location.search).has("long");
(window as unknown as { stagedKey: string | null }).stagedKey = humanDraftStorageKey(
  "alex",
  taskProject.id,
);

function Fixture() {
  useLayoutEffect(() => activateProject(taskProject.id), []);
  return (
    <main style={{ height: "100vh" }}>
      <AgentChatView
        graphTarget={{ kind: "main" }}
        project={taskProject as never}
        actorId="alex"
        tasks={[]}
        historyMessages={(long ? longTaskHistory : taskHistory) as never}
        conversationTitle="Alice"
        chatId="chat"
        presentation="workspace"
        onStartTask={async () => {
          throw new Error("No turn in this fixture");
        }}
        onInspectTask={() => undefined}
        onOpenInbox={() => undefined}
        onRepairGraphUpdate={async () => undefined}
        onNewSession={() => undefined}
        onClose={() => undefined}
        onResumeTask={() => undefined}
        onRetryTask={() => undefined}
        onRefreshTask={async () => {
          throw new Error("No turn in this fixture");
        }}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Fixture />
    <ArtifactViewer />
  </ResourceRuntime>,
);
