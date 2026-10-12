import { useCallback, useEffect, useRef, useState } from "react";
import { loadProjectDigest, markDigestCaughtUp } from "../core/api";
import { errorMessage } from "../core/errors";
import { graphSessionKey } from "../core/graphTarget";
import { catchUpProjectDigest, createDigestRequestFence } from "./projectDigest";
import type { GraphTargetRef, ProjectDigest } from "../core/types";

/**
 * The open project's "Since you last looked" digest for the open graph target.
 * Reads again when `freshness` (the snapshot revision) changes and on the
 * project heartbeat that refreshes the Inbox, without another clock.
 */
export function useProjectDigest(
  projectId: string | null,
  target: GraphTargetRef,
  freshness: string,
  onCaughtUp?: () => void,
) {
  // Responses are keyed by project and target, so a read for another target never paints.
  const key = projectId ? graphSessionKey(projectId, target) : null;
  const [snapshot, setSnapshot] = useState<{ key: string; digest: ProjectDigest } | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const [catchingUp, setCatchingUp] = useState(false);
  const fence = useRef(createDigestRequestFence());
  const inFlight = useRef(0);
  const activeKey = useRef(key);
  activeKey.current = key;

  const load = useCallback(async (id: string, at: GraphTargetRef) => {
    const readKey = graphSessionKey(id, at);
    const generation = fence.current.begin(readKey);
    inFlight.current += 1;
    try {
      const digest = await loadProjectDigest(id, at);
      if (fence.current.accept(readKey, generation)) {
        setSnapshot({ key: readKey, digest });
        setError(null);
      }
    } catch (reason) {
      if (fence.current.accept(readKey, generation))
        setError({ key: readKey, message: errorMessage(reason) });
    } finally {
      inFlight.current -= 1;
    }
  }, []);

  useEffect(() => {
    if (!projectId) return;
    const base = `/api/projects/${encodeURIComponent(projectId)}`;
    // The heartbeat ticks every second; skip a tick while a read is in flight.
    const heartbeat = (event: Event) => {
      if ((event as CustomEvent<string>).detail === base && inFlight.current === 0)
        void load(projectId, target);
    };
    window.addEventListener("rcp:refresh-questions", heartbeat);
    return () => window.removeEventListener("rcp:refresh-questions", heartbeat);
  }, [load, projectId, target]);

  const reload = useCallback(async () => {
    if (projectId) await load(projectId, target);
  }, [load, projectId, target]);

  useEffect(() => {
    void reload();
  }, [reload, freshness]);

  const digest = snapshot && snapshot.key === key ? snapshot.digest : null;

  const catchUp = useCallback(async () => {
    // One mark per member covers every target, so only main may move it.
    if (!projectId || !digest || target.kind !== "main") return;
    setCatchingUp(true);
    // A read issued before the mark moves must not repaint the acknowledged lines.
    fence.current.invalidate();
    try {
      // After the POST, read again only if this project is still the open one;
      // a late reload must not move the fence back from another project.
      const reloadIfOpen = async () => {
        if (activeKey.current === graphSessionKey(projectId, target)) await load(projectId, target);
      };
      await catchUpProjectDigest(projectId, digest, {
        mark: markDigestCaughtUp,
        reload: reloadIfOpen,
      });
      onCaughtUp?.();
    } catch (reason) {
      setError({ key: graphSessionKey(projectId, target), message: errorMessage(reason) });
    } finally {
      setCatchingUp(false);
    }
  }, [digest, load, onCaughtUp, projectId, target]);

  return {
    digest,
    error: error && error.key === key ? error.message : null,
    catchingUp,
    reload,
    catchUp,
  };
}
