import {
  createElement,
  type ComponentProps,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { InlineCode, Link, Parent, Root, RootContent, Text } from "mdast";
import type { GraphNode } from "./types";

const NODE_REFERENCE_CANDIDATE = /[a-z][a-z0-9]*(?:_[a-z0-9]+)*\/[a-z0-9]+(?:-[a-z0-9]+)*/g;
const NODE_REFERENCE_HREF_PREFIX = "#rcp-node=";
const NON_TEXT_CHILDREN = new Set([
  "code",
  "definition",
  "html",
  "image",
  "imageReference",
  "link",
  "linkReference",
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

function markdownComponents(
  nodeIds: ReadonlySet<string>,
  onOpenNode?: (nodeId: string) => void,
): Components {
  return {
    a: ({ href, children, className, node: _node, ...props }: MarkdownLinkProps) => {
      void _node;
      const nodeId = nodeIdFromReferenceHref(href, nodeIds);
      const nextClassName = [className, nodeId ? "chat-node-reference" : null]
        .filter(Boolean)
        .join(" ");
      const onClick =
        nodeId && onOpenNode
          ? (event: MouseEvent<HTMLAnchorElement>) => {
              if (event.defaultPrevented) return;
              event.preventDefault();
              onOpenNode(nodeId);
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
          "aria-label": nodeId ? `Open node ${nodeId}` : props["aria-label"],
          onClick,
          onKeyDown,
        },
        children,
      );
    },
  };
}

interface MarkdownAnswerProps {
  text: string;
  nodes?: Readonly<Record<string, GraphNode>>;
  onOpenNode?: (nodeId: string) => void;
}

export function MarkdownAnswer({ text, nodes = {}, onOpenNode }: MarkdownAnswerProps) {
  const nodeIds = onOpenNode ? new Set(Object.keys(nodes)) : new Set<string>();
  return createElement(ReactMarkdown, {
    children: text,
    remarkPlugins: [remarkGfm, nodeReferencePlugin(nodeIds)],
    components: markdownComponents(nodeIds, onOpenNode),
  });
}
