import { useMemo } from "react";
import { bulletinTime } from "../../chat/bulletinModel";
import { commentMentionCandidates, decodeCommentMentions } from "../mentions.ts";
import { anchorQuote } from "../model.ts";
import type { CommentMember, CommentPost, CommentThread } from "../types.ts";
import { CommentEditor } from "./CommentEditor";
import type { CommentsController } from "./useComments";

function PostRow({ post, members }: { post: CommentPost; members: readonly CommentMember[] }) {
  const author = post.author.display_name || post.author.user_id || "Member";
  const text = useMemo(() => {
    const known = [
      ...members,
      ...(post.mentions ?? []).map((mention) => ({
        user_id: mention.id,
        display_name: mention.display_name,
      })),
    ].filter(
      (member, index, list) => list.findIndex((item) => item.user_id === member.user_id) === index,
    );
    return decodeCommentMentions(post.text, commentMentionCandidates(known, false));
  }, [post, members]);
  return (
    <section className="rcp-comment-post">
      <div className="rcp-comment-byline">
        <span className="rcp-comment-avatar" aria-hidden="true">
          {author.slice(0, 1).toUpperCase()}
        </span>
        <strong>{author}</strong>
        <time dateTime={post.created_at} title={new Date(post.created_at).toLocaleString()}>
          {bulletinTime(post.created_at)}
        </time>
      </div>
      <p>{text}</p>
    </section>
  );
}

/** One thread: its quote, its posts in one flat column, a reply box, Resolve, and Send. */
export function CommentThreadCard({
  thread,
  comments,
  agentLabel,
  wholeLabel,
  selected,
  ticked,
  onTick,
  onLocate,
  note,
}: {
  thread: CommentThread;
  comments: CommentsController;
  agentLabel: string;
  /** What a whole-target thread's quote line reads. */
  wholeLabel: string;
  selected: boolean;
  ticked?: boolean;
  onTick?: (ticked: boolean) => void;
  onLocate: () => void;
  /** Why the thread has no pin, shown on hover. */
  note?: string;
}) {
  const open = thread.state === "open";
  const sendable = open && comments.canSend && thread.can_send;
  const quote =
    anchorQuote(thread.anchor) ||
    (thread.anchor.kind === "whole" ? wholeLabel : thread.anchor.where || "Selected area");
  return (
    <article
      className={`rcp-comment-thread${selected ? " selected" : ""}`}
      data-thread-id={thread.thread_id}
      title={note}
      onClick={(event) => {
        if (!(event.target as Element).closest("button,input,textarea,select,a,form")) onLocate();
      }}
    >
      <div className="rcp-comment-thread-top">
        {sendable && onTick && (
          <input
            type="checkbox"
            aria-label="Select thread"
            checked={Boolean(ticked)}
            onChange={(event) => onTick(event.target.checked)}
          />
        )}
        <button type="button" className="rcp-comment-quote" onClick={onLocate}>
          {quote}
        </button>
      </div>
      {thread.posts.map((post) => (
        <PostRow key={post.post_id} post={post} members={comments.members} />
      ))}
      {thread.sent_operation_id && <span className="rcp-comment-sent">Sent to agent</span>}
      {comments.canPost && (
        <>
          <CommentEditor
            label="Reply"
            members={comments.members}
            canSend={sendable}
            agentLabel={agentLabel}
            busy={comments.busy}
            onSubmit={(text, send) =>
              text
                ? comments.reply(thread.thread_id, text, send)
                : comments.send([thread.thread_id])
            }
          />
          <div className="rcp-comment-actions">
            <button
              type="button"
              disabled={comments.busy}
              onClick={() => void comments.setResolved(thread.thread_id, open)}
            >
              {open ? "Resolve" : "Reopen"}
            </button>
          </div>
        </>
      )}
    </article>
  );
}
