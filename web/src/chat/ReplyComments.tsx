import { useEffect, useRef, useState, type RefObject } from "react";
import { MessageSquare } from "lucide-react";
import { findText } from "../comments/anchors.ts";
import type { Point } from "../comments/box.ts";
import type { CommentThread } from "../comments/types.ts";
import {
  CommentComposer,
  CommentPopover,
  type CommentOffer,
} from "../comments/react/CommentComposer";
import { CommentPins } from "../comments/react/CommentPins";
import { CommentList } from "../comments/react/CommentRail";
import { CommentThreadCard } from "../comments/react/CommentThreadCard";
import {
  textOffset,
  flashRect,
  insideElement,
  rangeEnd,
  textRange,
  toRect,
} from "../comments/react/domText.ts";
import { MAX_QUOTE_CHARS, surroundingText } from "../comments/react/threadModel.ts";
import { useComments, type SendThreads } from "../comments/react/useComments";
import {
  SelectionCommentButton,
  useSelectionComment,
  firstLine,
  type SelectionCommentOffer,
} from "../comments/react/useSelectionComment";
import { takeCommentThreadRequest, useCommentThreadRequest } from "../core/commentThreadRequest";
import { annotatableAnswerSelectionRange } from "./chatInput";
import "./replyComments.css";

const GONE = "The anchor is gone";

function answers(root: HTMLElement, operationId: string): HTMLElement[] {
  return [
    ...root.querySelectorAll<HTMLElement>(
      `.agent-chat-view-line[data-task-id="${CSS.escape(operationId)}"] .chat-annotatable-answer`,
    ),
  ];
}

/** The range a reply thread's quote covers now, re-found in its turn's answers. */
function threadRange(root: HTMLElement, thread: CommentThread): Range | null {
  if (thread.anchor.kind !== "text") return null;
  for (const answer of answers(root, thread.target_id)) {
    const match = findText(
      answer.textContent ?? "",
      thread.anchor.text,
      thread.anchor.surrounding_text,
    );
    const range = match && textRange(answer, match.start, match.end);
    if (range) return range;
  }
  return null;
}

/** Shared comments on a project chat's replies: Comment on selected reply text, a
 *  pin at each open thread's quote that opens it, and the header count that lists
 *  the chat's threads. Threads go to the chat through its composer's sender. */
