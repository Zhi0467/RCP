import { useEffect, useRef, type ReactNode } from "react";
import type { AgentConfigSource } from "../core/AgentConfigControls";
import { useAgentConfigPicker } from "../core/AgentConfigPicker";
import type { AgentRunConfig } from "../core/types";

/**
 * Every agent view's one header row: the title on the left, then the chips that
 * have something to show, then the ⋯ menu.
 */
export function AgentHeader({
  leading,
  title,
  chips,
  menu,
  trailing,
  state,
  dragHandle = false,
}: {
  leading?: ReactNode;
  title: ReactNode;
  chips: ReactNode;
  menu: ReactNode;
  trailing?: ReactNode;
  state?: string;
  /** A floating window moves by its header. */
  dragHandle?: boolean;
}) {
  return (
    <header
      className="conversation-header agent-header"
      data-state={state}
      data-drag-handle={dragHandle ? "true" : undefined}
    >
      {leading}
      <div className="conversation-heading">{title}</div>
      <div className="chat-context-controls">
        {chips}
        {menu}
      </div>
      {trailing}
    </header>
  );
}

/**
 * The agent pick, one row of the header menu: `provider · model · effort`,
 * which opens the provider, model, and effort fields beneath it.
 */
export function AgentPickRow({
  source,
  value,
  effectiveModel,
  workLikeCapable,
  disabled,
  onChange,
  onShown,
}: {
  source: AgentConfigSource;
  value: AgentRunConfig;
  effectiveModel: string;
  workLikeCapable?: boolean;
  disabled: boolean;
  onChange: (value: AgentRunConfig) => void;
  /** The menu opened, so readiness is read again. */
  onShown?: () => void;
}) {
  const shown = useRef(onShown);
  useEffect(() => shown.current?.(), []);
  const picker = useAgentConfigPicker({
    project: source,
    value,
    onChange,
    label: "Chat agent",
    disabled,
    effectiveModel,
    workLike: true,
    workLikeCapable,
  });
  return (
    <div className="agent-pick-row">
      {picker.chip}
      {!disabled && picker.panel}
    </div>
  );
}
