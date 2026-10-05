import { isBelief, isBlocker, isControlNode, isOutcome } from "../researchType";
import type { Episode, GraphNode, ProjectSnapshot, ProviderLoginAccount } from "../types";
import {
  NEVER_CONFIRM,
  type WebMcpToolDefinition,
  type WebMcpToolSpec,
  compactNode,
  compactText,
  requiredStringInput,
  webMcpTextResult,
  withExecute,
} from "./shared";
import { compactExperimentControl } from "./experiments";

const WEBMCP_NODE_RESULT_MAX_CHARS = 16_000;
const WEBMCP_OVERVIEW_RESULT_MAX_CHARS = 6_000;
export const WEBMCP_NODE_CONTENT_MAX_CHARS = 6_000;
const OVERVIEW_LIST_LIMIT = 2;
const OVERVIEW_STOPPABLE_LIMIT = 16;
const NODE_RELATION_LIMIT = 32;
const NODE_TEXT_LIMIT = 1_200;
const NODE_TEXT_LIMIT_FLOOR = 64;
const NODE_LIST_LIMIT = 16;
const NODE_LIST_LIMIT_FLOOR = 2;
const NODE_ENTRY_LIMIT = 64;
const NODE_ENTRY_LIMIT_FLOOR = 8;
const NODE_KEY_LIMIT = 96;
const NODE_TRUNCATED_PATH_LIMIT = 24;

function recentNodes(
  project: ProjectSnapshot,
  playsRole: (type: GraphNode["type"]) => boolean,
): GraphNode[] {
  return Object.values(project.graph.nodes)
    .filter((node) => playsRole(node.type))
    .sort((left, right) => right.updated_rev - left.updated_rev || left.id.localeCompare(right.id))
    .slice(0, OVERVIEW_LIST_LIMIT);
}

export function projectOverview(
  project: ProjectSnapshot,
  episodes: Episode[] = [],
): Record<string, unknown> {
  const attentionIds = [
    ...project.attention.open_blocker_ids,
    ...project.attention.decisions_awaiting_choice_ids,
    ...project.attention.pending_proposal_ids,
  ];
  return {
    project: {
      id: project.id,
      name: compactText(project.name, 96),
      revision: project.revision,
      freshness: project.snapshot_freshness,
    },
    primary_question: project.primary_question ? compactNode(project.primary_question) : null,
    counts: project.counts,
    recent: {
      hypotheses: recentNodes(project, isBelief).map(compactNode),
      experiments: recentNodes(project, isControlNode).map(compactNode),
      evidence: recentNodes(project, isOutcome).map(compactNode),
      blockers: recentNodes(project, isBlocker).map(compactNode),
    },
    suggested_node_ids: [...new Set(attentionIds)].slice(0, 6),
    // The agent configured for each role, as the Settings tab shows it.
    agents: Object.fromEntries(
      Object.entries(project.agent_profiles).map(([role, profile]) => [
        role,
        {
          provider: profile.provider,
          model: profile.effective_model || profile.model || null,
          reasoning: profile.reasoning,
          run_on: profile.run_on,
        },
      ]),
    ),
    // Experiment episodes stop through their Experiment; an Auto-research
    // episode has no node, so its id is listed here for an exact Stop.
    stoppable_auto_research_episode_ids: episodes
      .filter((episode) => episode.mode === "auto_research" && episode.can_stop)
      .map((episode) => episode.episode_id)
      .slice(0, OVERVIEW_STOPPABLE_LIMIT),
  };
}

type BoundedJson = {
  value: unknown;
  truncation: {
    text_limit: number;
    list_limit: number;
    entry_limit: number;
    path_count: number;
    paths: string[];
  } | null;
};

type JsonBounds = { textLimit: number; listLimit: number; entryLimit: number };

