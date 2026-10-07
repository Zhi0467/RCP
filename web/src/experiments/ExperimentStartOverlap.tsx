import type { LoopOverlap } from "../core/types";
import { experimentBoardHref } from "./experimentBoardModel";

/** Both the preflight inventory and successful start receipt are informational. */
export function ExperimentStartOverlap({
  projectId,
  loops,
  onDismiss,
}: {
  projectId: string;
  loops: LoopOverlap;
  onDismiss?: () => void;
}) {
  if (!loops.rows.length && !loops.omitted) return null;
  const title = onDismiss ? "Other loops live when this run started" : "Other live loops";
  return (
    <section className="experiment-run-block" aria-label={title}>
      <div className="experiment-run-block-heading">
        <h3>{title}</h3>
        {onDismiss && (
          <button type="button" className="button compact" onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
      <ul>
        {loops.rows.map((loop) => (
          <li key={loop.episode_id} data-overlap-episode-id={loop.episode_id}>
            <a
              href={experimentBoardHref(projectId, {
                experiment_id: loop.node_id,
                episode_id: loop.episode_id,
                graph_target: loop.graph_target,
                parent_episode_id:
                  loop.started_by.kind === "auto_research" ? (loop.started_by.id ?? null) : null,
              })}
            >
              {loop.graph_target.kind === "main" ? "Main" : loop.graph_target.branch_id}
            </a>
            {" · "}
            <span data-starter-kind={loop.started_by.kind}>
              Started by{" "}
              {loop.started_by.kind === "auto_research"
                ? "Auto-research"
                : loop.started_by.display_name || "a member"}
            </span>
            {" · "}
            <span data-checkout-kind={loop.checkout.kind}>Checkout: {loop.checkout.kind}</span>
          </li>
        ))}
      </ul>
      {loops.omitted > 0 && (
        <p data-overlap-omitted={loops.omitted}>{loops.omitted} additional live loops</p>
      )}
    </section>
  );
}
