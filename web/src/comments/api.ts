import { ApiError } from "../core/api.ts";
import type {
  CommentAnchor,
  CommentMember,
  CommentMention,
  CommentTarget,
  CommentThread,
  CommentThreadPage,
} from "./types.ts";

/** Sends one comment route request and resolves its JSON or throws an `ApiError`. */
export type CommentRequest = <T>(path: string, init: RequestInit) => Promise<T>;

/** A bare fetch for pages outside the app, such as the artifact viewer. */
export function fetchRequest(fetcher: typeof fetch = fetch): CommentRequest {
  return async <T>(path: string, init: RequestInit) => {
    const response = await fetcher(path, { ...init, credentials: "same-origin" });
    const value = response.status === 204 ? undefined : await response.json().catch(() => null);
    if (response.ok) return value as T;
    const detail: unknown = value?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const { message } = detail as { message?: unknown };
      throw new ApiError(
        typeof message === "string" ? message : `Comment request failed (${response.status})`,
        response.status,
        detail as Record<string, unknown>,
      );
    }
    throw new ApiError(
      typeof detail === "string" ? detail : `Comment request failed (${response.status})`,
      response.status,
    );
  };
}

export function createCommentsClient(projectId: string, request: CommentRequest) {
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const threadPath = (id: string) => `${base}/comment-threads/${encodeURIComponent(id)}`;
  const send = <T>(path: string, method = "GET", body?: unknown, key?: string): Promise<T> => {
    const sent = method === "GET" ? body : (body ?? {});
    const headers: Record<string, string> = {};
    if (sent !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    return request<T>(path, {
      method,
      headers,
      ...(sent === undefined ? {} : { body: JSON.stringify(sent) }),
    });
  };
  const threadRequest = async (path: string, method = "GET", body?: unknown) =>
    (await send<{ thread: CommentThread }>(path, method, body)).thread;
  return {
    list: (target: CommentTarget) =>
      send<CommentThreadPage>(
        `${base}/comment-threads?${new URLSearchParams({ target_kind: target.kind, target_id: target.id })}`,
      ),
    listChatReplies: (chatId: string) =>
      send<CommentThreadPage>(
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
    resolve: (id: string) => threadRequest(`${threadPath(id)}/resolve`, "POST"),
    reopen: (id: string) => threadRequest(`${threadPath(id)}/reopen`, "POST"),
    members: async (): Promise<CommentMember[]> =>
      (await send<{ user_id: string; display_name: string | null }[]>(`${base}/members`)).map(
        (member) => ({
          user_id: member.user_id,
          display_name: member.display_name || member.user_id,
        }),
      ),
    mentions: async () =>
      (await send<{ mentions: CommentMention[] }>(`${base}/comment-mentions`)).mentions,
    sendArtifact: (id: string, threadIds: readonly string[], freshSession: boolean, key: string) =>
      send<{ operation_id: string }>(
        `${base}/artifacts/${encodeURIComponent(id)}/comments`,
        "POST",
        { thread_ids: threadIds, fresh_session: freshSession },
        key,
      ),
  };
}

export type CommentsClient = ReturnType<typeof createCommentsClient>;
