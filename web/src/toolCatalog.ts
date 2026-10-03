// One host-independent list of every RCP page tool. WebMCP registers the subset
// the current page can run; a voice session sends the whole list to its model and
// resolves each call against the same definitions App registers with WebMCP.

import {
  PROJECT_INDEX_TOOLS,
  PROJECT_TOOLS,
  type WebMcpJsonSchema,
  type WebMcpToolDefinition,
} from "./webmcp";

export type CatalogTool = {
  name: string;
  description: string;
  inputSchema: WebMcpJsonSchema;
  /** True when this exact call waits for the member's tap in a voice session. */
  confirm: (args: Record<string, unknown>) => boolean;
};

export type FunctionTool = {
  type: "function";
  name: string;
  description: string;
  parameters: WebMcpJsonSchema;
};

export type ToolResolution =
  { ok: true; definition: WebMcpToolDefinition } | { ok: false; refusal: string };

export type ToolSurfaceKind = "project-index" | "project" | null;

const CATALOG: readonly CatalogTool[] = [...PROJECT_INDEX_TOOLS, ...PROJECT_TOOLS].map(
  ({ name, description, inputSchema, confirm }) => ({ name, description, inputSchema, confirm }),
);

export function catalog(): readonly CatalogTool[] {
  return CATALOG;
}

/** The Responses function format; `confirm` and other local metadata stay out. */
export function catalogAsFunctionTools(): FunctionTool[] {
  return CATALOG.map(({ name, description, inputSchema }) => ({
    type: "function",
    name,
    description,
    parameters: inputSchema,
  }));
}

let surface: {
  kind: ToolSurfaceKind;
  definitions: WebMcpToolDefinition[];
  refusals: Partial<Record<string, string | null>>;
} = { kind: null, definitions: [], refusals: {} };

/** App publishes the definitions it registers with WebMCP, and why each missing
 * project tool is unavailable, whenever page state changes. */
export function publishToolSurface(
  kind: ToolSurfaceKind,
  definitions: WebMcpToolDefinition[],
  refusals: Partial<Record<string, string | null>> = {},
): void {
  surface = { kind, definitions, refusals };
}

/** The executable definition built from the latest page state, or why it cannot run. */
export function resolve(name: string): ToolResolution {
  if (!CATALOG.some((tool) => tool.name === name)) {
    return { ok: false, refusal: `${name} is not an RCP tool.` };
  }
  const definition = surface.definitions.find((candidate) => candidate.name === name);
  if (definition) return { ok: true, definition };
  const indexTool = PROJECT_INDEX_TOOLS.some((tool) => tool.name === name);
  if (!surface.kind) {
    return { ok: false, refusal: "RCP is not ready: sign in, finish setup, or let it load." };
  }
  if (surface.kind === "project" && indexTool) {
    return { ok: false, refusal: "This tool works only on the project index." };
  }
  if (surface.kind === "project-index" && !indexTool) {
    return { ok: false, refusal: "Open a project first." };
  }
  return { ok: false, refusal: surface.refusals[name] ?? `${name} is not available now.` };
}
