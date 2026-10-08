import {
  createContext,
  createElement,
  Fragment,
  useContext,
  type ComponentProps,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math-extended";
import type { Element as HastElement } from "hast";
import type { InlineCode, Link, Parent, Root, RootContent, Strong, Text } from "mdast";
import { segmentGlossaryText, type GlossaryIndex } from "../graph/glossary";
import { MarkdownCodeBlock } from "./MarkdownCodeBlock";
import { isRepositoryFileHrefCandidate } from "./repositoryFileLinks";
import type { GraphNode } from "./types";

/** The Markdown syntax every reply is read with; embed detection parses with it too. */
export const ANSWER_SYNTAX_PLUGINS = [remarkGfm, remarkMath] as const;

const NODE_REFERENCE_CANDIDATE = /[a-z][a-z0-9]*(?:_[a-z0-9]+)*\/[a-z0-9]+(?:-[a-z0-9]+)*/g;
const NODE_REFERENCE_HREF_PREFIX = "#rcp-node=";
const NON_TEXT_CHILDREN = new Set([
  "code",
  "definition",
  "html",
  "image",
  "imageReference",
  "inlineCode",
  "link",
  "linkReference",
]);
const HTML_VOID_ELEMENTS = new Set([
  "area",
  "base",
  "br",
  "col",
  "embed",
  "hr",
  "img",
  "input",
  "link",
  "meta",
  "param",
  "source",
  "track",
  "wbr",
]);

type MarkdownLinkProps = ComponentProps<"a"> & { node?: unknown };

function isParent(node: RootContent): node is Extract<RootContent, Parent> {
  return "children" in node;
}

function isNodeReferenceBoundary(value: string, start: number, end: number): boolean {
  const before = value[start - 1];
  const after = value[end];
  return (!before || !/[A-Za-z0-9_\/-]/.test(before)) && (!after || !/[A-Za-z0-9_\/-]/.test(after));
}

function nodeReferenceHref(nodeId: string): string {
  return `${NODE_REFERENCE_HREF_PREFIX}${encodeURIComponent(nodeId)}`;
}

function nodeIdFromReferenceHref(
  href: string | undefined,
  nodeIds: ReadonlySet<string>,
): string | null {
  if (!href?.startsWith(NODE_REFERENCE_HREF_PREFIX)) return null;
  try {
    const nodeId = decodeURIComponent(href.slice(NODE_REFERENCE_HREF_PREFIX.length));
    return nodeIds.has(nodeId) ? nodeId : null;
  } catch {
    return null;
  }
}

function linkTextNode(text: Text, nodeIds: ReadonlySet<string>): RootContent[] {
  const matches: Array<{ id: string; start: number; end: number }> = [];
  const pattern = new RegExp(NODE_REFERENCE_CANDIDATE.source, "g");
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text.value))) {
    const start = match.index;
    const end = start + match[0].length;
    if (nodeIds.has(match[0]) && isNodeReferenceBoundary(text.value, start, end)) {
      matches.push({ id: match[0], start, end });
    }
  }
  if (matches.length === 0) return [text];

  const children: RootContent[] = [];
  let cursor = 0;
  for (const match of matches) {
    if (match.start > cursor)
      children.push({ type: "text", value: text.value.slice(cursor, match.start) });
    const link: Link = {
      type: "link",
      title: null,
      url: nodeReferenceHref(match.id),
      children: [{ type: "text", value: match.id }],
    };
    children.push(link);
    cursor = match.end;
  }
  if (cursor < text.value.length) children.push({ type: "text", value: text.value.slice(cursor) });
  return children;
}

function linkInlineCodeNode(code: InlineCode, nodeIds: ReadonlySet<string>): RootContent {
  if (!nodeIds.has(code.value)) return code;
  return {
    type: "link",
    title: null,
    url: nodeReferenceHref(code.value),
    children: [code],
  };
}

