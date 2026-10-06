import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ChatsWorkspace } from "../../src/chat/ChatsWorkspace";
import { buildGlossaryIndex } from "../../src/graph/glossary";
import type { ChatConversation } from "../../src/chat/chatWorkspace";
import type { ChatTranscript, ProjectSnapshot } from "../../src/core/types";
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

// Unbroken tokens an agent really emits (paths, URLs, identifiers): the narrow
// layout must wrap them instead of scrolling the transcript or the page sideways.
const longToken = "outputs/model_stats/main_rollout_stats_rebuild_20260424_lcb_first_dp4";
const longAnswer = [
  `The probe extraction script expects them at \`${longToken}\`.`,
  `See https://example.com/${longToken}/bundle_schema_reference_without_breaks for the schema.`,
  `Bare identifier: ${longToken.replaceAll("/", "_")}.`,
  "",
  "```",
  `python extract_probes.py --input ${longToken} --output ${longToken}_probes`,
  "```",
  "",
  "| bundle | rollout labels |",
  "| --- | --- |",
  `| ${longToken} | ${longToken} |`,
].join("\n");
const longMessages = (chatId: string) =>
  [
    { role: "user", text: `Do you have ${longToken}?` },
    { role: "assistant", text: longAnswer },
  ].map(({ role, text }, index) => ({
    message_id: `${chatId}-${index}`,
    operation_id: `${chatId}-task`,
    role,
    text,
    timestamp: new Date().toISOString(),
    native_session_id: null,
    provider: "codex",
    model: null,
    reasoning: null,
    execution_machine: "local",
    applied_revision: null,
    mode: "discuss",
    graph_update: null,
    trigger: "human",
    attachments: [],
    active_compute_ids: [],
  }));

const conversations: ChatConversation[] = ["First chat", "Second chat"].map((title) => ({
  chatId: title,
  title,
  kind: "project_chat",
  nodeId: null,
  tasks: [],
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
      message_count: conversation.chatId === "First chat" ? 2 : 0,
      last_message_preview: "",
      messages: conversation.chatId === "First chat" ? longMessages(conversation.chatId) : [],
    },
  ]),
);

function Fixture() {
  const [selected, setSelected] = useState(conversations[0].chatId);
  return (
    <main style={{ height: "100vh" }}>
      <ChatsWorkspace
        project={project}
        conversations={conversations}
        selectedChatId={selected}
        board={false}
        onBoardChange={() => {}}
        nodes={{}}
        experimentEntries={[]}
        graphTarget={{ kind: "main" }}
        glossaryIndex={buildGlossaryIndex({})}
        runScope={[]}
        tasks={[]}
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

createRoot(document.getElementById("root")!).render(<Fixture />);