export function useReplyComments({
  projectId,
  chatId,
  replyIds,
  linesRef,
  canPost,
  send,
}: {
  projectId: string;
  chatId: string;
  /** The operation ids of this chat's replies, which name their threads. */
  replyIds: readonly string[];
  linesRef: RefObject<HTMLElement | null>;
  canPost: boolean;
  send: SendThreads | null;
}) {
  const [offer, setOffer] = useState<CommentOffer | null>(null);
  const [shown, setShown] = useState<{ threadId: string; point: Point | null } | null>(null);
  const [listPoint, setListPoint] = useState<Point | null>(null);
  const comments = useComments({
    projectId,
    kind: "reply",
    ids: replyIds,
    chatId,
    canPost,
    send,
    picker: Boolean(offer || shown || listPoint),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const countButton = useRef<HTMLButtonElement>(null);
  const replyKey = replyIds.join(",");
  useEffect(() => {
    setOffer(null);
    setShown(null);
    setListPoint(null);
    setSelected(null);
  }, [projectId, chatId]);

  const resolve = (selection: Selection): SelectionCommentOffer | null => {
    const root = linesRef.current;
    const range = annotatableAnswerSelectionRange(selection, root);
    const answer = (
      range?.startContainer instanceof Element
        ? range.startContainer
        : range?.startContainer.parentElement
    )?.closest<HTMLElement>(".chat-annotatable-answer");
    const operationId = answer?.closest<HTMLElement>(".agent-chat-view-line[data-task-id]")?.dataset
      .taskId;
    if (!range || !answer || !operationId || !replyIds.includes(operationId)) return null;
    const quote = range.toString().trim().slice(0, MAX_QUOTE_CHARS);
    if (!quote) return null;
    const text = answer.textContent ?? "";
    const at = text.indexOf(
      quote,
      Math.max(0, textOffset(answer, range.startContainer, range.startOffset) - 1),
    );
    const rect = range.getBoundingClientRect();
    return {
      rect,
      firstLine: firstLine(range),
      open: () =>
        setOffer({
          target: { kind: "reply", id: operationId },
          anchor: {
            kind: "text",
            text: quote,
            surrounding_text: at < 0 ? quote : surroundingText(text, at, at + quote.length),
            comment: "",
          },
          drawnVersion: null,
          point: { x: rect.left, y: rect.bottom },
        }),
    };
  };
  const selection = useSelectionComment(canPost && !offer, resolve);

  const position = (thread: CommentThread): Point | null => {
    const root = linesRef.current;
    const range = root && threadRange(root, thread);
    const point = range && rangeEnd(range);
    return point && insideElement(point, root) ? point : null;
  };
  const locate = (thread: CommentThread) => {
    setSelected(thread.thread_id);
    const root = linesRef.current;
    const range = root && threadRange(root, thread);
    const element =
      (range?.startContainer.parentElement as HTMLElement | null) ??
      (root && answers(root, thread.target_id)[0]);
    element?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => {
      const moved = root && threadRange(root, thread);
      if (moved) flashRect(toRect(moved.getBoundingClientRect()));
    }, 350);
  };
  const locateRef = useRef(locate);
  locateRef.current = locate;

  const request = useCommentThreadRequest();
  useEffect(() => {
    if (!comments.loaded || request?.targetKind !== "reply") return;
    if (!replyIds.includes(request.targetId)) return;
    const threadId = takeCommentThreadRequest(projectId, "reply", request.targetId);
    const thread = comments.threads.find((item) => item.thread_id === threadId);
    if (!thread) return;
    locateRef.current(thread);
    window.setTimeout(() => setShown({ threadId: thread.thread_id, point: position(thread) }), 400);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the reply set is named by its key
  }, [request, projectId, replyKey, comments.loaded, comments.threads]);

  const shownThread =
    shown && comments.threads.find((thread) => thread.thread_id === shown.threadId);
  const openCount = comments.threads.filter((thread) => thread.state === "open").length;
  const note = (thread: CommentThread) => {
    const root = linesRef.current;
    return thread.state === "open" &&
      thread.anchor.kind === "text" &&
      root &&
      !threadRange(root, thread)
      ? GONE
      : undefined;
  };

  const headerButton =
    comments.threads.length > 0 ? (
      <button
        ref={countButton}
        type="button"
        className={`chat-comment-count${listPoint ? " is-open" : ""}`}
        aria-expanded={Boolean(listPoint)}
        aria-label={`${openCount} open comment${openCount === 1 ? "" : "s"}`}
        onClick={() => {
          if (listPoint) {
            setListPoint(null);
            return;
          }
          const box = countButton.current?.getBoundingClientRect();
          setListPoint(box ? { x: box.right - 344, y: box.bottom } : null);
        }}
      >
        <MessageSquare size={12} /> {openCount}
      </button>
    ) : null;

  const layers = (
    <>
      <CommentPins
        threads={comments.threads}
        position={position}
        selected={selected}
        revision={replyKey}
        onPick={(thread) => {
          setSelected(thread.thread_id);
          setShown({ threadId: thread.thread_id, point: position(thread) });
        }}
      />
      <SelectionCommentButton offer={selection.offer} onOpen={() => selection.setOffer(null)} />
      {offer && (
        <CommentComposer
          offer={offer}
          comments={comments}
          agentLabel="This chat"
          canSend={Boolean(send)}
          onClose={() => setOffer(null)}
          onCreated={(thread) => setSelected(thread.thread_id)}
        />
      )}
      {shownThread && (
        <CommentPopover point={shown.point} label="Comment" onClose={() => setShown(null)}>
          <CommentThreadCard
            thread={shownThread}
            comments={comments}
            agentLabel="This chat"
            wholeLabel="Whole reply"
            selected={false}
            onLocate={() => locate(shownThread)}
          />
          <div className="rcp-comments-notice" role="status">
            {comments.error}
          </div>
        </CommentPopover>
      )}
      {listPoint && (
        <CommentPopover
          point={listPoint}
          label="Comments"
          className="chat-comment-list"
          onClose={() => setListPoint(null)}
        >
          <CommentList
            comments={comments}
            agentLabel="This chat"
            wholeLabel="Whole reply"
            selected={selected}
            onLocate={locate}
            note={note}
          />
        </CommentPopover>
      )}
    </>
  );
  return { headerButton, layers };
}
