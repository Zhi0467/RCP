import { useCallback, useMemo, useRef, useState } from "react";
import type { CommentsClient } from "../api.ts";
import type { CommentAnchor, CommentMember, CommentTarget, CommentThread } from "../types.ts";
import type { DockThreads } from "./CommentDock.tsx";
import { errorText } from "./threadModel.ts";

export interface CommentsController {
  threads: CommentThread[];
  loaded: boolean;
  members: CommentMember[];
  busy: boolean;
  error: string | null;
  showError(cause: unknown): void;
  clearError(): void;
  canPost: boolean;
  create(
    target: CommentTarget,
    anchor: CommentAnchor,
    text: string,
    drawnVersion: string | null,
  ): Promise<CommentThread | null>;
  reply(threadId: string, text: string): Promise<boolean>;
  setResolved(threadId: string, resolved: boolean): Promise<void>;
}

/** The actions every comment surface shares over its own thread source: one action
 *  at a time, a refusal shown in the rail, `reload` after each, and every saved
 *  thread handed to `dock`, which carries it with the surface's next message. */
export function useCommentActions({
  client,
  threads,
  loaded,
  members,
  canPost,
  dock,
  reload,
}: {
  client: CommentsClient;
  threads: CommentThread[];
  loaded: boolean;
  members: CommentMember[];
  canPost: boolean;
  dock: DockThreads | null;
  reload: () => void;
}): CommentsController {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const dockRef = useRef(dock);
  dockRef.current = dock;
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const run = useCallback(async <T>(action: () => Promise<T>): Promise<T | null> => {
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
      reloadRef.current();
    }
  }, []);
  const showError = useCallback((cause: unknown) => setError(errorText(cause)), []);
  const clearError = useCallback(() => setError(null), []);
  const handOff = useCallback((thread: CommentThread) => {
    if (thread.can_send) dockRef.current?.([thread]);
  }, []);
  return useMemo(
    () => ({
      threads,
      loaded,
      members,
      busy,
      error,
      showError,
      clearError,
      canPost,
      create: (target, anchor, text, drawnVersion) =>
        run(async () => {
          const thread = await client.create(target, anchor, text, drawnVersion);
          handOff(thread);
          return thread;
        }),
      reply: async (threadId, text) =>
        (await run(async () => {
          handOff(await client.reply(threadId, text));
          return true;
        })) === true,
      setResolved: async (threadId, resolved) => {
        await run(() => (resolved ? client.resolve(threadId) : client.reopen(threadId)));
      },
    }),
    [threads, loaded, members, busy, error, showError, clearError, canPost, client, run, handOff],
  );
}
