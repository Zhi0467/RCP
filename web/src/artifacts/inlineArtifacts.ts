import type { Definition, Image, ImageReference, Node, Parent } from "mdast";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { ANSWER_SYNTAX_PLUGINS } from "../core/chatMarkdown";
import type { AgentArtifactDescriptor, ArtifactSelection } from "../core/types";
import { turnArtifactName } from "../core/repositoryFileLinks";

// Mirror ARTIFACT_INLINE_MAX_HEIGHT_PX, ARTIFACT_INLINE_INITIAL_HEIGHT_PX, and
// ARTIFACT_INLINE_LIVE_STATE_REFRESH_MS in src/rcp/limits.py.
export const INLINE_ARTIFACT_MAX_HEIGHT = 1200;
export const INLINE_ARTIFACT_INITIAL_HEIGHT = 320;
export const INLINE_ARTIFACT_LIVE_STATE_REFRESH_MS = 10_000;
// Mirror ARTIFACT_ERROR_MAX_CHARS in src/rcp/limits.py.
export const ARTIFACT_ERROR_MAX_CHARS = 1024;

/** The artifact names a reply embeds in place with Markdown image syntax.
 *
 * The reply is parsed exactly as it renders, so syntax quoted in code, inside a
 * comment, or escaped embeds nothing and keeps the artifact's card.
 */
export function inlineArtifactNames(text: string, taskId: string): Set<string> {
  const processor = unified().use(remarkParse);
  for (const plugin of ANSWER_SYNTAX_PLUGINS) processor.use(plugin);
  const tree = processor.parse(text);
  const sources: string[] = [];
  const references: string[] = [];
  const definitions = new Map<string, string>();
  const visit = (node: Node) => {
    if (node.type === "image") sources.push((node as Image).url);
    else if (node.type === "imageReference") references.push((node as ImageReference).identifier);
    else if (node.type === "definition") {
      // CommonMark renders the first definition of a label; later ones are inert.
      const definition = node as Definition;
      if (!definitions.has(definition.identifier)) {
        definitions.set(definition.identifier, definition.url);
      }
    }
    if ("children" in node) (node as Parent).children.forEach(visit);
  };
  visit(tree);
  for (const reference of references) {
    const url = definitions.get(reference);
    if (url) sources.push(url);
  }
  const names = new Set<string>();
  for (const source of sources) {
    const name = turnArtifactName(source, taskId);
    if (name) names.add(name);
  }
  return names;
}

/** Whether a turn's artifact can render in place: anything the viewer shows. */
export function isInlineViewable(artifact: AgentArtifactDescriptor): boolean {
  return artifact.view !== "pdf" && artifact.view !== "file";
}

/** The artifact a reply's image source names, when the turn registered one. */
export function inlineArtifactFor(
  src: string | undefined,
  taskId: string,
  artifacts: readonly AgentArtifactDescriptor[] | undefined,
): { name: string; artifact: AgentArtifactDescriptor | null } | null {
  const name = src ? turnArtifactName(src, taskId) : null;
  if (!name) return null;
  return { name, artifact: artifacts?.find((candidate) => candidate.name === name) ?? null };
}

/** The shell entrance for a reply: the viewer URL in its inline presentation. */
export function inlineViewerUrl(viewerUrl: string): string {
  const url = new URL(viewerUrl, "http://rcp.invalid");
  url.searchParams.set("presentation", "inline");
  return `${url.pathname}${url.search}`;
}

export type InlineShellMessage =
  | { kind: "error"; message: string; count: number }
  | { kind: "error-clear" }
  | { kind: "size"; height: number }
  | { kind: "selection"; selection: InlineSelection | null; description: string };

export type InlineSelection =
  | Omit<Extract<ArtifactSelection, { kind: "text" }>, "comment">
  | Omit<Extract<ArtifactSelection, { kind: "box" }>, "comment">;

/** Read one message from an inline shell; anything else on the window is ignored. */
export function readInlineShellMessage(
  event: { source: unknown; origin: string; data: unknown },
  frame: unknown,
  origin: string,
): InlineShellMessage | null {
  if (!frame || event.source !== frame || event.origin !== origin) return null;
  const data = event.data as Record<string, unknown> | null;
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  if (data.kind === "rcp-artifact-error-clear")
    return Object.keys(data).length === 1 ? { kind: "error-clear" } : null;
  if (data.kind === "rcp-artifact-error") {
    if (
      Object.keys(data).length !== 3 ||
      typeof data.message !== "string" ||
      data.message.length > ARTIFACT_ERROR_MAX_CHARS ||
      typeof data.count !== "number" ||
      !Number.isSafeInteger(data.count) ||
      data.count < 1
    )
      return null;
    return { kind: "error", message: data.message, count: data.count };
  }
  if (data.version !== 1) return null;
  if (data.type === "rcp-artifact-size") {
    if (typeof data.height !== "number" || !Number.isFinite(data.height)) return null;
    return {
      kind: "size",
      height: Math.max(1, Math.min(INLINE_ARTIFACT_MAX_HEIGHT, Math.ceil(data.height))),
    };
  }
  if (data.type !== "rcp-artifact-selection" || !("selection" in data)) return null;
  const description = typeof data.description === "string" ? data.description.slice(0, 512) : "";
  if (data.selection === null) return { kind: "selection", selection: null, description: "" };
  const selection = inlineSelection(data.selection);
  return selection ? { kind: "selection", selection, description } : null;
}

const fraction = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;

function inlineSelection(raw: unknown): InlineSelection | null {
  const value = raw as Record<string, unknown> | null;
  if (!value || typeof value !== "object") return null;
  if (value.kind === "text" && typeof value.text === "string" && value.text.trim())
    return {
      kind: "text",
      text: value.text.slice(0, 4096),
      surrounding_text:
        typeof value.surrounding_text === "string" ? value.surrounding_text.slice(0, 6144) : "",
    };
  const rect = value.rect as Record<string, unknown> | undefined;
  const viewport = value.viewport as Record<string, unknown> | undefined;
  if (
    value.kind !== "box" ||
    !rect ||
    !["x", "y", "width", "height"].every((key) => fraction(rect[key])) ||
    !viewport ||
    typeof viewport.width !== "number" ||
    typeof viewport.height !== "number"
  )
    return null;
  const elements = Array.isArray(value.elements) ? value.elements.slice(0, 8) : [];
  return {
    kind: "box",
    rect: {
      x: rect.x as number,
      y: rect.y as number,
      width: rect.width as number,
      height: rect.height as number,
    },
    viewport: {
      width: Math.max(1, Math.min(32768, Math.round(viewport.width))),
      height: Math.max(1, Math.min(32768, Math.round(viewport.height))),
    },
    elements: elements.map((element: Record<string, unknown>) => {
      const region = element?.region as Record<string, unknown> | undefined;
      return {
        path: typeof element?.path === "string" ? element.path.slice(0, 512) : "body",
        label: typeof element?.label === "string" ? element.label.slice(0, 256) : "",
        text: typeof element?.text === "string" ? element.text.slice(0, 512) : "",
        ...(region && ["x", "y", "width", "height"].every((key) => fraction(region[key]))
          ? {
              region: {
                x: region.x as number,
                y: region.y as number,
                width: region.width as number,
                height: region.height as number,
              },
            }
          : {}),
      };
    }),
  };
}
