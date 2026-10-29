import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { AgentConfigChip, AgentConfigControls } from "./AgentConfigControls";
import type { AgentRunConfig, ProjectSnapshot } from "./types";
import { useLayerDismiss } from "./useLayerDismiss";

const PANEL_GAP = 6;
const PANEL_MAX_HEIGHT = 460;

/**
 * A launch's agent pick: the chip and the panel it opens, which Escape, a
 * press outside both, or the chip again closes. The panel opens above its chip
 * inside its positioned container, never over it, so the chip stays reachable.
 */
export function useAgentConfigPicker({
  project,
  value,
  onChange,
  label,
  disabled = false,
  effectiveModel = "",
  workLike = false,
  workLikeCapable = true,
  panelClassName,
}: {
  project: ProjectSnapshot;
  value: AgentRunConfig;
  onChange: (value: AgentRunConfig) => void;
  label: string;
  disabled?: boolean;
  effectiveModel?: string;
  workLike?: boolean;
  workLikeCapable?: boolean;
  panelClassName: string;
}) {
  const [open, setOpen] = useState(false);
  const chipRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useLayerDismiss(open, close, [chipRef, panelRef], chipRef);
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const panel = panelRef.current;
      const chip = chipRef.current;
      const frame = panel?.offsetParent?.getBoundingClientRect();
      if (!panel || !chip || !frame) return;
      const top = chip.getBoundingClientRect().top;
      panel.style.bottom = `${frame.bottom - top + PANEL_GAP}px`;
      panel.style.maxHeight = `${Math.min(PANEL_MAX_HEIGHT, top - frame.top - 2 * PANEL_GAP)}px`;
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open]);
  return {
    chip: (
      <AgentConfigChip
        ref={chipRef}
        project={project}
        value={value}
        effectiveModel={effectiveModel}
        open={open}
        disabled={disabled}
        label={label}
        workLike={workLike}
        onToggle={() => setOpen(!open)}
      />
    ),
    panel: open && (
      <div className={panelClassName} ref={panelRef}>
        <AgentConfigControls
          project={project}
          value={value}
          onChange={onChange}
          effectiveModel={effectiveModel}
          workLikeCapable={workLikeCapable}
          compact
        />
      </div>
    ),
  };
}
