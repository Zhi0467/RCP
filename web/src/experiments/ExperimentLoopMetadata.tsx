import type { AuthorizedHuman, EpisodeLoopMetadata } from "../core/types";
import { experimentBoardHref, AUTO_RESEARCH_ROUTE_PREFIX } from "./experimentBoardModel";

export function ExperimentLoopMetadata({
  projectId,
  metadata,
  author,
}: {
  projectId: string;
  metadata: Pick<
    EpisodeLoopMetadata,
    "started_by" | "auto_research_parent_episode_id" | "checkout"
  >;
  author?: AuthorizedHuman | null;
}) {
  const starter = metadata.started_by;
  const parentId = metadata.auto_research_parent_episode_id;
  const checkout = metadata.checkout;
  const duplicateStarter =
    starter?.kind === "human" &&
    author != null &&
    starter.human?.space_id === author.space_id &&
    starter.human?.user_id === author.user_id;
  return (
    <>
      {!duplicateStarter && (
        <span className="experiment-starter" data-starter-kind={starter?.kind ?? "unknown"}>
          Started by{" "}
          {starter?.kind === "auto_research" ? (
            parentId ? (
              <a href={experimentBoardHref(projectId, `${AUTO_RESEARCH_ROUTE_PREFIX}${parentId}`)}>
                Auto-research
              </a>
            ) : (
              "Auto-research"
            )
          ) : starter?.kind === "human" ? (
            (starter.human?.display_name ?? "Unknown member")
          ) : (
            "Unknown"
          )}
        </span>
      )}
      <span className="experiment-checkout" data-checkout-kind={checkout?.kind ?? "unknown"}>
        Checkout: {checkout?.kind ?? "unknown"}
      </span>
    </>
  );
}
