import { useCallback, useMemo, useRef, useState } from "react";
import type { ResourceProfile } from "../../core/resourceStore";
import { resourceKeyId } from "../../core/resourceStore";
import { useRefreshRuntime } from "../../core/useResource";
import type { ProjectMember } from "../../core/types";
import { api } from "../../core/api";
import { projectResourceKey, useProjectResource } from "../../projects/projectResources";
import { createCommentsClient } from "../api.ts";
import type { CommentAnchor, CommentMember, CommentTarget, CommentThread } from "../types.ts";
import { errorText, sendKeys, targetThreads } from "./threadModel.ts";

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
      const client = createCommentsClient(projectId, (input, init) =>
        fetch(input, { ...init, signal }),
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
  const path = `/api/projects/${encodeURIComponent(projectId)}/members`;
  return {
    key: projectResourceKey(projectId, "members", { query: "comment-picker" }),
    fetch: async (signal) =>
      (await api<ProjectMember[]>(path, { signal })).map((member) => ({
        user_id: member.user_id,
        display_name: member.display_name || member.user_id,
      })),
  };
}

/** What a surface sends ticked threads with: its composer's own sender, which
 *  steers a running turn or starts one, under `key` so a retry repeats it. */
export type SendThreads = (threadIds: string[], key: string) => Promise<unknown>;

export interface CommentsController {
  threads: CommentThread[];
  loaded: boolean;
  members: CommentMember[];
  busy: boolean;
  error: string | null;
  clearError(): void;
  canPost: boolean;
  /** A sender exists here; each thread's `can_send` still decides. */
  canSend: boolean;
  create(
    target: CommentTarget,
    anchor: CommentAnchor,
    text: string,
    drawnVersion: string | null,
    send: boolean,
  ): Promise<CommentThread | null>;
  reply(threadId: string, text: string, send: boolean): Promise<boolean>;
  setResolved(threadId: string, resolved: boolean): Promise<void>;
  send(threadIds: string[]): Promise<boolean>;
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
  const client = useMemo(() => createCommentsClient(projectId), [projectId]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const keys = useRef(sendKeys());
  const sendRef = useRef(send);
  sendRef.current = send;
  const reload = useCallback(() => {
    if (profile) store.invalidate(resourceKeyId(profile.key));
  }, [store, profile]);

  /** One action at a time; a refusal shows in the rail and changes nothing here. */
  const run = useCallback(
    async <T>(action: () => Promise<T>): Promise<T | null> => {
      if (busyRef.current) return null;
      busyRef.current = true;
      setBusy(true);
      setError(null);
      try {
        return await action();
      } catch (cause) {
        setError(errorText(cause));
        return null;
      } finally {
        busyRef.current = false;
        setBusy(false);
        reload();
      }
    },
    [reload],
  );
  const fail = useCallback((cause: unknown) => setError(errorText(cause)), []);
  const clearError = useCallback(() => setError(null), []);
  const sendNow = useCallback(async (threadIds: string[]) => {
    const sender = sendRef.current;
    if (!sender || !threadIds.length) return;
    await sender(threadIds, keys.current.key(threadIds));
    keys.current.done();
  }, []);

  return {
    threads,
    loaded: !profile || resource.value !== undefined,
    members,
    busy,
    error,
    clearError,
    canPost,
    canSend: Boolean(send),
    create: (target, anchor, text, drawnVersion, toAgent) =>
      run(async () => {
        const thread = await client.create(target, anchor, text, drawnVersion);
        if (toAgent) await sendNow([thread.thread_id]).catch(fail);
        return thread;
      }),
    reply: async (threadId, text, toAgent) =>
      (await run(async () => {
        await client.reply(threadId, text);
        if (toAgent) await sendNow([threadId]).catch(fail);
        return true;
      })) === true,
    setResolved: async (threadId, resolved) => {
      await run(() => (resolved ? client.resolve(threadId) : client.reopen(threadId)));
    },
    send: async (threadIds) =>
      (await run(async () => {
        await sendNow(threadIds);
        return true;
      })) === true,
  };
}
