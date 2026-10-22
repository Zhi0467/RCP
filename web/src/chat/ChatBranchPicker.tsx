import { CornerUpLeft } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { branchChat, createGraphFork, loadGraphRefs, requireChatBranchable } from "../core/api";
import { projectViewHash, sameGraphTarget } from "../core/graphTarget";
import type { ChatOrigin, GraphRef, GraphTargetRef } from "../core/types";
import { GraphPicker } from "../graph/GraphPicker";
import { graphRefTarget } from "../graph/graphPickerModel";
import "./chatBranch.css";

/** The first transcript line of a branched chat: a link back to the chat it came from. */
export function ChatOriginLink({ projectId, origin }: { projectId: string; origin: ChatOrigin }) {
  return (
    <a
      className="chat-origin-link"
      href={projectViewHash(projectId, origin.source_graph_target, "chats", {
        chatId: origin.source_chat_id,
      })}
    >
      <CornerUpLeft size={12} aria-hidden="true" />
      {origin.source_title}
    </a>
  );
}

/**
 * "Branch this chat": the branch picker, choosing an existing branch or creating
 * one from the chat's head, then opening the new chat there.
 */
export function ChatBranchPicker({
  projectId,
  chatId,
  graphTarget,
  disabled = false,
}: {
  projectId: string;
  chatId: string;
  graphTarget: GraphTargetRef;
  disabled?: boolean;
}) {
  const [refs, setRefs] = useState<GraphRef[] | null | undefined>(undefined);
  const [failed, setFailed] = useState<string | null>(null);
  // A branch already created for this name: a retry after the chat step failed
  // opens the chat there instead of creating the branch again.
  const created = useRef<{ name: string; target: GraphTargetRef } | null>(null);
  // One request id per target: a retry after a lost response reopens the same chat.
  const attempt = useRef<{ target: GraphTargetRef; requestId: string } | null>(null);
  useEffect(() => {
    let live = true;
    loadGraphRefs(projectId).then(
      (loaded) => live && setRefs(loaded),
      () => live && setRefs(null),
    );
    return () => {
      live = false;
    };
  }, [projectId]);

  const open = async (target: GraphTargetRef) => {
    setFailed(null);
    if (!attempt.current || !sameGraphTarget(attempt.current.target, target))
      attempt.current = { target, requestId: crypto.randomUUID() };
    try {
      const transcript = await branchChat(projectId, chatId, target, attempt.current.requestId);
      attempt.current = null;
      window.location.hash = projectViewHash(projectId, transcript.graph_target, "chats", {
        chatId: transcript.chat_id,
      });
    } catch (failure) {
      setFailed(failure instanceof Error ? failure.message : String(failure));
      throw failure;
    }
  };

  const head = refs?.find((ref) => sameGraphTarget(graphRefTarget(ref), graphTarget))?.head;
  return (
    <div
      className="chat-branch-picker"
      data-failed={failed ? "true" : undefined}
      title={failed ?? undefined}
    >
      <GraphPicker
        refs={disabled ? null : refs}
        activeRef={graphTarget}
        projectId={projectId}
        label="Branch this chat"
        heading="Move to branch"
        pickOnly
        viewedHead={head ?? null}
        onSelect={(target) => void open(target).catch(() => undefined)}
        onCreate={async (name, requestId, forkHead) => {
          if (created.current?.name !== name) {
            await requireChatBranchable(projectId, chatId);
            const fork = await createGraphFork(
              projectId,
              forkHead.target,
              forkHead.revision,
              requestId,
              name,
            );
            created.current = { name, target: graphRefTarget(fork) };
          }
          await open(created.current.target);
        }}
        onRefUpdated={() => undefined}
      />
    </div>
  );
}
