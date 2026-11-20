import { useCallback, useEffect, useMemo } from "react";
import { api } from "../../core/api";
import type { ResourceProfile } from "../../core/resourceStore";
import { resourceKeyId } from "../../core/resourceStore";
import { useRefreshRuntime, useResource } from "../../core/useResource";
import { projectResourceKey, useProjectResource } from "../../projects/projectResources";
import { createCommentsClient, createScopedCommentsClient, type CommentScope } from "../api.ts";
import type { CommentMember, CommentTarget, CommentThread } from "../types.ts";
import { targetThreads } from "./threadModel.ts";
import { useCommentActions, type CommentsController } from "./useCommentActions";
import type { DockThreads } from "./CommentDock.tsx";

export type { CommentsController, DockThreads };

/** Every thread on these targets of one kind, or on every reply in `chatId` in one
 *  read. Any post or thread change moves the `comments` watermark, which re-reads it;
 *  a new target set is a new read, so a slower answer for the previous file or chat
 *  never shows here. A Space thread's comments have no watermark; each action re-reads. */
function threadsProfile(
  scope: CommentScope,
  kind: CommentTarget["kind"],
  ids: readonly string[],
  chatId: string | null,
): ResourceProfile<CommentThread[]> {
  const query = chatId ? `chat:${chatId}` : `threads:${kind}:${ids.join(",")}`;
  return {
    key:
      scope.kind === "project"
        ? projectResourceKey(scope.projectId, "comments", { query })
        : {
            backend: "",
            member: "",
            project: "",
            name: "agent_threads",
            entity: scope.threadId,
            query: `comments:${query}`,
          },
    fetch: async (signal) => {
      const client = createScopedCommentsClient(scope, (path, init) =>
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
  scope,
  kind,
  ids,
  canPost,
  dock,
  picker,
  chatId = null,
}: {
  scope: CommentScope;
  kind: CommentTarget["kind"];
  ids: readonly string[];
  /** Reply threads: read the whole chat at once; `ids` still picks which show. */
  chatId?: string | null;
  canPost: boolean;
  dock: DockThreads | null;
  /** Whether an editor is on screen, so its `@` picker needs the members. */
  picker: boolean;
}): CommentsController {
  const idKey = ids.join(",");
  const scopeId = scope.kind === "project" ? scope.projectId : scope.threadId;
  const profile = useMemo(
    () => (ids.length ? threadsProfile(scope, kind, ids, chatId) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [scope.kind, scopeId, kind, idKey, chatId],
  );
  const projectThreads = useProjectResource(scope.kind === "project" ? profile : null);
  const spaceThreads = useResource("space-comments", scope.kind === "space" ? profile : null);
  const resource = scope.kind === "project" ? projectThreads : spaceThreads;
  const { store, scheduler } = useRefreshRuntime();
  const spaceKey = scope.kind === "space" ? profile?.key : undefined;
  useEffect(() => {
    if (!spaceKey) return;
    const { project, name, ref, entity } = spaceKey;
    return scheduler.watchAgentThreads((version, observation) =>
      store.observe(project, [{ name, ref, entity, version: version ?? "" }], observation),
    );
  }, [spaceKey, scheduler, store]);
  const threads = useMemo(
    () => targetThreads(resource.value ?? [], kind, ids),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [resource.value, kind, idKey],
  );
  const projectId = scope.kind === "project" ? scope.projectId : null;
  const memberResource = useProjectResource(
    useMemo(
      () => (projectId && picker && canPost ? membersPickerProfile(projectId) : null),
      [projectId, picker, canPost],
    ),
  );
  const members = useMemo(() => memberResource.value ?? [], [memberResource.value]);
  const client = useMemo(
    () => createScopedCommentsClient(scope, api),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [scope.kind, scopeId],
  );
  const reload = useCallback(() => {
    if (profile) store.invalidate(resourceKeyId(profile.key));
  }, [store, profile]);
  return useCommentActions({
    client,
    threads,
    loaded: !profile || resource.value !== undefined,
    members,
    canPost,
    dock,
    reload,
  });
}
