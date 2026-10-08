import { PROJECT_OVERVIEW_TOOL, PROVIDER_LOGINS_TOOL, INSPECT_NODE_TOOL } from "./overview";
import { LIST_ARTIFACTS_TOOL, OPEN_ARTIFACT_TOOL } from "./artifacts";
import {
  LIST_CONVERSATIONS_TOOL,
  INSPECT_CONVERSATION_TOOL,
  SEND_CONVERSATION_TOOL,
} from "./conversations";
import {
  INSPECT_EXPERIMENT_TOOL,
  START_EXPERIMENT_TOOL,
  AUTHORIZE_AUTO_RESEARCH_TOOL,
  STOP_EPISODE_TOOL,
} from "./experiments";
import { OPEN_VIEW_TOOL } from "./view";
import { LIST_READ_ROUTES_TOOL, READ_TOOL } from "./reads";
export { useProjectBroadReadTools } from "./reads";
import type { WebMcpToolSpec } from "./shared";

export {
  withExecute,
  WEBMCP_RESULT_MAX_CHARS,
  modelContextFromDocument,
  currentWebMcpContext,
  registerWebMcpTools,
  createWebMcpToolRegistry,
  webMcpTextResult,
} from "./shared";
export type {
  WebMcpJsonSchema,
  WebMcpToolResult,
  WebMcpToolDefinition,
  WebMcpToolSpec,
  WebMcpModelContext,
  WebMcpRegistration,
  WebMcpToolRegistry,
} from "./shared";
export {
  listProjectsForWebMcp,
  openProjectFromIndex,
  PROJECT_INDEX_TOOLS,
  projectIndexToolDefinitions,
} from "./projectIndex";
export {
  WEBMCP_NODE_CONTENT_MAX_CHARS,
  projectOverview,
  boundJsonToBudget,
  inspectProjectNode,
  webMcpSurface,
  providerLoginsForWebMcp,
  providerLoginToolDefinitions,
  projectReadToolDefinitions,
} from "./overview";
export {
  listProjectArtifacts,
  openProjectArtifact,
  projectArtifactToolDefinitions,
} from "./artifacts";
export type { ProjectArtifactRecord, WebMcpArtifactSource } from "./artifacts";
export {
  inspectProjectConversation,
  listProjectConversations,
  projectConversationToolDefinitions,
  conversationSendTarget,
  sendProjectConversationMessage,
  projectConversationSendToolDefinitions,
} from "./conversations";
export type { WebMcpConversationSource } from "./conversations";
export {
  experimentStartRefusal,
  inspectProjectExperiment,
  startProjectExperiment,
  projectExperimentToolDefinitions,
  stopProjectExperimentEpisode,
  stopProjectAutoResearchEpisode,
  episodeStopRefusal,
  projectEpisodeStopToolDefinitions,
  authorizeProjectAutoResearch,
  projectAutoResearchToolDefinitions,
} from "./experiments";
export { openProjectView, projectViewToolDefinitions } from "./view";
export type { WebMcpViewOwners, WebMcpViewSource } from "./view";
/** Every tool of the open-project surface, in catalog order. */
export const PROJECT_TOOLS: readonly WebMcpToolSpec[] = [
  PROJECT_OVERVIEW_TOOL,
  LIST_READ_ROUTES_TOOL,
  READ_TOOL,
  PROVIDER_LOGINS_TOOL,
  INSPECT_NODE_TOOL,
  OPEN_VIEW_TOOL,
  LIST_ARTIFACTS_TOOL,
  OPEN_ARTIFACT_TOOL,
  LIST_CONVERSATIONS_TOOL,
  INSPECT_CONVERSATION_TOOL,
  SEND_CONVERSATION_TOOL,
  INSPECT_EXPERIMENT_TOOL,
  START_EXPERIMENT_TOOL,
  AUTHORIZE_AUTO_RESEARCH_TOOL,
  STOP_EPISODE_TOOL,
];
