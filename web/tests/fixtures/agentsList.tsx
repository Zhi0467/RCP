import { useState } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { ChatsWorkspace } from "../../src/chat/ChatsWorkspace";
import { buildGlossaryIndex } from "../../src/graph/glossary";
import type { ChatConversation } from "../../src/chat/chatWorkspace";
import type { AgentTask, ChatTranscript, ProjectSnapshot } from "../../src/core/types";
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
  repositories: [],
  project_truth_scope: [],
  machines: [{ alias: "local", host: null }],
} as unknown as ProjectSnapshot;

const turn = (chatId: string, provider: string, label: string) =>
  ({
    operation_id: `turn-${chatId}`,
    project_id: "project",
    kind: "project_chat",
    status: chatId === "Working chat" ? "running" : "succeeded",
    active: chatId === "Working chat",
    settled: chatId !== "Working chat",
    finished: chatId !== "Working chat",
    elapsed_seconds: 120,
    request: { chat_id: chatId, provider },
    provider_label: label,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  }) as unknown as AgentTask;
const conversations: ChatConversation[] = [
  ["Claude chat", "claude", "Claude"],
  ["Codex chat", "codex", "Codex"],
  ["Archived chat", "custom", "Custom agent"],
  ["Working chat", "claude", "Claude"],
].map(([title, provider, label]) => ({
  chatId: title,
  title,
  kind: "project_chat",
  nodeId: null,
  tasks: [turn(title, provider, label)],
  updatedAt: new Date().toISOString(),
}));
const chatTranscripts = new Map<string, ChatTranscript>(
  conversations.map((conversation) => [
    conversation.chatId,
    {
      chat_id: conversation.chatId,
      kind: conversation.kind,
      node_id: null,
      title: conversation.title,
      updated_at: conversation.updatedAt,
      message_count: 0,
      last_message_preview: "",
      messages: [],
    },
  ]),
);

function Fixture() {
  const [selected, setSelected] = useState(conversations[0].chatId);
  const [board, setBoard] = useState(new URLSearchParams(location.search).has("board"));
  return (
    <main style={{ height: "100vh" }}>
      <ChatsWorkspace
        project={project}
        conversations={conversations}
        selectedChatId={selected}
        board={board}
        onBoardChange={setBoard}
        nodes={{}}
        experimentEntries={[]}
        graphTarget={{ kind: "main" }}
        glossaryIndex={buildGlossaryIndex({})}
        runScope={[]}
        tasks={conversations.flatMap((conversation) => conversation.tasks)}
        watchers={[]}
        graphChangesDisabled={false}
        unreadChatIds={new Set()}
        chatTranscripts={chatTranscripts}
        hasMore={false}
        loadingMore={false}
        onSelect={setSelected}
        onLoadMore={() => {}}
        onStartTask={async () => {}}
        onResumeTask={() => {}}
        onRetryTask={() => {}}
        onRefreshTask={async () => {
          throw new Error("No task in this fixture");
        }}
        onInspectTask={() => {}}
        onOpenInbox={() => {}}
        onRepairGraphUpdate={async () => {}}
        onNewSession={() => {}}
      />
    </main>
  );
}

const root = createRoot(document.getElementById("root")!);
root.render(<Fixture />);

/** Leaves and reopens Agents, as switching project tabs does. */
export function remountAgents() {
  flushSync(() => root.render(<></>));
  root.render(<Fixture />);
}