function boundJsonValue(
  value: unknown,
  bounds: JsonBounds,
  path: string,
  truncatedPaths: string[],
): unknown {
  if (typeof value === "string") {
    if (value.length <= bounds.textLimit) return value;
    truncatedPaths.push(path);
    return compactText(value, bounds.textLimit);
  }
  if (Array.isArray(value)) {
    if (value.length > bounds.listLimit) truncatedPaths.push(path);
    return value
      .slice(0, bounds.listLimit)
      .map((item, index) => boundJsonValue(item, bounds, `${path}[${index}]`, truncatedPaths));
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length > bounds.entryLimit) truncatedPaths.push(path);
    return Object.fromEntries(
      entries.slice(0, bounds.entryLimit).map(([key, item]) => {
        const childPath = path ? `${path}.${key}` : key;
        if (key.length > NODE_KEY_LIMIT) truncatedPaths.push(childPath);
        const boundedKey = compactText(key, NODE_KEY_LIMIT);
        return [
          boundedKey,
          boundJsonValue(item, bounds, path ? `${path}.${boundedKey}` : boundedKey, truncatedPaths),
        ];
      }),
    );
  }
  return value;
}

/** Bound one saved record to a serialized budget by shortening strings and
 * lists, halving both limits until the record fits. Every shortened path is
 * reported so the reader can tell exact content from truncated content. */
export function boundJsonToBudget(value: unknown, maxChars: number): BoundedJson {
  const bounds: JsonBounds = {
    textLimit: NODE_TEXT_LIMIT,
    listLimit: NODE_LIST_LIMIT,
    entryLimit: NODE_ENTRY_LIMIT,
  };
  for (;;) {
    const truncatedPaths: string[] = [];
    const bounded = boundJsonValue(value, bounds, "", truncatedPaths);
    const fits = JSON.stringify(bounded).length <= maxChars;
    const atFloor =
      bounds.textLimit <= NODE_TEXT_LIMIT_FLOOR &&
      bounds.listLimit <= NODE_LIST_LIMIT_FLOOR &&
      bounds.entryLimit <= NODE_ENTRY_LIMIT_FLOOR;
    if (fits || atFloor) {
      return {
        value: bounded,
        truncation: truncatedPaths.length
          ? {
              text_limit: bounds.textLimit,
              list_limit: bounds.listLimit,
              entry_limit: bounds.entryLimit,
              path_count: truncatedPaths.length,
              paths: truncatedPaths.slice(0, NODE_TRUNCATED_PATH_LIMIT),
            }
          : null,
      };
    }
    bounds.textLimit = Math.max(NODE_TEXT_LIMIT_FLOOR, Math.floor(bounds.textLimit / 2));
    bounds.listLimit = Math.max(NODE_LIST_LIMIT_FLOOR, Math.floor(bounds.listLimit / 2));
    bounds.entryLimit = Math.max(NODE_ENTRY_LIMIT_FLOOR, Math.floor(bounds.entryLimit / 2));
  }
}

export function inspectProjectNode(
  project: ProjectSnapshot,
  input: Record<string, unknown>,
): Record<string, unknown> {
  const nodeId = requiredStringInput(input, "node_id");
  const node = project.graph.nodes[nodeId];
  if (!node) throw new Error(`Node ${nodeId} is not present in the current project graph.`);
  const bounded = boundJsonToBudget(node, WEBMCP_NODE_CONTENT_MAX_CHARS);
  const control = isControlNode(node.type) ? project.experiment_control[node.id] : undefined;
  const allRelations = Object.values(project.graph.edges)
    .filter((edge) => edge.source === nodeId || edge.target === nodeId)
    .sort((left, right) => left.id.localeCompare(right.id))
    .map((edge) => ({
      edge_id: edge.id,
      source_id: edge.source,
      target_id: edge.target,
      relation: edge.relation,
      layer: edge.layer,
    }));
  const relations = allRelations.slice(0, NODE_RELATION_LIMIT);
  const allRelatedNodeIds = allRelations.map((edge) =>
    edge.source_id === nodeId ? edge.target_id : edge.source_id,
  );
  const relatedNodeIds = [...new Set(allRelatedNodeIds)];
  return {
    project_id: project.id,
    graph_revision: project.graph.revision,
    node: bounded.value,
    node_truncation: bounded.truncation,
    relation_count: allRelations.length,
    relations_truncated: allRelations.length > relations.length,
    relations,
    related_node_ids: relatedNodeIds.slice(0, NODE_RELATION_LIMIT),
    related_node_ids_truncated: relatedNodeIds.length > NODE_RELATION_LIMIT,
    ...(isControlNode(node.type)
      ? { experiment_control: control ? compactExperimentControl(control) : null }
      : {}),
  };
}

