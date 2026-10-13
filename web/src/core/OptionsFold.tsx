import { ChevronDown, ChevronUp, SlidersHorizontal } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * The one folded Options control for chats and launches. The chip counts the
 * options that are on, so a grant stays visible while folded. The composer opens
 * it as a popover over the chat; a launch form opens it in place.
 */
export function OptionsFold({
  count,
  label,
  placement,
  children,
}: {
  count: number;
  /** Names the open group, such as "Turn options" or "Launch options". */
  label: string;
  placement: "popover" | "inline";
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open || placement !== "popover") return;
    const closeOutside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open, placement]);
  const Chevron = placement === "popover" ? ChevronUp : ChevronDown;
  return (
    <div className={`chat-compute-picker chat-options-picker options-fold-${placement}`} ref={ref}>
      <button
        className="chat-compute-trigger"
        type="button"
        aria-expanded={open}
        aria-label="Options"
        title="Options"
        onClick={() => setOpen((value) => !value)}
      >
        <SlidersHorizontal size={14} />
        {count ? <strong>{count}</strong> : null}
        <Chevron size={12} />
      </button>
      {/* Mounted while folded, so a child that loads saved state still reports it. */}
      <fieldset className="chat-options-menu" aria-label={label} hidden={!open}>
        {children}
      </fieldset>
    </div>
  );
}