function transformNodeReferences(node: Parent, nodeIds: ReadonlySet<string>): void {
  const children: RootContent[] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      children.push(...linkTextNode(child, nodeIds));
    } else if (child.type === "inlineCode") {
      children.push(linkInlineCodeNode(child, nodeIds));
    } else {
      if (!NON_TEXT_CHILDREN.has(child.type) && isParent(child)) {
        transformNodeReferences(child, nodeIds);
      }
      children.push(child);
    }
  }
  node.children = children;
}

function nodeReferencePlugin(nodeIds: ReadonlySet<string>) {
  return function attachNodeReferencePlugin() {
    return function transform(tree: Root): void {
      transformNodeReferences(tree, nodeIds);
    };
  };
}

function glossaryNodes(text: Text, glossaryIndex: GlossaryIndex): RootContent[] {
  return segmentGlossaryText(text.value, glossaryIndex).map((segment) => {
    if (segment.kind === "text") return { type: "text", value: segment.text };
    const definitionNode: Strong = {
      type: "strong",
      data: {
        hName: "dfn",
        hProperties: {
          className: ["glossary-definition"],
          "data-definition": segment.plainDefinition,
          tabIndex: 0,
        },
      },
      children: [{ type: "text", value: segment.text }],
    };
    return definitionNode;
  });
}

function transformGlossaryDefinitions(node: Parent, glossaryIndex: GlossaryIndex): void {
  const children: RootContent[] = [];
  let htmlDepth = 0;
  for (const child of node.children) {
    if (child.type === "html") {
      htmlDepth = htmlDepthAfter(child.value, htmlDepth);
      children.push(child);
    } else if (htmlDepth > 0) {
      children.push(child);
    } else if (child.type === "text") {
      children.push(...glossaryNodes(child, glossaryIndex));
    } else {
      if (!NON_TEXT_CHILDREN.has(child.type) && isParent(child)) {
        transformGlossaryDefinitions(child, glossaryIndex);
      }
      children.push(child);
    }
  }
  node.children = children;
}

function htmlDepthAfter(value: string, initialDepth: number): number {
  let depth = initialDepth;
  const tags = value.matchAll(/<\s*(\/)?\s*([A-Za-z][\w:-]*)[^>]*?(\/)?\s*>/g);
  for (const tag of tags) {
    const name = tag[2]?.toLowerCase();
    if (!name || HTML_VOID_ELEMENTS.has(name)) continue;
    if (tag[1]) depth = Math.max(0, depth - 1);
    else if (!tag[3]) depth += 1;
  }
  return depth;
}

function glossaryDefinitionPlugin(glossaryIndex?: GlossaryIndex) {
  return function attachGlossaryDefinitionPlugin() {
    return function transform(tree: Root): void {
      if (glossaryIndex) transformGlossaryDefinitions(tree, glossaryIndex);
    };
  };
}

interface MarkdownLinkContextValue {
  nodeIds: ReadonlySet<string>;
  onOpenNode?: (nodeId: string) => void;
  onOpenRepositoryFileLink?: (href: string) => void;
  /** Renders an image source the reply embeds in place; null keeps the plain image. */
  renderEmbed?: (src: string, alt: string) => ReactNode | null;
}

const MarkdownLinkContext = createContext<MarkdownLinkContextValue>({ nodeIds: new Set() });

