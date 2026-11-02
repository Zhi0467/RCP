import { Ellipsis, type LucideIcon } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { popoverRows, rovingFocusTarget } from "../core/rovingFocus";
import { popoverPlacement, type PopoverPlacement } from "./popoverPlacement";

export interface OverflowMenuItem {
  label: string;
  icon?: LucideIcon;
  disabled?: boolean;
  /** A note on hover, such as why the item's feature is unavailable. */
  title?: string;
  onSelect: () => void;
}

/** The menu's rows: its items, then the triggers of the pickers it holds, not their options. */
const MENU_ROWS = "button[role=menuitem]:not(:disabled), button[aria-expanded]:not(:disabled)";

/**
 * A "⋯" button and its menu: items, then any inline controls. `portal` renders
 * the list in the body, fixed beside its button, so a scrolling container never
 * clips it.
 */
export function OverflowMenu({
  label,
  items,
  children,
  className,
  triggerClassName = "icon-button",
  listClassName,
  portal = false,
  disabled = false,
}: {
  label: string;
  items: readonly OverflowMenuItem[];
  children?: ReactNode;
  className?: string;
  triggerClassName?: string;
  listClassName?: string;
  portal?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<PopoverPlacement | null>(null);
  // A press inside the menu's React tree, portaled lists and popovers included,
  // reaches the root's handler before the document listener sees the same event.
  const pressedInside = useRef<Event | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  // A portaled list is measured hidden first, then placed.
  const shown = open && (!portal || place !== null);
  useLayoutEffect(() => {
    if (!open || !portal) return setPlace(null);
    const position = () =>
      setPlace(popoverPlacement(trigger.current, list.current?.offsetWidth ?? 0));
    position();
    window.addEventListener("resize", position);
    document.addEventListener("scroll", position, true);
    return () => {
      window.removeEventListener("resize", position);
      document.removeEventListener("scroll", position, true);
    };
  }, [open, portal]);
  useEffect(() => {
    if (shown) popoverRows(list.current, MENU_ROWS)[0]?.focus();
  }, [shown]);
  useEffect(() => {
    if (!open) return;
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent) {
        // A nested control that handled Escape closes only its own layer.
        if (event.key !== "Escape" || event.defaultPrevented) return;
        trigger.current?.focus();
      } else if (pressedInside.current === event) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  if (items.length === 0 && !children) return null;
  const menu = (
    <div
      ref={list}
      className={`agent-row-menu-list overflow-menu-list${portal ? " is-portaled" : ""}${listClassName ? ` ${listClassName}` : ""}`}
      role="menu"
      aria-label={label}
      style={portal ? (place ?? { visibility: "hidden" }) : undefined}
      onKeyDown={(event) => {
        // A nested picker that handled the key moves within its own layer.
        if (event.defaultPrevented) return;
        const next = rovingFocusTarget(
          popoverRows(list.current, MENU_ROWS),
          event.target,
          event.key,
        );
        if (!next) return;
        event.preventDefault();
        next.focus();
      }}
    >
      {items.map(({ label: name, icon: Icon, disabled, title, onSelect }) => (
        <button
          key={name}
          type="button"
          role="menuitem"
          disabled={disabled}
          title={title}
          onClick={() => {
            setOpen(false);
            onSelect();
          }}
        >
          {Icon && <Icon size={14} aria-hidden="true" />}
          {name}
        </button>
      ))}
      {children}
    </div>
  );
  return (
    <div
      className={className}
      onMouseDown={(event) => {
        pressedInside.current = event.nativeEvent;
      }}
    >
      <button
        ref={trigger}
        type="button"
        className={triggerClassName}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
      >
        <Ellipsis size={16} />
      </button>
      {open && (portal ? createPortal(menu, document.body) : menu)}
    </div>
  );
}
