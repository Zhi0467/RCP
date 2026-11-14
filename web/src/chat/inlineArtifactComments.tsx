import { useMemo, useState } from "react";
import type { InlineComments } from "../agentWindow/useAgentWindowArtifacts";
import type { Point } from "../comments/box.ts";
import {
  CommentComposer,
  CommentPopover,
  type CommentOffer,
} from "../comments/react/CommentComposer";
import { CommentThreadCard } from "../comments/react/CommentThreadCard";
import { useComments, type SendThreads } from "../comments/react/useComments";

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
  const [shown, setShown] = useState<{
    threadId: string;
    point: Point | null;
    locate: () => void;
  } | null>(null);
  const [shownArtifacts, setShownArtifacts] = useState<ReadonlyMap<string, number>>(
    () => new Map(),
  );
  const artifactIds = useMemo(() => [...shownArtifacts.keys()].sort(), [shownArtifacts]);
  const count = (artifactId: string, change: number) =>
    setShownArtifacts((current) => {
      const next = new Map(current);
      const value = (next.get(artifactId) ?? 0) + change;
      if (value > 0) next.set(artifactId, value);
      else next.delete(artifactId);
      return next;
    });
  const comments = useComments({
    projectId,
    kind: "artifact",
    ids: artifactIds,
    canPost,
    send,
    picker: Boolean(offer || shown),
  });
  const handlers: InlineComments = {
    open: (_taskId, artifact, event) => {
      offer?.clear?.();
      setShown(null);
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
    watch: (artifactId) => {
      count(artifactId, 1);
      return () => count(artifactId, -1);
    },
    threads: (artifactId) => comments.threads.filter((thread) => thread.target_id === artifactId),
    selectedThread: shown?.threadId ?? null,
    pickThread: (thread, point, locate) => setShown({ threadId: thread.thread_id, point, locate }),
  };
  const shownThread =
    shown && comments.threads.find((thread) => thread.thread_id === shown.threadId);
  const layer = (
    <>
      {offer && (
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
      )}
      {shown && shownThread && (
        <CommentPopover point={shown.point} label="Comment" onClose={() => setShown(null)}>
          <CommentThreadCard
            thread={shownThread}
            comments={comments}
            agentLabel="This chat"
            wholeLabel="Whole artifact"
            selected={false}
            onLocate={shown.locate}
          />
          <div className="rcp-comments-notice" role="status">
            {comments.error}
          </div>
        </CommentPopover>
      )}
    </>
  );
  return { handlers, layer };
}
