import { graphTargetUrl, sameGraphTarget } from "../core/graphTarget";
import type {
  GraphTargetRef,
  AgentTask,
  AppView,
  ChatTranscript,
  Episode,
  ExperimentLoopIndexEntry,
  ProjectSnapshot,
} from "../core/types";
import { episodeRunHash } from "../core/notificationLinks";
import {
  NEVER_CONFIRM,
  type WebMcpToolDefinition,
  type WebMcpToolSpec,
  requiredStringInput,
  webMcpTextResult,
  withExecute,
} from "./shared";
import {
  type ProjectArtifactRecord,
  type WebMcpArtifactSource,
  openProjectArtifact,
  withExactEpisode,
} from "./artifacts";

/** The page's own navigation owners; none of them changes project or graph target. */
export type WebMcpViewOwners = {
  openNode: (nodeId: string) => void;
  openConversation: (transcript: ChatTranscript) => void;
  openRunRoute: (hash: string) => void;
  openTab: (view: AppView) => void;
  openArtifact: (record: ProjectArtifactRecord, projectId: string) => boolean | Promise<boolean>;
  /** False once the page has left this project or graph target. */
  isCurrent: (projectId: string, graphTarget: GraphTargetRef | undefined) => boolean;
};

export type WebMcpViewSource = WebMcpArtifactSource & {
  loadTranscript: (chatId: string) => Promise<ChatTranscript>;
  loadExperimentEntries: () => Promise<ExperimentLoopIndexEntry[]>;
};

const VIEW_KINDS = ["node", "conversation", "run", "artifact", "tab"];

/** Project tabs by the name the member sees, and the view each opens. */
const PROJECT_TABS: Record<string, AppView> = {
  overview: "overview",
  inbox: "attention",
  research: "scientific",
  runs: "execution",
  artifacts: "artifacts",
  terminals: "terminals",
  agents: "chats",
  settings: "settings",
};

export async function openProjectView(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  episodes: Episode[],
  input: Record<string, unknown>,
  owners: WebMcpViewOwners,
  source: WebMcpViewSource,
): Promise<Record<string, unknown>> {
  const kind = requiredStringInput(input, "kind");
  if (!VIEW_KINDS.includes(kind)) throw new Error(`kind must be one of ${VIEW_KINDS.join(", ")}.`);
  // Every owner runs only while the page still shows this project and graph target,
  // checked after the reads, so a move during an await cannot address another one.
  const assertCurrent = () => {
    if (!owners.isCurrent(project.id, project.graph_target)) {
      throw new Error("The page has left this project or graph target.");
    }
  };
  const id = requiredStringInput(input, "id");
  if (kind === "tab") {
    if (!Object.hasOwn(PROJECT_TABS, id)) {
      throw new Error(`A tab id is one of ${Object.keys(PROJECT_TABS).join(", ")}.`);
    }
    assertCurrent();
    owners.openTab(PROJECT_TABS[id]);
    return { project_id: project.id, kind, id, opened: true };
  }
  if (kind === "node") {
    if (!project.graph.nodes[id]) {
      throw new Error(`Node ${id} is not present in the current project graph.`);
    }
    assertCurrent();
    owners.openNode(id);
  } else if (kind === "conversation") {
    const transcript = await source.loadTranscript(id);
    if (transcript.chat_id !== id) {
      throw new Error(`Conversation ${id} returned a mismatched transcript.`);
    }
    assertCurrent();
    owners.openConversation(transcript);
  } else if (kind === "run") {
    const episode = (await withExactEpisode(episodes, id, source)).find(
      (candidate) => candidate.episode_id === id,
    );
    if (!episode) throw new Error(`Episode ${id} is not present in the current project.`);
    const entries = episode.mode === "auto_research" ? [] : await source.loadExperimentEntries();
    // An Experiment run route names its own graph target; never pair it with another one.
    const entry = entries.find((item) => item.episode?.episode_id === id);
    // Without its board entry the route falls back to the generic Runs tab, which is
    // not the run that was asked for.
    if (episode.mode !== "auto_research" && !entry) {
      throw new Error(`Episode ${id} has no exact run view; open the Runs tab instead.`);
    }
    if (entry && !sameGraphTarget(entry.graph_target, project.graph_target)) {
      throw new Error(`Episode ${id} runs on another graph target; open that graph first.`);
    }
    assertCurrent();
    owners.openRunRoute(
      graphTargetUrl(episodeRunHash(project.id, id, episode, entries), project.graph_target),
    );
  } else {
    await openProjectArtifact(
      project,
      tasks,
      episodes,
      { viewer_id: id },
      (record, projectId) => {
        assertCurrent();
        return owners.openArtifact(record, projectId);
      },
      source,
    );
  }
  return { project_id: project.id, kind, id, opened: true };
}

export const OPEN_VIEW_TOOL: WebMcpToolSpec = {
  name: "rcp_open_view",
  description:
    "Show one exact node, conversation, run, or artifact of the open project in this page, or one of its tabs, such as Settings. It never changes project or graph branch.",
  inputSchema: {
    type: "object",
    properties: {
      kind: {
        type: "string",
        enum: VIEW_KINDS,
        description: "What to show.",
      },
      id: {
        type: "string",
        minLength: 1,
        description: `Exact node id, chat_id, episode id, or artifact viewer_id; for a tab, one of ${Object.keys(PROJECT_TABS).join(", ")}.`,
      },
    },
    required: ["kind", "id"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export function projectViewToolDefinitions(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  episodes: Episode[],
  owners: WebMcpViewOwners,
  source: WebMcpViewSource,
): WebMcpToolDefinition[] {
  return [
    withExecute(OPEN_VIEW_TOOL, async (toolInput) =>
      webMcpTextResult(await openProjectView(project, tasks, episodes, toolInput, owners, source)),
    ),
  ];
}
