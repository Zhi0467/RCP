import { ChevronDown, Eye, GitBranch, Network } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties } from "react";

import type { TrustView } from "../core/types";

/** The two Research layouts, by their app view ids. */
export type ResearchLayout = "scientific" | "dag";

const SHOW_OPTIONS: Array<[TrustView, string]> = [
  ["working", "Working graph"],
  ["accepted", "Accepted only"],
  ["review", "Everything"],
];

interface ViewChoice {
  layout: ResearchLayout;
  trustView: TrustView;
  onLayout: (layout: ResearchLayout) => void;
  onTrustView: (trustView: TrustView) => void;
}

/**
 * Research's one view control: the drill-down or the DAG, and which graph
 * records to show. On phones it renders inline in the project menu.
 */
export function ViewPicker({ inline = false, ...choice }: ViewChoice & { inline?: boolean }) {
  // The tab strip scrolls, so the open menu is placed against the window.
  const [menuPosition, setMenuPosition] = useState<CSSProperties | null>(null);
  const open = menuPosition !== null;
  const setOpen = (next: boolean) => {
    const rect = ref.current?.getBoundingClientRect();
    setMenuPosition(
      next && rect ? { top: rect.bottom + 6, right: window.innerWidth - rect.right } : null,
    );
  };
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setMenuPosition(null);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuPosition(null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (inline) {
    return (
      <div className="view-picker inline">
        <ViewOptions {...choice} />
      </div>
    );
  }
  const close =
    <T,>(apply: (value: T) => void) =>
    (value: T) => {
      apply(value);
      setOpen(false);
    };
  return (
    <div className="view-picker" ref={ref}>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Eye size={14} />
        <span>View</span>
        <ChevronDown size={13} />
      </button>
      {open && (
        <div
          className="view-picker-menu"
          role="group"
          aria-label="View options"
          style={menuPosition ?? undefined}
        >
          <ViewOptions
            {...choice}
            onLayout={close(choice.onLayout)}
            onTrustView={close(choice.onTrustView)}
          />
        </div>
      )}
    </div>
  );
}

function ViewOptions({ layout, trustView, onLayout, onTrustView }: ViewChoice) {
  return (
    <>
      <div className="segmented" role="group" aria-label="Research layout">
        <button
          type="button"
          aria-pressed={layout === "scientific"}
          onClick={() => onLayout("scientific")}
        >
          <GitBranch size={14} /> Research
        </button>
        <button type="button" aria-pressed={layout === "dag"} onClick={() => onLayout("dag")}>
          <Network size={14} /> DAG
        </button>
      </div>
      <div className="view-picker-row">
        <span>Show</span>
        <div className="segmented" role="group" aria-label="Graph filter">
          {SHOW_OPTIONS.map(([value, label]) => (
            <button
              type="button"
              aria-pressed={trustView === value}
              onClick={() => onTrustView(value)}
              key={value}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
    </>
  );
}
