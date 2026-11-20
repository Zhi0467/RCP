import { useMemo } from "react";
import { Link } from "lucide-react";
import { bulletinTime } from "../../chat/bulletinModel";
import { copyShareLink, useShareLinks } from "../../core/shareLinks";
import { commentMentionCandidates, decodeCommentMentions } from "../mentions.ts";
import { anchorQuote } from "../model.ts";
import type { CommentMember, CommentPost, CommentThread } from "../types.ts";
import { CommentEditor } from "./CommentEditor";
import type { CommentsController } from "./useCommentActions";

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
    return decodeCommentMentions(post.text, commentMentionCandidates(known));
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

/** One thread: its quote, its posts in one flat column, and a reply box with Resolve. */
export function CommentThreadCard({
  thread,
  comments,
  wholeLabel,
  selected,
  onLocate,
  note,
}: {
  thread: CommentThread;
  comments: CommentsController;
  /** What a whole-target thread's quote line reads. */
  wholeLabel: string;
  selected: boolean;
  onLocate: () => void;
  /** Why the thread has no pin, shown on hover. */
  note?: string;
}) {
  const open = thread.state === "open";
  const share = useShareLinks();
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
        <button type="button" className="rcp-comment-quote" onClick={onLocate}>
          {quote}
        </button>
        {share && (
          <button
            type="button"
            className="rcp-comment-link"
            aria-label="Copy link"
            title="Copy link"
            onClick={() =>
              copyShareLink(thread.project_id, {
                kind: "comment",
                item_id: thread.thread_id,
                graph_target: thread.graph_target,
              }).catch(comments.showError)
            }
          >
            <Link size={14} aria-hidden="true" />
          </button>
        )}
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
            busy={comments.busy}
            secondary={
              <button
                type="button"
                className="rcp-comment-resolve"
                disabled={comments.busy}
                onClick={() => void comments.setResolved(thread.thread_id, open)}
              >
                {open ? "Resolve" : "Reopen"}
              </button>
            }
            onSubmit={(text) => comments.reply(thread.thread_id, text)}
          />
        </>
      )}
    </article>
  );
}
