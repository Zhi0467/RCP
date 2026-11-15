import { useCallback, useMemo } from "react";
import { api } from "../../core/api";
import type { ResourceProfile } from "../../core/resourceStore";
import { resourceKeyId } from "../../core/resourceStore";
import { useRefreshRuntime } from "../../core/useResource";
import { projectResourceKey, useProjectResource } from "../../projects/projectResources";
import { createCommentsClient } from "../api.ts";
import type { CommentMember, CommentTarget, CommentThread } from "../types.ts";
import { targetThreads } from "./threadModel.ts";
import { useCommentActions, type CommentsController, type SendThreads } from "./useCommentActions";

export type { CommentsController, SendThreads };

/** Every thread on these targets of one kind, or on every reply in `chatId` in one
 *  read. Any post or thread change moves the `comments` watermark, which re-reads it;
 *  a new target set is a new read, so a slower answer for the previous file or chat
 *  never shows here. */
function threadsProfile(
  projectId: string,
  kind: CommentTarget["kind"],
  ids: readonly string[],
  chatId: string | null,
): ResourceProfile<CommentThread[]> {
  const query = chatId ? `chat:${chatId}` : `threads:${kind}:${ids.join(",")}`;
  return {
    key: projectResourceKey(projectId, "comments", { query }),
    fetch: async (signal) => {
      const client = createCommentsClient(projectId, (path, init) =>
        api(path, { ...init, signal }),
      );
      if (chatId) return (await client.listChatReplies(chatId)).threads;
      const pages = await Promise.all(ids.map((id) => client.list({ kind, id })));
      return pages.flatMap((page) => page.threads);
    },
  };
}

/** The project's members, whom the `@` picker offers; the server drops a mention of
 *  one who cannot read the target. */
function membersPickerProfile(projectId: string): ResourceProfile<CommentMember[]> {
  return {
    key: projectResourceKey(projectId, "members", { query: "comment-picker" }),
    fetch: (signal) =>
      createCommentsClient(projectId, (path, init) => api(path, { ...init, signal })).members(),
  };
}

/** Threads on `ids` (one target kind), the project's members for the picker, and
 *  the actions every comment surface shares. */
export function useComments({
  projectId,
  kind,
  ids,
  canPost,
  send,
  picker,
  chatId = null,
}: {
  projectId: string;
  kind: CommentTarget["kind"];
  ids: readonly string[];
  /** Reply threads: read the whole chat at once; `ids` still picks which show. */
  chatId?: string | null;
  canPost: boolean;
  send: SendThreads | null;
  /** Whether an editor is on screen, so its `@` picker needs the members. */
  picker: boolean;
}): CommentsController {
  const { store } = useRefreshRuntime();
  const idKey = ids.join(",");
  const profile = useMemo(
    () => (ids.length ? threadsProfile(projectId, kind, ids, chatId) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [projectId, kind, idKey, chatId],
  );
  const resource = useProjectResource(profile);
  const threads = useMemo(
    () => targetThreads(resource.value ?? [], kind, ids),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [resource.value, kind, idKey],
  );
  const memberResource = useProjectResource(
    useMemo(
      () => (picker && canPost ? membersPickerProfile(projectId) : null),
      [projectId, picker, canPost],
    ),
  );
  const members = useMemo(() => memberResource.value ?? [], [memberResource.value]);
  const client = useMemo(() => createCommentsClient(projectId, api), [projectId]);
  const reload = useCallback(() => {
    if (profile) store.invalidate(resourceKeyId(profile.key));
  }, [store, profile]);
  return useCommentActions({
    client,
    threads,
    loaded: !profile || resource.value !== undefined,
    members,
    canPost,
    send,
    reload,
  });
}
