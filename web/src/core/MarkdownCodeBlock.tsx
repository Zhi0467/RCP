import { useEffect, useRef, useState, type ComponentProps } from "react";
import { Check, Copy } from "lucide-react";

type MarkdownPreProps = ComponentProps<"pre"> & { node?: unknown };

/**
 * A fenced Markdown block with a copy control. Long or wide blocks cannot be
 * selected reliably on a phone, so the block's exact text is one tap away.
 * The control is icon-only so an answer-text selection never picks up a label.
 */
export function MarkdownCodeBlock({ node: _node, ...props }: MarkdownPreProps) {
  void _node;
  const pre = useRef<HTMLPreElement>(null);
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (status === "idle") return;
    const timer = window.setTimeout(() => setStatus("idle"), 1600);
    return () => window.clearTimeout(timer);
  }, [status]);
  const label =
    status === "copied" ? "Copied" : status === "failed" ? "Copy failed" : "Copy code block";
  return (
    <div className="markdown-code-block">
      <pre ref={pre} {...props} />
      <button
        type="button"
        className={`markdown-code-copy${status === "failed" ? " failed" : ""}`}
        aria-label={label}
        title={label}
        onClick={() => {
          const text = (pre.current?.textContent ?? "").replace(/\n$/, "");
          void copyText(text).then(
            () => setStatus("copied"),
            () => setStatus("failed"),
          );
        }}
      >
        {status === "copied" ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
}

/**
 * The async Clipboard API exists only in secure contexts, and a server reached
 * over plain HTTP on a private network is not one; there the selection-based
 * copy still works inside the tap's user gesture.
 */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  area.style.top = "0";
  area.style.left = "0";
  document.body.append(area);
  try {
    area.select();
    area.setSelectionRange(0, text.length);
    if (!document.execCommand("copy")) throw new Error("Copy was refused.");
  } finally {
    area.remove();
  }
}
