import { MessageSquare } from "lucide-react";
import { ExperimentCheckout } from "./ExperimentCheckout";
import { projectViewHash } from "../core/graphTarget";
import type { AuthorizedHuman, EpisodeLoopMetadata, GraphTargetRef } from "../core/types";
import { experimentBoardHref, AUTO_RESEARCH_ROUTE_PREFIX } from "./experimentBoardModel";

export function ExperimentLoopMetadata({
  projectId,
  graphTarget,
  metadata,
  author,
}: {
  projectId: string;
  /** The episode's graph target; a chat that started it lives on the same one. */
  graphTarget: GraphTargetRef;
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
      {starter?.kind === "chat" && starter.chat_id ? (
        <span className="experiment-starter" data-starter-kind="chat">
          Started from{" "}
          <a href={projectViewHash(projectId, graphTarget, "chats", { chatId: starter.chat_id })}>
            <MessageSquare size={12} aria-hidden="true" />
            {starter.chat_name || "Chat"}
          </a>
        </span>
      ) : (
        !duplicateStarter && (
          <span className="experiment-starter" data-starter-kind={starter?.kind ?? "unknown"}>
            Started by{" "}
            {starter?.kind === "auto_research" ? (
              parentId ? (
                <a
                  href={experimentBoardHref(projectId, `${AUTO_RESEARCH_ROUTE_PREFIX}${parentId}`)}
                >
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
        )
      )}
      <ExperimentCheckout checkout={checkout} />
    </>
  );
}
