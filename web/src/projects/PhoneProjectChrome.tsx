import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, ChevronDown, Ellipsis, Menu, MessageCircle } from "lucide-react";

/**
 * A disclosure panel that closes on Escape, on a press outside it, and when a
 * control inside it is chosen, so each phone menu needs no close button.
 */
function usePanel() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return { open, setOpen, root };
}

/**
 * The phone project bar: back, the project name (which opens the open-project
 * dock), staged-change Sync, and every other project control behind "⋯".
 */
export function PhoneProjectBar({
  projectName,
  hasDraft,
  status,
  onBack,
  dock,
  sync,
  menu,
}: {
  projectName: string;
  hasDraft: boolean;
  status?: ReactNode;
  onBack: () => void;
  dock: ReactNode;
  sync: ReactNode;
  menu: ReactNode;
}) {
  const projects = usePanel();
  const more = usePanel();
  return (
    <header className={`phone-project-bar${hasDraft ? " has-draft" : ""}`}>
      <button className="icon-button phone-bar-back" aria-label="All projects" onClick={onBack}>
        <ArrowLeft size={18} />
      </button>
      <div className="phone-bar-project" ref={projects.root}>
        <button
          className="phone-bar-name"
          aria-expanded={projects.open}
          aria-controls="phone-project-dock"
          onClick={() => projects.setOpen(!projects.open)}
        >
          <span>{projectName}</span>
          <ChevronDown size={14} aria-hidden="true" />
        </button>
        {projects.open && (
          <div
            className="phone-bar-panel phone-project-dock"
            id="phone-project-dock"
            onClick={(event) => {
              if ((event.target as Element).closest("button")) projects.setOpen(false);
            }}
          >
            {dock}
          </div>
        )}
      </div>
      {status}
      {hasDraft && <div className="phone-bar-sync">{sync}</div>}
      <div className="phone-bar-more" ref={more.root}>
        <button
          className="icon-button"
          aria-label="Project menu"
          aria-expanded={more.open}
          aria-controls="phone-project-menu"
          onClick={() => more.setOpen(!more.open)}
        >
          <Ellipsis size={20} />
        </button>
        {/* Kept mounted so a control's own popover survives the menu closing. */}
        <div
          className="phone-bar-panel phone-project-menu"
          id="phone-project-menu"
          hidden={!more.open}
          onClick={(event) => {
            const target = event.target as Element;
            if (target.closest("button") && !target.closest(".landing-identity-menu"))
              more.setOpen(false);
          }}
        >
          {menu}
        </div>
      </div>
    </header>
  );
}

/**
 * The phone panel tabs at the bottom of the screen: the first panels directly,
 * the rest in a More sheet, which reads as active while one of its panels is.
 */
export function PhoneTabBar({
  primary,
  more,
  moreActive,
  moreBadge,
}: {
  primary: ReactNode;
  more: ReactNode;
  moreActive: boolean;
  moreBadge?: ReactNode;
}) {
  const sheet = usePanel();
  return (
    <nav className="phone-tab-bar" aria-label="Project panels" ref={sheet.root}>
      {primary}
      <button
        className={moreActive ? "active" : ""}
        aria-expanded={sheet.open}
        aria-controls="phone-more-panels"
        onClick={() => sheet.setOpen(!sheet.open)}
      >
        <Menu size={14} />
        <span>More</span>
        {moreBadge}
      </button>
      <div
        className="phone-more-sheet"
        id="phone-more-panels"
        hidden={!sheet.open}
        onClick={(event) => {
          if ((event.target as Element).closest("button")) sheet.setOpen(false);
        }}
      >
        {more}
      </div>
    </nav>
  );
}

export function PhoneAskButton({ disabled, onAsk }: { disabled: boolean; onAsk: () => void }) {
  return (
    <button
      className="phone-ask-button"
      aria-label="Ask about this project"
      disabled={disabled}
      onClick={onAsk}
    >
      <MessageCircle size={22} />
    </button>
  );
}
