import { ArtifactViewer } from "../../src/artifacts/ArtifactViewer";
import { ResourceRuntime } from "./resourceRuntime";
import { createRoot } from "react-dom/client";
import { ChatsWorkspace } from "../../src/chat/ChatsWorkspace";
import { ProjectLanding } from "../../src/projects/ProjectLanding";
import { buildGlossaryIndex } from "../../src/graph/glossary";
import type { AgentThread, ProjectSnapshot } from "../../src/core/types";
import type { VoiceAgent } from "../../src/voice/useVoiceAgent";
import "../../src/styles.css";

declare global {
  interface Window {
    voiceOpened: string[];
  }
}
window.voiceOpened = [];

// The page's voice session, reduced to what a thread's mic reads and calls.
const voice = {
  phase: "idle",
  thread: null,
  open: async (thread: AgentThread) => {
    window.voiceOpened.push(thread.thread_id);
  },
  end: () => {},
} as unknown as VoiceAgent;

const identity = {
  space_id: "space",
  space_kind: new URLSearchParams(location.search).has("personal") ? "personal" : "team",
  space_name: "Team space",
  user: {
    user_id: new URLSearchParams(location.search).get("member") ?? "alex",
    display_name: "Alex",
    identity_kind: "team_member",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  },
};

function Space() {
  return (
    <ProjectLanding
      themeChoice="aqua"
      colorModeChoice="light"
      onThemeChoiceChange={() => {}}
      onColorModeChoiceChange={() => {}}
      palette="light"
      projects={[]}
      groups={[]}
      onGroupsChanged={async () => {}}
      groupFiling={null}
      onGroupFilingTaken={() => {}}
      invitations={[]}
      onAnswerInvitation={async () => {}}
      spaceRuns={[]}
      onOpen={() => {}}
      onOpenExperiment={() => {}}
      onArchiveEpisode={async () => {}}
      onCreate={() => {}}
      projectCreation={{
        requires_authenticated_member: false,
        intents: [
          {
            intent: "use_existing_checkout_personally",
            eligible: true,
            preselected: true,
            primary_action_label: "Use existing checkout",
            required_fields: ["repositories"],
            pinned_source_project_id: null,
            unavailable_reason: null,
          },
        ],
      }}
      onDelete={async () => ({ deleted: true, unstopped: [] })}
      openProjectTabs={[]}
      onActivateProjectTab={() => {}}
      onCloseProjectTab={() => {}}
      identity={identity as never}
      identityError={null}
      onRequestIdentityName={() => {}}
      onOpenSpaceSettings={() => {}}
      voice={voice}
    />
  );
}

const profile = { provider: "claude", model: "opus", reasoning: "high", run_on: "local" };
const project = {
  id: "p1",
  name: "Sparse attention",
  agent_profiles: { node_chat: profile, project_chat: profile },
  provider_readiness: {},
  repositories: [],
  project_truth_scope: [],
  machines: [],
} as unknown as ProjectSnapshot;

const moss: AgentThread = {
  thread_id: "moss",
  kind: "project",
  project_id: "p1",
  project_name: "Sparse attention",
  agent_name: "Moss",
  unread: false,
  last_activity_at: new Date().toISOString(),
  live_state: "needs_you",
  browser_requested: true,
};

function Project() {
  const team = ["Ablate head count at 16k", "Kernel profile for block size 64"].map((title) => ({
    chatId: title,
    title,
    kind: "project_chat" as const,
    nodeId: null,
    tasks: [],
    updatedAt: new Date().toISOString(),
    conversationKind: "chat" as const,
  }));
  return (
    <main style={{ height: "100vh" }}>
      <ChatsWorkspace
        project={project}
        actorId="alex"
        conversations={team}
        selectedChatId="moss"
        board={false}
        onBoardChange={() => {}}
        nodes={{}}
        graphTarget={{ kind: "main" }}
        glossaryIndex={buildGlossaryIndex({})}
        tasks={[]}
        watchers={[]}
        graphChangesDisabled={false}
        unreadChatIds={new Set()}
        chatTranscripts={new Map()}
        onSelect={() => {}}
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
        onRemoveDraft={() => {}}
        ownThread={{ thread: moss, revision: null, voice, onThreadChanged: () => {} }}
      />
    </main>
  );
}

const view = new URLSearchParams(location.search).get("view");
createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    {view === "project" ? <Project /> : <Space />}
    <ArtifactViewer />
  </ResourceRuntime>,
);
