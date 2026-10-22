import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ResourceRuntime } from "./resourceRuntime";
import { NodeChat } from "../../src/chat/NodeChat";
import { ChatBranchDialog } from "../../src/chat/ChatBranchDialog";
import { loadChatTranscript } from "../../src/chat/chatApi";
import { api } from "../../src/core/api";
import { graphTargetFromHash } from "../../src/core/graphTarget";
import type { ChatTranscript } from "../../src/core/types";
import "../../src/styles.css";

const profile = {
  provider: "codex",
  model: "",
  reasoning: "medium",
  runtime: "app-server",
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
const source = {
  chat_id: "source",
  graph_target: { kind: "main" },
  title: "Source chat",
  graph_title: "main",
  messages: [
    {
      message_id: "message",
      operation_id: "source-turn",
      role: "assistant",
      text: "A retained answer",
      timestamp: "2026-09-05T10:00:00Z",
      mode: "discuss",
      attachments: [],
    },
  ],
} as unknown as ChatTranscript;

function Fixture() {
  const [chat, setChat] = useState(source);
  // Stands in for the chat list's row menu, which opens the dialog.
  const [branching, setBranching] = useState(false);
  useEffect(() => {
    const route = () => {
      const id = new URLSearchParams(window.location.hash.split("?")[1]).get("chat");
      if (id === "source") {
        setChat(source);
        return;
      }
      if (id)
        void loadChatTranscript(
          "/api/projects/project",
          id,
          api,
          graphTargetFromHash(window.location.hash),
        ).then(setChat);
    };
    window.addEventListener("hashchange", route);
    return () => window.removeEventListener("hashchange", route);
  }, []);
  return (
    <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <NodeChat
        key={chat.chat_id}
        actorId={null}
        project={project as never}
        graphTarget={chat.graph_target}
        header={
          <>
            <strong>{chat.title}</strong>
            <button type="button" onClick={() => setBranching(true)}>
              Branch this chat
            </button>
          </>
        }
        origin={chat.origin}
        runScope={["repo"]}
        tasks={[]}
        historyMessages={chat.messages}
        chatId={chat.chat_id}
        presentation="workspace"
        onStartTask={async (_kind, request) => {
          await api("/api/start", { method: "POST", body: JSON.stringify(request) });
          throw new Error("Probe received");
        }}
        onInspectTask={() => {}}
        onOpenInbox={() => {}}
        onRepairGraphUpdate={async () => {}}
        onNewSession={() => {}}
        onClose={() => {}}
        onResumeTask={() => {}}
        onRetryTask={() => {}}
        onRefreshTask={async () => {
          throw new Error("No task");
        }}
      />
      {branching && (
        <ChatBranchDialog
          projectId="project"
          chatId={chat.chat_id}
          graphTarget={chat.graph_target}
          onClose={() => setBranching(false)}
        />
      )}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Fixture />
  </ResourceRuntime>,
);
