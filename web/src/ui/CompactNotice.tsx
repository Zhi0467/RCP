import { useId, useState, type ReactNode } from "react";
import { ChevronDown, TriangleAlert } from "lucide-react";

interface Props {
  /** One short line the phone shows while the notice is collapsed. */
  summary: string;
  /** The full explanation and its actions. */
  children: ReactNode;
  /** Stays visible while collapsed, such as a failed check's alert. */
  footer?: ReactNode;
}

/**
 * The body of a `.provider-login-notice`. Wider screens show the body as
 * before and never show the disclosure; a phone shows only the summary line
 * until it is expanded. The body stays in the DOM either way.
 */
export function CompactNotice({ summary, children, footer }: Props) {
  const [expanded, setExpanded] = useState(false);
  const bodyId = useId();
  return (
    <>
      <button
        className="compact-notice-toggle"
        type="button"
        aria-expanded={expanded}
        aria-controls={bodyId}
        onClick={() => setExpanded((open) => !open)}
      >
        <TriangleAlert size={14} aria-hidden="true" />
        <span>{summary}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      <div className="compact-notice-body" id={bodyId} data-expanded={expanded}>
        {children}
      </div>
      {footer}
    </>
  );
}
