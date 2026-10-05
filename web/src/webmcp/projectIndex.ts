import type { ProjectCard } from "../types";
import {
  NEVER_CONFIRM,
  type WebMcpToolDefinition,
  type WebMcpToolSpec,
  compactText,
  optionalStringInput,
  requiredStringInput,
  webMcpTextResult,
  withExecute,
} from "./shared";

const WEBMCP_PROJECT_INDEX_RESULT_MAX_CHARS = 6_000;
const PROJECT_LIST_LIMIT = 8;

function compactProjectCard(project: ProjectCard): Record<string, unknown> {
  return {
    id: project.id,
    name: compactText(project.name, 96),
    primary_question: project.primary_question ? compactText(project.primary_question, 160) : null,
    remote: project.remote,
    reachable: project.reachable ?? null,
    revision: project.revision ?? null,
    attention_count: project.attention_count,
  };
}

export function listProjectsForWebMcp(
  projects: ProjectCard[],
  input: Record<string, unknown>,
): Record<string, unknown> {
  const query = optionalStringInput(input, "query")?.trim().toLowerCase() ?? null;
  const matches = query
    ? projects.filter((project) =>
        [project.id, project.name, project.primary_question ?? ""].some((value) =>
          value.toLowerCase().includes(query),
        ),
      )
    : projects;
  const visible = matches.slice(0, PROJECT_LIST_LIMIT);
  return {
    total: projects.length,
    matched: matches.length,
    returned: visible.length,
    truncated: matches.length > visible.length,
    projects: visible.map(compactProjectCard),
  };
}

export async function openProjectFromIndex(
  projects: ProjectCard[],
  input: Record<string, unknown>,
  openProject: (projectId: string) => boolean | void | Promise<boolean | void>,
): Promise<Record<string, unknown>> {
  const projectId = requiredStringInput(input, "project_id");
  const project = projects.find((candidate) => candidate.id === projectId);
  if (!project)
    throw new Error(`Project ${projectId} is not present in the current project index.`);
  if ((await openProject(project.id)) === false) {
    throw new Error(
      `Project ${projectId} requires the existing desktop access review before it can open.`,
    );
  }
  return {
    navigation_requested: true,
    target_view: "project",
    project: compactProjectCard(project),
  };
}

const LIST_PROJECTS_TOOL: WebMcpToolSpec = {
  name: "rcp_list_projects",
  description: "List the RCP projects available from the current project index.",
  inputSchema: {
    type: "object",
    properties: {
      query: {
        type: "string",
        minLength: 1,
        description: "Optional words from the project name or primary research question.",
      },
    },
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

const OPEN_PROJECT_TOOL: WebMcpToolSpec = {
  name: "rcp_open_project",
  description: "Open one exact listed RCP project in the current browser page.",
  inputSchema: {
    type: "object",
    properties: {
      project_id: {
        type: "string",
        minLength: 1,
        description: "Exact project id returned by rcp_list_projects.",
      },
    },
    required: ["project_id"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

/** The tools of the project index surface; every other tool needs an open project. */
export const PROJECT_INDEX_TOOLS: readonly WebMcpToolSpec[] = [
  LIST_PROJECTS_TOOL,
  OPEN_PROJECT_TOOL,
];

export function projectIndexToolDefinitions(
  currentProjects: () => ProjectCard[],
  openProject: (projectId: string) => boolean | void | Promise<boolean | void>,
): WebMcpToolDefinition[] {
  return [
    withExecute(LIST_PROJECTS_TOOL, (input) =>
      webMcpTextResult(
        listProjectsForWebMcp(currentProjects(), input),
        WEBMCP_PROJECT_INDEX_RESULT_MAX_CHARS,
      ),
    ),
    withExecute(OPEN_PROJECT_TOOL, async (input) =>
      webMcpTextResult(await openProjectFromIndex(currentProjects(), input, openProject)),
    ),
  ];
}
