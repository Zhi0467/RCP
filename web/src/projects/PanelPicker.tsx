import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

const HOVER_CLOSE_DELAY_MS = 160;

/**
 * The desktop project header's panel switcher: a trigger naming the current
 * panel that drops the panel list on hover, click, or focus. Choosing a panel
 * closes it.
 */
export function PanelPicker({
  icon,
  label,
  badge,
  children,
}: {
  icon: ReactNode;
  label: string;
  badge?: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | undefined>(undefined);
  const cancelClose = () => window.clearTimeout(closeTimer.current);
  const show = () => {
    cancelClose();
    setOpen(true);
  };
  const hide = () => {
    cancelClose();
    setOpen(false);
  };
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);
  useEffect(() => cancelClose, []);
  return (
    <div
      ref={root}
      className={`panel-picker${open ? " open" : ""}`}
      onMouseEnter={cancelClose}
      onMouseLeave={() => {
        cancelClose();
        closeTimer.current = window.setTimeout(() => setOpen(false), HOVER_CLOSE_DELAY_MS);
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) hide();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !open) return;
        event.stopPropagation();
        hide();
        root.current?.querySelector<HTMLButtonElement>(".panel-picker-trigger")?.focus();
      }}
    >
      <button
        type="button"
        className="panel-picker-trigger"
        aria-expanded={open}
        aria-haspopup="true"
        aria-label={`Panel: ${label}`}
        onMouseEnter={show}
        onClick={show}
      >
        {icon}
        <span>{label}</span>
        {badge}
        <ChevronDown size={13} aria-hidden="true" />
      </button>
      <nav
        className="panel-picker-menu"
        aria-label="Project panels"
        inert={!open}
        onClick={(event) => {
          if ((event.target as HTMLElement).closest("button")) hide();
        }}
      >
        {children}
      </nav>
    </div>
  );
}
