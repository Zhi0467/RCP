import { useCallback, useEffect, useRef, useState } from "react";
import { fetchQuestions } from "../core/api";
import type { AgentQuestion } from "../core/types";

/** Reuses the owner's refreshed projection and project heartbeat, without another clock. */
export function useQuestions(
  apiBase: string,
  kind: "chat" | "episode",
  ownerId: string | undefined,
  freshness: string,
) {
  const [snapshot, setSnapshot] = useState<{ key: string; items: AgentQuestion[] }>({
    key: "",
    items: [],
  });
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const reload = useRef<() => void>(() => undefined);
  const key = `${apiBase}/${kind}/${ownerId}`;
  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let queued = false;
    const fetch = async () => {
      if (!ownerId || cancelled) return;
      if (inFlight) {
        queued = true;
        return;
      }
      inFlight = true;
      try {
        const items = await fetchQuestions(apiBase, kind, ownerId);
        if (!cancelled) {
          setSnapshot({ key, items });
          setError(null);
        }
      } catch (reason) {
        if (!cancelled)
          setError({ key, message: reason instanceof Error ? reason.message : String(reason) });
      } finally {
        inFlight = false;
        if (queued && !cancelled) {
          queued = false;
          void fetch();
        }
      }
    };
    reload.current = () => void fetch();
    const heartbeat = (event: Event) => {
      if ((event as CustomEvent<string>).detail === apiBase) void fetch();
    };
    window.addEventListener("rcp:refresh-questions", heartbeat);
    return () => {
      cancelled = true;
      window.removeEventListener("rcp:refresh-questions", heartbeat);
    };
  }, [apiBase, kind, ownerId, key]);
  useEffect(() => {
    reload.current();
  }, [key, freshness]);
  const refresh = useCallback(() => reload.current(), []);
  return {
    questions: snapshot.key === key ? snapshot.items : [],
    error: error?.key === key ? error.message : null,
    refresh,
  };
}
