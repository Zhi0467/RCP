import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ChatsWorkspace } from "../../src/chat/ChatsWorkspace";
import { buildGlossaryIndex } from "../../src/graph/glossary";
import type { ChatConversation } from "../../src/chat/chatWorkspace";
import type { AgentTask, ChatTranscript, Episode, ProjectSnapshot } from "../../src/core/types";
import "../../src/styles.css";

declare global {
  interface Window {
    sentMessages: unknown[][];
  }
}
window.sentMessages = [];

const profile = { provider: "codex", model: "", reasoning: "medium", run_on: "local" };
const project = {
  id: "project",
  name: "Project",
  agent_profiles: { node_chat: profile, project_chat: profile },
  provider_readiness: {},
  repositories: [],
  project_truth_scope: [],
  machines: [{ alias: "local", host: null }],
} as unknown as ProjectSnapshot;
const branch = { kind: "branch" as const, branch_id: "root-episode" };
const orchestratorTurn = (
  operationId: string,
  createdAt: string,
  request: Record<string, unknown>,
  answer: string,
) =>
  ({
    operation_id: operationId,
    project_id: "project",
    kind: "auto_research",
    status: "succeeded",
    settled: true,
    finished: true,
    elapsed_seconds: 60,
    graph_target: branch,
    request: { episode_id: "root-episode", role: "orchestrator", provider: "codex", ...request },
    result: { messages: [answer] },
    provider_label: "Codex",
    created_at: createdAt,
    updated_at: "2026-10-01T00:10:00Z",
  }) as unknown as AgentTask;
const tasks = [
  orchestratorTurn(
    "root-turn",
    "2026-10-01T00:00:00Z",
    { instruction: "Trace the evidence." },
    "Root findings.",
  ),
  orchestratorTurn(
    "mail-turn",
    "2026-10-01T00:05:00Z",
    { wake_cause: "message" },
    "Control checked.",
  ),
];
const conversation: ChatConversation = {
  chatId: "root-episode",
  title: "research-oct-01",
  kind: "project_chat",
  nodeId: null,
  tasks,
  updatedAt: "2026-10-01T00:10:00Z",
  graphTarget: branch,
  conversationKind: "auto_research",
  orchestratorEpisodeId: "root-episode",
};
// The mail a wake delivered is known only to the transcript.
const chatTranscripts = new Map<string, ChatTranscript>([
  [
    "root-episode",
    {
      chat_id: "root-episode",
      kind: "project_chat",
      node_id: null,
      title: conversation.title,
      updated_at: conversation.updatedAt,
      message_count: 1,
      last_message_preview: "",
      conversation_kind: "auto_research",
      graph_target: branch,
      messages: [
        {
          message_id: "mail-turn:0",
          operation_id: "mail-turn",
          role: "user",
          text: "Check the control.",
          timestamp: "2026-10-01T00:05:00Z",
          trigger: "human",
        },
      ],
    } as unknown as ChatTranscript,
  ],
]);
// The newest chain member has ended, so a message adds turns.
const episode = {
  episode_id: "member-two",
  project_id: "project",
  mode: "auto_research",
  graph_target: branch,
  status: "completed",
  health: "needs_action",
  updated_at: "2026-10-01T00:10:00Z",
  can_message: true,
  message_requires_continuation: true,
  message_refusal: null,
} as unknown as Episode;

const otherEpisode = {
  ...episode,
  episode_id: "other-root",
  graph_target: { kind: "branch", branch_id: "other-root" },
  health: "healthy",
} as unknown as Episode;

function Fixture() {
  const [selected, setSelected] = useState(conversation.chatId);
  return (
    <main style={{ height: "100vh" }}>
      <ChatsWorkspace
        project={project}
        actorId={null}
        conversations={[conversation]}
        selectedChatId={selected}
        board={false}
        onBoardChange={() => {}}
        nodes={{}}
        // Viewed from main: a branch orchestrator opens in place, like any agent.
        graphTarget={{ kind: "main" }}
        glossaryIndex={buildGlossaryIndex({})}
        runScope={[]}
        tasks={tasks}
        watchers={[]}
        graphChangesDisabled={false}
        unreadChatIds={new Set()}
        chatTranscripts={chatTranscripts}
        onSelect={setSelected}
        onStartTask={async () => {
          throw new Error("An orchestrator card never starts a chat turn");
        }}
        onResumeTask={() => {}}
        onRetryTask={() => {}}
        onRefreshTask={async () => {
          throw new Error("No task refresh in this fixture");
        }}
        onInspectTask={() => {}}
        onOpenInbox={() => {}}
        onRepairGraphUpdate={async () => {}}
        onNewSession={() => {}}
        onRemoveDraft={() => {}}
        // Another branch's orchestrator episode comes first; the card finds its own by branch.
        orchestratorEpisodes={[otherEpisode, episode]}
        onSendOrchestratorMessage={async (...args) => {
          window.sentMessages.push(args);
        }}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
