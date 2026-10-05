import { useCallback, useEffect, useRef, useState } from "react";
import { loadConsolidation } from "../consolidation";
import type { ConsolidationView } from "../types";

/** Reuses the project refresh and heartbeat that refresh the Inbox, without another clock. */
export function useConsolidation(apiBase: string, freshness: string) {
  const [snapshot, setSnapshot] = useState<{ key: string; view: ConsolidationView } | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const reload = useRef<() => void>(() => undefined);
  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let queued = false;
    const fetch = async () => {
      if (!apiBase || cancelled) return;
      if (inFlight) {
        queued = true;
        return;
      }
      inFlight = true;
      try {
        const view = await loadConsolidation(apiBase);
        if (!cancelled) {
          setSnapshot({ key: apiBase, view });
          setError(null);
        }
      } catch (reason) {
        if (!cancelled)
          setError({
            key: apiBase,
            message: reason instanceof Error ? reason.message : String(reason),
          });
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
  }, [apiBase]);
  useEffect(() => {
    reload.current();
  }, [apiBase, freshness]);
  const refresh = useCallback(() => reload.current(), []);
  return {
    consolidation: snapshot?.key === apiBase ? snapshot.view : null,
    error: error?.key === apiBase ? error.message : null,
    refresh,
  };
}
