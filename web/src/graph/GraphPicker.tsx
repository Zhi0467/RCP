import { useId, type ReactNode } from "react";
import { graphSessionKey } from "../core/graphTarget";
import type { GraphRef, GraphTargetRef } from "../core/types";
import { graphPickerOptions, graphRefTarget } from "./graphPickerModel";

interface GraphPickerProps {
  refs: readonly GraphRef[];
  activeRef: GraphTargetRef;
  showArchived: boolean;
  onShowArchivedChange: (show: boolean) => void;
  onSelect: (target: GraphTargetRef) => void;
  episodeTasksLink?: ReactNode;
}

export function GraphPicker({
  refs,
  activeRef,
  showArchived,
  onShowArchivedChange,
  onSelect,
  episodeTasksLink,
}: GraphPickerProps) {
  const selectId = useId();
  const options = graphPickerOptions(refs, activeRef, showArchived);
  return (
    <section className="branch-graph-banner" aria-label="Graph picker">
      <span>
        <label htmlFor={selectId}>Graph</label>
        <select
          id={selectId}
          className="button compact secondary"
          value={graphSessionKey("", activeRef)}
          onChange={(event) => {
            const ref = options.find(
              (option) => graphSessionKey("", graphRefTarget(option)) === event.target.value,
            );
            if (ref) onSelect(graphRefTarget(ref));
          }}
        >
          {options.map((ref) => (
            <option
              key={graphSessionKey("", graphRefTarget(ref))}
              value={graphSessionKey("", graphRefTarget(ref))}
            >
              {ref.kind === "main"
                ? "Main"
                : `${ref.branch_id}${ref.archived ? " (archived)" : ""}`}
            </option>
          ))}
        </select>
      </span>
      <div>
        <label>
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(event) => onShowArchivedChange(event.target.checked)}
          />
          Show archived
        </label>
        {episodeTasksLink}
      </div>
    </section>
  );
}
