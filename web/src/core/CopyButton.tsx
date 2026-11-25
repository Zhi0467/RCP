import { Check, Copy } from "lucide-react";
import { useCallback, useEffect, useState, type ComponentProps, type ReactNode } from "react";
import { copyText } from "./clipboard";

export type CopyState = "idle" | "copied" | "failed";

export interface CopyControl {
  state: CopyState;
  copy: (text: string) => void;
  reset: () => void;
}

export const COPIED_RESET_MS = 1600;

/**
 * One copy attempt's state. Copied clears itself; failed stays until the next
 * attempt so its message can be read.
 */
export function useCopy(onError?: (error: unknown) => void): CopyControl {
  const [state, setState] = useState<CopyState>("idle");
  useEffect(() => {
    if (state !== "copied") return;
    const timer = window.setTimeout(() => setState("idle"), COPIED_RESET_MS);
    return () => window.clearTimeout(timer);
  }, [state]);
  const copy = useCallback(
    (text: string) => {
      void copyText(text).then(
        () => setState("copied"),
        (error: unknown) => {
          setState("failed");
          onError?.(error);
        },
      );
    },
    [onError],
  );
  const reset = useCallback(() => setState("idle"), []);
  return { state, copy, reset };
}

/** Visible text for a button whose label becomes "Copied". */
export function copiedOr(idle: string): (state: CopyState) => string {
  return (state) => (state === "copied" ? "Copied" : idle);
}

type CopyButtonProps = Omit<ComponentProps<"button">, "children" | "onClick" | "type" | "title"> & {
  text: string | (() => string);
  /** Names an icon-only button; a button with visible text may omit it. */
  label?: string;
  iconSize?: number;
  /** A site that shows its own failure message owns the state and passes it here. */
  control?: CopyControl;
  onError?: (error: unknown) => void;
  children?: ReactNode | ((state: CopyState) => ReactNode);
};

/** Every copy-to-clipboard button: one write, one state, one icon, a visible failure. */
export function CopyButton({
  text,
  label,
  iconSize = 14,
  control,
  onError,
  children,
  ...button
}: CopyButtonProps) {
  const own = useCopy(onError);
  const { state, copy } = control ?? own;
  const named = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : label;
  const content = typeof children === "function" ? children(state) : children;
  return (
    <button
      {...button}
      type="button"
      data-copy-state={state}
      aria-label={label ? named : undefined}
      title={content == null || state === "failed" ? named : undefined}
      onClick={() => copy(typeof text === "function" ? text() : text)}
    >
      {state === "copied" ? (
        <Check size={iconSize} aria-hidden="true" />
      ) : (
        <Copy size={iconSize} aria-hidden="true" />
      )}
      {content}
    </button>
  );
}

/**
 * One fix command, exactly as the backend rendered it for the viewer's own prompt,
 * with an icon-only copy. A failed copy marks the row red; the text stays selectable.
 */
export function CopyCommand({ command }: { command: string }) {
  return (
    <div className="copy-command">
      <code>{command}</code>
      <CopyButton className="icon-button" text={command} label="Copy command" />
    </div>
  );
}
