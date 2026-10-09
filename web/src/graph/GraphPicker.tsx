import { useId, type ReactNode } from "react";
import { GitBranch } from "lucide-react";
import { graphSessionKey } from "../core/graphTarget";
import type { GraphRef, GraphTargetRef } from "../core/types";
import { branchMergeStateLabel } from "../experiments/CampaignRuns";
import {
  graphPickerOptions,
  graphRefLabel,
  graphRefTarget,
  unlistedActiveBranch,
} from "./graphPickerModel";

interface GraphPickerProps {
  /** null when the ref list could not be read; the active ref stays named. */
  refs: readonly GraphRef[] | null;
  activeRef: GraphTargetRef;
  revision: number;
  showArchived: boolean;
  onShowArchivedChange: (show: boolean) => void;
  onSelect: (target: GraphTargetRef) => void;
  episodeTasksLink?: ReactNode;
}

export function GraphPicker({
  refs,
  activeRef,
  revision,
  showArchived,
  onShowArchivedChange,
  onSelect,
  episodeTasksLink,
}: GraphPickerProps) {
  const selectId = useId();
  const listed = refs ?? [];
  const options = graphPickerOptions(listed, activeRef, showArchived);
  // Without its own option the select would show Main and could not switch to it.
  const unlisted = unlistedActiveBranch(listed, activeRef);
  const activeKey = graphSessionKey("", activeRef);
  const active = options.find((ref) => graphSessionKey("", graphRefTarget(ref)) === activeKey);
  return (
    <section className="branch-graph-banner" aria-label="Graph picker">
      <span>
        <GitBranch size={16} />
        <label htmlFor={selectId}>Graph</label>
        <select
          id={selectId}
          className="button compact secondary"
          value={activeKey}
          disabled={refs === null}
          onChange={(event) => {
            const ref = options.find(
              (option) => graphSessionKey("", graphRefTarget(option)) === event.target.value,
            );
            if (ref) onSelect(graphRefTarget(ref));
          }}
        >
          {unlisted !== null && <option value={activeKey}>{unlisted.slice(0, 8)}</option>}
          {refs === null && activeRef.kind === "main" && <option value={activeKey}>Main</option>}
          {options.map((ref) => (
            <option
              key={graphSessionKey("", graphRefTarget(ref))}
              value={graphSessionKey("", graphRefTarget(ref))}
            >
              {`${graphRefLabel(ref)}${ref.archived ? " (archived)" : ""}`}
            </option>
          ))}
        </select>
        {activeRef.kind === "branch" && <span>Revision {revision}</span>}
        {active?.kind === "branch" && <span>{branchMergeStateLabel(active.merge_state)}</span>}
        {refs === null && <span className="muted">Graph list unavailable</span>}
      </span>
      <div>
        {refs !== null && (
          <label>
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(event) => onShowArchivedChange(event.target.checked)}
            />
            Show archived
          </label>
        )}
        {episodeTasksLink}
      </div>
    </section>
  );
}
