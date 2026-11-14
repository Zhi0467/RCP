import type {
  CommentAnchor,
  CommentMember,
  CommentMention,
  CommentTarget,
  CommentThread,
  CommentThreadPage,
} from "./types.ts";

export class CommentRequestError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail
          ? String(detail.message)
          : `Comment request failed (${status})`,
    );
    this.status = status;
    this.detail = detail;
  }
}

export function createCommentsClient(projectId: string, fetcher: typeof fetch = fetch) {
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const threadPath = (id: string) => `${base}/comment-threads/${encodeURIComponent(id)}`;
  const request = async <T>(
    path: string,
    method = "GET",
    body?: unknown,
    key?: string,
  ): Promise<T> => {
    const sent = method === "GET" ? body : (body ?? {});
    const headers: Record<string, string> = {};
    if (sent !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    const response = await fetcher(path, {
      method,
      credentials: "same-origin",
      headers,
      ...(sent === undefined ? {} : { body: JSON.stringify(sent) }),
    });
    const value = response.status === 204 ? undefined : await response.json();
    if (!response.ok) throw new CommentRequestError(response.status, value?.detail);
    return value as T;
  };
  const threadRequest = async (path: string, method = "GET", body?: unknown) =>
    (await request<{ thread: CommentThread }>(path, method, body)).thread;
  return {
    list: (target: CommentTarget) =>
      request<CommentThreadPage>(
        `${base}/comment-threads?${new URLSearchParams({ target_kind: target.kind, target_id: target.id })}`,
      ),
    listChatReplies: (chatId: string) =>
      request<CommentThreadPage>(
        `${base}/comment-threads?${new URLSearchParams({ target_kind: "reply", chat_id: chatId })}`,
      ),
    get: (id: string) => threadRequest(threadPath(id)),
    create: (
      target: CommentTarget,
      anchor: CommentAnchor,
      text: string,
      drawnVersion: string | null = null,
    ) =>
      threadRequest(`${base}/comment-threads`, "POST", {
        target,
        anchor,
        drawn_version: drawnVersion,
        text,
      }),
    reply: (id: string, text: string) => threadRequest(`${threadPath(id)}/posts`, "POST", { text }),
    editPost: (id: string, text: string) =>
      threadRequest(`${base}/comment-posts/${encodeURIComponent(id)}`, "PATCH", { text }),
    deletePost: (id: string) =>
      request<{ ok: boolean }>(`${base}/comment-posts/${encodeURIComponent(id)}`, "DELETE"),
    resolve: (id: string) => threadRequest(`${threadPath(id)}/resolve`, "POST"),
    reopen: (id: string) => threadRequest(`${threadPath(id)}/reopen`, "POST"),
    members: async (): Promise<CommentMember[]> =>
      (await request<{ user_id: string; display_name: string | null }[]>(`${base}/members`)).map(
        (member) => ({
          user_id: member.user_id,
          display_name: member.display_name || member.user_id,
        }),
      ),
    mentions: async () =>
      (await request<{ mentions: CommentMention[] }>(`${base}/comment-mentions`)).mentions,
    sendArtifact: (id: string, threadIds: readonly string[], freshSession: boolean, key: string) =>
      request<{ operation_id: string }>(
        `${base}/artifacts/${encodeURIComponent(id)}/comments`,
        "POST",
        { thread_ids: threadIds, fresh_session: freshSession },
        key,
      ),
    startTask: (body: Record<string, unknown>, threadIds: readonly string[], key: string) =>
      request<{ operation_id: string }>(
        `${base}/tasks`,
        "POST",
        { ...body, comment_thread_ids: threadIds },
        key,
      ),
    steer: (
      operationId: string,
      message: string,
      threadIds: readonly string[],
      messageId: string,
    ) =>
      request<unknown>(`${base}/tasks/${encodeURIComponent(operationId)}/steer`, "POST", {
        message,
        comment_thread_ids: threadIds,
        message_id: messageId,
      }),
  };
}