// The page shows no project or index content until the backend session is ready,
// setup is closed, and the open has finished; the WebMCP inventory follows the
// same gate and names one surface at a time.
export function webMcpSurface<P extends { id: string }>(gate: {
  backendSessionReady: boolean;
  setupOpen: boolean;
  loading: boolean;
  projectId: string | null;
  project: P | null;
}): { project: P | null; indexAvailable: boolean; key: string | null } {
  const pageReady = gate.backendSessionReady && !gate.setupOpen && !gate.loading;
  const project =
    pageReady && gate.project && gate.project.id === gate.projectId ? gate.project : null;
  const indexAvailable = pageReady && !gate.projectId;
  const key = project ? `project:${project.id}` : indexAvailable ? "project-index" : null;
  return { project, indexAvailable, key };
}

export const PROJECT_OVERVIEW_TOOL: WebMcpToolSpec = {
  name: "rcp_get_project_overview",
  description: "Read a compact map of the open RCP research project.",
  inputSchema: { type: "object", additionalProperties: false },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export const INSPECT_NODE_TOOL: WebMcpToolSpec = {
  name: "rcp_inspect_node",
  description:
    "Read one exact saved RCP graph node and its direct relations. Oversized text or lists are shortened and every shortened path is reported in node_truncation.",
  inputSchema: {
    type: "object",
    properties: {
      node_id: {
        type: "string",
        minLength: 1,
        description: "Exact current graph node id returned by an RCP read tool.",
      },
    },
    required: ["node_id"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export const PROVIDER_LOGINS_TOOL: WebMcpToolSpec = {
  name: "rcp_get_provider_logins",
  description:
    "Read whether each agent provider, such as Codex or Claude, is signed in for this RCP space, as Settings shows it. This is RCP's record, not a live check of the credential.",
  inputSchema: { type: "object", additionalProperties: false },
  annotations: { readOnlyHint: true },
  confirm: NEVER_CONFIRM,
};

/** Each provider's sign-in state as Settings shows it; credentials never appear. */
export function providerLoginsForWebMcp(accounts: ProviderLoginAccount[]): Record<string, unknown> {
  return {
    providers: accounts.map((account) => ({
      provider: account.provider,
      label: account.label,
      host: account.host || null,
      machines: account.machines,
      state: account.state,
      detail: account.detail ? compactText(account.detail, 240) : null,
    })),
  };
}

export function providerLoginToolDefinitions(
  loadLogins: () => Promise<ProviderLoginAccount[]>,
): WebMcpToolDefinition[] {
  return [
    withExecute(PROVIDER_LOGINS_TOOL, async () =>
      webMcpTextResult(providerLoginsForWebMcp(await loadLogins())),
    ),
  ];
}

export function projectReadToolDefinitions(
  project: ProjectSnapshot,
  episodes: Episode[] = [],
): WebMcpToolDefinition[] {
  return [
    withExecute(PROJECT_OVERVIEW_TOOL, () =>
      webMcpTextResult(projectOverview(project, episodes), WEBMCP_OVERVIEW_RESULT_MAX_CHARS),
    ),
    withExecute(INSPECT_NODE_TOOL, (input) =>
      webMcpTextResult(inspectProjectNode(project, input), WEBMCP_NODE_RESULT_MAX_CHARS),
    ),
  ];
}
