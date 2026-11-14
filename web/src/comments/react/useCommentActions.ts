import { useCallback, useMemo, useRef, useState } from "react";
import type { CommentsClient } from "../api.ts";
import type { CommentAnchor, CommentMember, CommentTarget, CommentThread } from "../types.ts";
import { errorText, sendKeys } from "./threadModel.ts";

/** What a surface sends ticked threads with: its composer's own sender, which
 *  steers a running turn or starts one, under `key` so a retry repeats it. */
export type SendThreads = (threadIds: string[], key: string) => Promise<unknown>;

export interface CommentsController {
  threads: CommentThread[];
  loaded: boolean;
  members: CommentMember[];
  busy: boolean;
  error: string | null;
  showError(cause: unknown): void;
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

/** The actions every comment surface shares over its own thread source: one action
 *  at a time, a refusal shown in the rail, and `reload` after each. */
export function useCommentActions({
  client,
  threads,
  loaded,
  members,
  canPost,
  send,
  reload,
}: {
  client: CommentsClient;
  threads: CommentThread[];
  loaded: boolean;
  members: CommentMember[];
  canPost: boolean;
  send: SendThreads | null;
  reload: () => void;
}): CommentsController {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const keys = useRef(sendKeys());
  const sendRef = useRef(send);
  sendRef.current = send;
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
  const sendNow = useCallback(async (threadIds: string[]) => {
    const sender = sendRef.current;
    if (!sender || !threadIds.length) return;
    try {
      await sender(threadIds, keys.current.key(threadIds));
    } catch (cause) {
      keys.current.failed(cause);
      throw cause;
    }
    keys.current.done();
  }, []);
  const canSend = Boolean(send);
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
      canSend,
      create: (target, anchor, text, drawnVersion, toAgent) =>
        run(async () => {
          const thread = await client.create(target, anchor, text, drawnVersion);
          if (toAgent) await sendNow([thread.thread_id]).catch(showError);
          return thread;
        }),
      reply: async (threadId, text, toAgent) =>
        (await run(async () => {
          await client.reply(threadId, text);
          if (toAgent) await sendNow([threadId]).catch(showError);
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
    }),
    [
      threads,
      loaded,
      members,
      busy,
      error,
      showError,
      clearError,
      canPost,
      canSend,
      client,
      run,
      sendNow,
    ],
  );
}
