import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AgentConfigChip,
  AgentConfigControls,
  launchProviderReady,
  profileRunConfig,
  type AgentConfigSource,
} from "./AgentConfigControls";
import type { AgentProfile, AgentRunConfig, ProjectSnapshot } from "./types";
import { useLayerDismiss } from "./useLayerDismiss";

/** The model the backend runs for a pick that names none: the profile's, on its own provider. */
export function launchEffectiveModel(
  profile: Pick<AgentProfile, "provider" | "effective_model"> | undefined,
  config: Pick<AgentRunConfig, "provider"> | undefined,
): string {
  return config && profile && config.provider === profile.provider ? profile.effective_model : "";
}

/**
 * A launch's agent: the member's explicit pick, else the seed (the agent the
 * run last used), else the profile. Only the pick is state, so an untouched
 * picker follows the profile; `reset` drops the pick.
 */
export function useLaunchConfig(
  project: (AgentConfigSource & Pick<ProjectSnapshot, "run_on">) | undefined,
  profile: AgentProfile | undefined,
  seed?: AgentRunConfig | null,
) {
  const [picked, setConfig] = useState<AgentRunConfig | null>(null);
  const reset = useCallback(() => setConfig(null), []);
  const config =
    picked ?? seed ?? (project && profile ? profileRunConfig(profile, project.run_on) : undefined);
  return {
    config,
    setConfig,
    reset,
    ready: !project || !config || launchProviderReady(project, config, profile?.work_like_capable),
    effectiveModel: launchEffectiveModel(profile, config),
  };
}

const PANEL_GAP = 6;
const PANEL_MAX_HEIGHT = 460;

/**
 * A launch's agent pick: the chip and the panel it opens, which Escape, a
 * press outside both, or the chip again closes. With `panelClassName` the panel
 * floats above its chip inside its positioned container, never over it, so the
 * chip stays reachable; without it the panel sits in the page where the host
 * places it.
 */
export function useAgentConfigPicker({
  project,
  value,
  onChange,
  label,
  disabled = false,
  locked = disabled,
  effectiveModel = "",
  workLike = false,
  workLikeCapable = true,
  panelClassName,
  options,
  optionCount = 0,
  forceOpen = false,
}: {
  /** Without a project or a pick there is nothing to choose, so neither part renders. */
  project: AgentConfigSource | undefined;
  value: AgentRunConfig | undefined;
  onChange: (value: AgentRunConfig) => void;
  label: string;
  disabled?: boolean;
  /** Shows the open panel's fields without letting them change; follows `disabled` unless set. */
  locked?: boolean;
  effectiveModel?: string;
  workLike?: boolean;
  workLikeCapable?: boolean;
  panelClassName?: string;
  /** Launch options the picker carries below the agent, such as Browser use. */
  options?: React.ReactNode;
  /** Launch options switched on, counted on the chip. */
  optionCount?: number;
  /** Keeps the panel shown while it carries something the member must see. */
  forceOpen?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const chipRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const floating = panelClassName !== undefined;
  const layer = useMemo(() => [chipRef, panelRef], []);
  useLayerDismiss(open, close, layer, chipRef, floating ? "pointerdown" : "click");
  useLayoutEffect(() => {
    if (!open || !floating) return;
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
  }, [open, floating]);
  const shown = open || forceOpen;
  if (!project || !value) return { close, chip: null, panel: null };
  return {
    close,
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
        optionCount={optionCount}
        onToggle={() => setOpen(!open)}
      />
    ),
    panel:
      shown &&
      (floating ? (
        <div className={panelClassName} ref={panelRef}>
          <AgentConfigControls
            project={project}
            value={value}
            onChange={onChange}
            effectiveModel={effectiveModel}
            workLikeCapable={workLikeCapable}
            locked={locked}
            compact
          />
          {options}
        </div>
      ) : (
        <AgentConfigControls
          ref={panelRef}
          project={project}
          value={value}
          onChange={onChange}
          effectiveModel={effectiveModel}
          workLikeCapable={workLikeCapable}
          locked={locked}
          compact
        >
          {options}
        </AgentConfigControls>
      )),
  };
}
