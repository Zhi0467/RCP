import { Check, Copy } from "lucide-react";
import { useState } from "react";

/**
 * One fix command, exactly as the backend rendered it for the viewer's own prompt,
 * with an icon-only copy. A failed copy marks the row red; the text stays selectable.
 */
export function CopyCommand({ command }: { command: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <div className="copy-command" data-copy-command={state}>
      <code>{command}</code>
      <button
        type="button"
        className="icon-button"
        aria-label={state === "copied" ? "Copied" : "Copy command"}
        title={state === "copied" ? "Copied" : "Copy command"}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(command);
            setState("copied");
          } catch {
            setState("failed");
          }
        }}
      >
        {state === "copied" ? <Check size={14} /> : <Copy size={14} />}
      </button>
    </div>
  );
}
