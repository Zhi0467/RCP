import { Ellipsis } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

export interface ChatHeaderAction {
  label: string;
  disabled?: boolean;
  onSelect: () => void;
}

/** The chat header's one overflow menu: chat actions, then any inline controls. */
export function ChatHeaderMenu({
  actions,
  children,
}: {
  actions: readonly ChatHeaderAction[];
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape") return;
      } else if (
        root.current?.contains(event.target as Node) ||
        // The branch picker's list is portaled out of the menu but belongs to it.
        (event.target instanceof Element && event.target.closest(".branch-picker-popover"))
      )
        return;
      setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  if (actions.length === 0 && !children) return null;
  return (
    <div className="chat-header-menu" ref={root}>
      <button
        type="button"
        className="icon-button"
        aria-label="Chat menu"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Ellipsis size={16} />
      </button>
      {open && (
        <div className="agent-row-menu-list chat-header-menu-list" role="menu">
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              role="menuitem"
              disabled={action.disabled}
              onClick={() => {
                setOpen(false);
                action.onSelect();
              }}
            >
              {action.label}
            </button>
          ))}
          {children}
        </div>
      )}
    </div>
  );
}
