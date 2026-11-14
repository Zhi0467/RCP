import { useState } from "react";
import type { InlineComments } from "../agentWindow/useAgentWindowArtifacts";
import { CommentComposer, type CommentOffer } from "../comments/react/CommentComposer";
import { useComments, type SendThreads } from "../comments/react/useComments";

const NO_TARGETS: readonly string[] = [];

export function useInlineArtifactComments({
  projectId,
  canPost,
  send,
}: {
  projectId: string;
  canPost: boolean;
  send: SendThreads | null;
}) {
  const [offer, setOffer] = useState<CommentOffer | null>(null);
  const comments = useComments({
    projectId,
    kind: "artifact",
    ids: NO_TARGETS,
    canPost,
    send,
    picker: Boolean(offer),
  });
  const handlers: InlineComments = {
    open: (_taskId, artifact, event) => {
      offer?.clear?.();
      setOffer({
        target: { kind: "artifact", id: artifact.artifact_id },
        anchor: !event.selection
          ? { kind: "whole", comment: "" }
          : event.selection.kind === "box"
            ? { ...event.selection, elements: event.selection.elements ?? null, comment: "" }
            : { ...event.selection, comment: "" },
        drawnVersion: event.version,
        point: { x: event.anchor.left, y: event.anchor.top },
        clear: event.clear,
      });
    },
    cancel: (clear) => setOffer((current) => (current?.clear === clear ? null : current)),
    versionChanged: (artifact) =>
      setOffer((current) => (current?.target.id === artifact.artifact_id ? null : current)),
  };
  const layer = offer && (
    <CommentComposer
      offer={offer}
      comments={comments}
      agentLabel="This chat"
      canSend={Boolean(send)}
      onClose={() => {
        offer.clear?.();
        setOffer(null);
      }}
    />
  );
  return { handlers: canPost ? handlers : null, layer };
}
