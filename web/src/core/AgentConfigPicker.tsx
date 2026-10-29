import { useCallback, useRef, useState } from "react";
import { AgentConfigChip, AgentConfigControls } from "./AgentConfigControls";
import type { AgentRunConfig, ProjectSnapshot } from "./types";
import { useLayerDismiss } from "./useLayerDismiss";

/**
 * A launch's agent pick: the chip and the panel it opens, which Escape or a
 * press outside both closes. Each caller places the two where its layout wants.
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
