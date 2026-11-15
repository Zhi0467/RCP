import type { AgentTask, ChatSummary } from "../core/types";

/** The Coach's chats: the member's unarchived Paper chats, newest first. A chat is the
 *  member's when one of its turns was theirs; with no member, every Paper chat is. */
export function memberPaperChats<T extends Pick<ChatSummary, "chat_id" | "updated_at" | "focus">>(
  summaries: T[],
  archived: string[],
  tasks: Pick<AgentTask, "request" | "authorized_by">[],
  actorId: string | null,
): T[] {
  const hidden = new Set(archived);
  const mine =
    actorId === null
      ? null
      : new Set(
          tasks
            .filter((task) => task.authorized_by?.user_id === actorId)
            .map((task) => task.request.chat_id),
        );
  return summaries
    .filter(
      (summary) =>
        summary.focus === "paper" &&
        !hidden.has(summary.chat_id) &&
        (mine === null || mine.has(summary.chat_id)),
    )
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}
