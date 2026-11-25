import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { RunPresentation } from "../core/types";

/** A run's state badge: the published label, or the whole state line, in the published tone. */
export function TaskStateBadge({
  presentation,
  line = false,
}: {
  presentation: RunPresentation;
  line?: boolean;
}) {
  return (
    <span
      className={`${line ? "question-state" : "status-pill"} task-state ${presentation.tone}`}
      data-tone={presentation.tone}
      data-phase={presentation.phase}
      title={line ? undefined : presentation.state_line}
    >
      {line ? presentation.state_line : presentation.label}
    </span>
  );
}

/**
 * The head of every task card: icon, title, and the run's state as the server
 * published it. The full form heads a Runs card and may fold its detail; the
 * compact form is one line for the dock and the pane.
 */
export function TaskCardHeader({
  presentation,
  icon: Icon,
  title,
  href,
  compact = false,
  toggle,
  meta,
  trailing,
}: {
  /** Null before the run exists; the header then shows no state. */
  presentation: RunPresentation | null;
  icon: LucideIcon;
  /** Replaces the published title where the host holds a better one. */
  title?: string;
  /** Where the title leads, for a compact card. */
  href?: string;
  compact?: boolean;
  /** Makes the whole head fold and unfold the card's detail. */
  toggle?: { expanded: boolean; controls: string; label: string; onToggle: () => void };
  /** Facts after the state badge: time, branch, author. */
  meta?: ReactNode;
  /** Controls at the far end: a meter, archive, a menu. */
  trailing?: ReactNode;
}) {
  const shownTitle = title ?? presentation?.title ?? "";
  if (compact) {
    return (
      <div className="approval-head task-card-header" data-tone={presentation?.tone}>
        <span className="approval-icon" aria-hidden="true">
          <Icon size={16} />
        </span>
        <div className="approval-title">
          {href ? (
            <a href={href}>
              <strong>{shownTitle}</strong>
            </a>
          ) : (
            <strong>{shownTitle}</strong>
          )}
        </div>
        {presentation && <TaskStateBadge presentation={presentation} line />}
        {meta}
        {trailing}
      </div>
    );
  }
  return (
    <div className="campaign-run-heading task-card-header" data-tone={presentation?.tone}>
      {toggle && (
        <button
          className="campaign-run-toggle"
          type="button"
          aria-label={toggle.label}
          aria-expanded={toggle.expanded}
          aria-controls={toggle.controls}
          onClick={toggle.onToggle}
        />
      )}
      <span className="campaign-run-identity">
        <strong className="campaign-run-title">
          <Icon size={14} aria-hidden="true" />
          <span>{shownTitle}</span>
        </strong>
        <span className="campaign-run-meta">
          {presentation && <TaskStateBadge presentation={presentation} />}
          {meta}
        </span>
      </span>
      {trailing}
    </div>
  );
}
