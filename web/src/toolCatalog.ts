// One host-independent list of every RCP page tool. WebMCP registers the subset
// the current page can run; a voice session sends the whole list to its model and
// resolves each call against the same definitions App publishes for the page.
// Voice-only tools are published for voice but never registered with WebMCP.

import {
  PROJECT_INDEX_TOOLS,
  PROJECT_TOOLS,
  type WebMcpJsonSchema,
  type WebMcpToolDefinition,
} from "./webmcp";
import { VOICE_TERMINAL_TOOLS } from "./voiceTerminal";

export type CatalogTool = {
  name: string;
  description: string;
  inputSchema: WebMcpJsonSchema;
  /** True when this exact call waits for the member's tap in a voice session. */
  confirm: (args: Record<string, unknown>) => boolean;
  /** True when a voice session shows the card even while running without confirming. */
  alwaysConfirm: boolean;
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

const SPECS = [...PROJECT_INDEX_TOOLS, ...PROJECT_TOOLS, ...VOICE_TERMINAL_TOOLS];

const CATALOG: readonly CatalogTool[] = SPECS.map(
  ({ name, description, inputSchema, confirm, alwaysConfirm }) => ({
    name,
    description,
    inputSchema,
    confirm,
    alwaysConfirm: alwaysConfirm === true,
  }),
);

const VOICE_ONLY = new Set(SPECS.filter((spec) => spec.voiceOnly).map((spec) => spec.name));

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

/** The published definitions a WebMCP host may receive: voice-only tools stay out. */
export function webMcpHostDefinitions(definitions: WebMcpToolDefinition[]): WebMcpToolDefinition[] {
  return definitions.filter((definition) => !VOICE_ONLY.has(definition.name));
}

let surface: {
  kind: ToolSurfaceKind;
  definitions: WebMcpToolDefinition[];
  refusals: Partial<Record<string, string | null>>;
} = { kind: null, definitions: [], refusals: {} };

/** App publishes the page's definitions, voice-only ones included, and why each missing
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
