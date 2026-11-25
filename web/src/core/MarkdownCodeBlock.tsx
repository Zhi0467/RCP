import { useRef, type ComponentProps } from "react";
import { CopyButton } from "./CopyButton";

type MarkdownPreProps = ComponentProps<"pre"> & { node?: unknown };

/**
 * A fenced Markdown block with a copy control. Long or wide blocks cannot be
 * selected reliably on a phone, so the block's exact text is one tap away.
 * The control is icon-only so an answer-text selection never picks up a label.
 */
export function MarkdownCodeBlock({ node: _node, ...props }: MarkdownPreProps) {
  void _node;
  const pre = useRef<HTMLPreElement>(null);
  return (
    <div className="markdown-code-block">
      <pre ref={pre} {...props} />
      <CopyButton
        className="markdown-code-copy"
        label="Copy code block"
        iconSize={13}
        text={() => (pre.current?.textContent ?? "").replace(/\n$/, "")}
      />
    </div>
  );
}
