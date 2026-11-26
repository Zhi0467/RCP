import { MessageSquarePlus } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import type { PublicHealth } from "../core/types";
import { FEEDBACK_MAX_CHARS, type FeedbackContext, feedbackIssueUrl } from "./feedbackModel";

async function readContext(): Promise<FeedbackContext | null> {
  try {
    const response = await fetch("/api/health", { credentials: "same-origin" });
    if (!response.ok) return null;
    const health = (await response.json()) as PublicHealth;
    return { version: health.version, build: health.build, space_kind: health.space_kind };
  } catch {
    return null;
  }
}

export function FeedbackButton({ className }: { className: string }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [context, setContext] = useState<FeedbackContext | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const ready = text.trim().length > 0;

  useEffect(() => {
    if (!open) return;
    let stopped = false;
    void readContext().then((next) => {
      if (!stopped) setContext(next);
    });
    const closeOnPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      stopped = true;
      document.removeEventListener("pointerdown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className="feedback-menu" ref={rootRef}>
      <button
        className={className}
        type="button"
        ref={triggerRef}
        aria-label="Send feedback"
        title="Send feedback"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}
      >
        <MessageSquarePlus size={16} aria-hidden="true" />
      </button>
      <section
        className="feedback-panel"
        id={panelId}
        role="dialog"
        aria-modal="false"
        aria-label="Send feedback"
        hidden={!open}
      >
        <textarea
          value={text}
          maxLength={FEEDBACK_MAX_CHARS}
          rows={5}
          placeholder="What happened, or what would help?"
          aria-label="Feedback"
          ref={(node) => {
            if (open) node?.focus();
          }}
          onChange={(event) => setText(event.target.value)}
        />
        <footer>
          <a
            className="feedback-continue"
            href={feedbackIssueUrl(text, context)}
            target="_blank"
            rel="noopener noreferrer"
            aria-disabled={!ready}
            title="Opens a prefilled GitHub issue you can review before sending"
            onClick={(event) => {
              if (!ready) {
                event.preventDefault();
                return;
              }
              setOpen(false);
            }}
          >
            Continue on GitHub
          </a>
        </footer>
      </section>
    </div>
  );
}
