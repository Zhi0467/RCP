import type { Definition, Image, ImageReference, Node, Parent } from "mdast";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { ANSWER_SYNTAX_PLUGINS } from "../core/chatMarkdown";
import type { AgentArtifactDescriptor, ArtifactSelection } from "../core/types";
import { turnArtifactLocation } from "../core/repositoryFileLinks";

// Mirror ARTIFACT_INLINE_MAX_HEIGHT_PX and ARTIFACT_INLINE_INITIAL_HEIGHT_PX
// in src/rcp/limits.py.
export const INLINE_ARTIFACT_MAX_HEIGHT = 1200;
export const INLINE_ARTIFACT_INITIAL_HEIGHT = 320;
// Mirror ARTIFACT_ERROR_MAX_CHARS in src/rcp/limits.py.
export const ARTIFACT_ERROR_MAX_CHARS = 1024;

/** The ids of this turn's artifacts a reply embeds in place with Markdown image syntax.
 *
 * The reply is parsed exactly as it renders, so syntax quoted in code, inside a
 * comment, or escaped embeds nothing and keeps the artifact's card.
 */
export function inlineArtifactIds(
  text: string,
  taskId: string,
  artifacts: readonly AgentArtifactDescriptor[],
): Set<string> {
  // By identity: two files of one name from different turn folders keep apart.
  const ids = new Set<string>();
  for (const source of embedSources(text)) {
    const found = turnArtifactFor(source, taskId, (id) => (id === taskId ? artifacts : []));
    if (found?.taskId === taskId && found.artifact) ids.add(found.artifact.artifact_id);
  }
  return ids;
}

// A chat re-renders every reply on each keystroke and refresh beat, and a full
// parse per artifact card made typing in a long chat take seconds. A reply's
// text never changes, so each text is parsed once.
const EMBED_SOURCES_CACHE_SIZE = 1000;
const embedSourcesByText = new Map<string, readonly string[]>();

/** The image sources a reply renders, in order, with references resolved. */
function embedSources(text: string): readonly string[] {
  const cached = embedSourcesByText.get(text);
  if (cached) return cached;
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
  if (embedSourcesByText.size >= EMBED_SOURCES_CACHE_SIZE) {
    embedSourcesByText.delete(embedSourcesByText.keys().next().value as string);
  }
  embedSourcesByText.set(text, sources);
  return sources;
}

/** Whether a turn's artifact can render in place: anything the viewer shows. */
export function isInlineViewable(artifact: AgentArtifactDescriptor): boolean {
  return artifact.view !== "pdf" && artifact.view !== "file";
}

/** A turn file a reply cites, resolved to the turn whose descriptors own it. */
export interface TurnArtifactTarget {
  name: string;
  /** The citing turn, or the cited folder's own turn. */
  taskId: string;
  artifact: AgentArtifactDescriptor | null;
  /** The owning turn's descriptors are not loaded yet. */
  loading: boolean;
}

/** The artifact a cited `.../turns/<folder>/artifacts/<name>` path names, from any turn. */
export function turnArtifactFor(
  src: string | undefined,
  taskId: string,
  artifactsOf: (taskId: string) => readonly AgentArtifactDescriptor[] | undefined,
): TurnArtifactTarget | null {
  const location = src ? turnArtifactLocation(src) : null;
  if (!location) return null;
  const { folder, name } = location;
  const own = artifactsOf(taskId);
  if (own === undefined) return { name, taskId, artifact: null, loading: true };
  // This turn's own file, an earlier turn's file it published a new version of, or
  // one whose edit it kept as a fork because the file moved meanwhile.
  const mine = own.find(
    (candidate) =>
      (candidate.name === name && (folder === taskId || candidate.turn_folder === folder)) ||
      (candidate.forked_from?.turn_folder === folder && candidate.forked_from.name === name),
  );
  if (mine || folder === taskId) return { name, taskId, artifact: mine ?? null, loading: false };
  // Any other turn's file: a turn folder is named by its turn, whose descriptors own it.
  const theirs = artifactsOf(folder);
  return {
    name,
    taskId: folder,
    artifact:
      theirs?.find(
        (candidate) => candidate.name === name && (candidate.turn_folder ?? folder) === folder,
      ) ?? null,
    loading: theirs === undefined,
  };
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