// One stable component type for every render: a component created per render
// makes React replace each link's DOM on any re-render of the answer, which
// cuts a reader's text selection that starts or ends inside a link.
function MarkdownLink({ href, children, className, node, ...props }: MarkdownLinkProps) {
  const { nodeIds, onOpenNode, onOpenRepositoryFileLink, renderEmbed } =
    useContext(MarkdownLinkContext);
  // An artifact shown in place is interactive and carries its own actions, so a
  // link around it would swallow those clicks; it renders without the link.
  if (renderEmbed && wrapsEmbed(node, renderEmbed)) return createElement(Fragment, null, children);
  const nodeId = nodeIdFromReferenceHref(href, nodeIds);
  const repositoryFile =
    !nodeId && Boolean(onOpenRepositoryFileLink) && isRepositoryFileHrefCandidate(href);
  const nextClassName = [
    className,
    nodeId ? "chat-node-reference" : null,
    repositoryFile ? "chat-repository-file-reference" : null,
  ]
    .filter(Boolean)
    .join(" ");
  const onClick =
    nodeId && onOpenNode
      ? (event: MouseEvent<HTMLAnchorElement>) => {
          if (event.defaultPrevented) return;
          event.preventDefault();
          onOpenNode(nodeId);
        }
      : repositoryFile && href && onOpenRepositoryFileLink
        ? (event: MouseEvent<HTMLAnchorElement>) => {
            if (event.defaultPrevented) return;
            event.preventDefault();
            onOpenRepositoryFileLink(href);
          }
        : undefined;
  const onKeyDown =
    nodeId && onOpenNode
      ? (event: ReactKeyboardEvent<HTMLAnchorElement>) => {
          if (event.defaultPrevented || event.key !== "Enter") return;
          event.preventDefault();
          onOpenNode(nodeId);
        }
      : undefined;
  return createElement(
    "a",
    {
      ...props,
      href,
      className: nextClassName || undefined,
      "aria-label": nodeId
        ? `Open node ${nodeId}`
        : repositoryFile
          ? "Open repository file preview"
          : props["aria-label"],
      onClick,
      onKeyDown,
    },
    children,
  );
}

function wrapsEmbed(
  node: unknown,
  renderEmbed: NonNullable<MarkdownLinkContextValue["renderEmbed"]>,
): boolean {
  const element = node as HastElement | undefined;
  if (!element || !Array.isArray(element.children)) return false;
  return element.children.some((child) => {
    if (child.type !== "element") return false;
    const src = child.properties?.src;
    if (child.tagName === "img" && typeof src === "string") {
      const alt = child.properties?.alt;
      return renderEmbed(src, typeof alt === "string" ? alt : "") !== null;
    }
    return wrapsEmbed(child, renderEmbed);
  });
}

type MarkdownImageProps = ComponentProps<"img"> & { node?: unknown };

// An image whose source is an artifact the turn wrote renders that artifact in place.
function MarkdownImage({ node: _node, ...props }: MarkdownImageProps) {
  const { renderEmbed } = useContext(MarkdownLinkContext);
  void _node;
  const embedded =
    renderEmbed && typeof props.src === "string" ? renderEmbed(props.src, props.alt ?? "") : null;
  return embedded ?? createElement("img", props);
}

const MARKDOWN_COMPONENTS: Components = {
  a: MarkdownLink,
  img: MarkdownImage,
  pre: MarkdownCodeBlock,
};

interface MarkdownAnswerProps {
  text: string;
  nodes?: Readonly<Record<string, GraphNode>>;
  onOpenNode?: (nodeId: string) => void;
  glossaryIndex?: GlossaryIndex;
  onOpenRepositoryFileLink?: (href: string) => void;
  renderEmbed?: (src: string, alt: string) => ReactNode | null;
}

export function MarkdownAnswer({
  text,
  nodes = {},
  onOpenNode,
  glossaryIndex,
  onOpenRepositoryFileLink,
  renderEmbed,
}: MarkdownAnswerProps) {
  const nodeIds = onOpenNode ? new Set(Object.keys(nodes)) : new Set<string>();
  const markdown = createElement(ReactMarkdown, {
    children: text,
    remarkPlugins: [
      ...ANSWER_SYNTAX_PLUGINS,
      nodeReferencePlugin(nodeIds),
      glossaryDefinitionPlugin(glossaryIndex),
    ],
    rehypePlugins: [[rehypeKatex, { strict: false, trust: false }]],
    components: MARKDOWN_COMPONENTS,
  });
  return createElement(
    MarkdownLinkContext.Provider,
    { value: { nodeIds, onOpenNode, onOpenRepositoryFileLink, renderEmbed } },
    markdown,
  );
}
