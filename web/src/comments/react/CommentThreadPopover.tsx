import { CommentPopover, type PopoverAnchor } from "./CommentComposer";
import { CommentThreadCard } from "./CommentThreadCard";
import type { CommentThread } from "../types.ts";
import type { CommentsController } from "./useCommentActions";

/** One thread opened from its pin: the card, and the line a failed action reports on. */
export function CommentThreadPopover({
  thread,
  point,
  comments,
  wholeLabel,
  onLocate,
  onClose,
}: {
  thread: CommentThread;
  point: PopoverAnchor | null;
  comments: CommentsController;
  wholeLabel: string;
  onLocate: () => void;
  onClose: () => void;
}) {
  return (
    <CommentPopover point={point} label="Comment" onClose={onClose}>
      <CommentThreadCard
        thread={thread}
        comments={comments}
        wholeLabel={wholeLabel}
        selected={false}
        onLocate={onLocate}
      />
      <div className="rcp-comments-notice" role="status">
        {comments.error}
      </div>
    </CommentPopover>
  );
}
