import type { LoopStatusRow } from "../core/types";
import { experimentBoardHref } from "./experimentBoardModel";
import { ExperimentLoopMetadata } from "./ExperimentLoopMetadata";

/** The successful start's overlap receipt is informational, never an admission gate. */
export function ExperimentStartOverlap({
  projectId,
  loops,
  onDismiss,
}: {
  projectId: string;
  loops: LoopStatusRow[];
  onDismiss: () => void;
}) {
  if (!loops.length) return null;
  return (
    <section className="experiment-run-block" aria-label="Other loops live when this run started">
      <div className="experiment-run-block-heading">
        <h3>Other loops live when this run started</h3>
        <button type="button" className="button compact" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
      <ul>
        {loops.map((loop) => (
          <li key={loop.episode_id} data-overlap-episode-id={loop.episode_id}>
            <a
              href={experimentBoardHref(projectId, {
                experiment_id: loop.node_id,
                episode_id: loop.episode_id,
                graph_target: loop.graph_target,
                parent_episode_id: loop.auto_research_parent_episode_id,
              })}
            >
              {loop.graph_target.kind === "main" ? "Main" : loop.graph_target.branch_id}
            </a>
            {" · "}
            <ExperimentLoopMetadata projectId={projectId} metadata={loop} />
          </li>
        ))}
      </ul>
    </section>
  );
}
