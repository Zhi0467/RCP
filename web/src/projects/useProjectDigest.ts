import { useCallback, useEffect, useRef, useState } from "react";
import { loadProjectDigest, markDigestCaughtUp } from "../api";
import { errorMessage } from "../errors";
import { catchUpProjectDigest, createDigestRequestFence } from "../projectDigest";
import type { ProjectDigest } from "../types";

/**
 * The open project's "Since you last looked" digest. Reads again when
 * `freshness` (the snapshot revision) changes and on the project heartbeat
 * that refreshes the Inbox, without another clock.
 */
export function useProjectDigest(
  projectId: string | null,
  freshness: string,
  onCaughtUp?: () => void,
) {
  const [snapshot, setSnapshot] = useState<{ projectId: string; digest: ProjectDigest } | null>(
    null,
  );
  const [error, setError] = useState<{ projectId: string; message: string } | null>(null);
  const [catchingUp, setCatchingUp] = useState(false);
  const fence = useRef(createDigestRequestFence());
  const inFlight = useRef(0);
  const activeProject = useRef(projectId);
  activeProject.current = projectId;

  const load = useCallback(async (id: string) => {
    const generation = fence.current.begin(id);
    inFlight.current += 1;
    try {
      const digest = await loadProjectDigest(id);
      if (fence.current.accept(id, generation)) {
        setSnapshot({ projectId: id, digest });
        setError(null);
      }
    } catch (reason) {
      if (fence.current.accept(id, generation))
        setError({ projectId: id, message: errorMessage(reason) });
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
        void load(projectId);
    };
    window.addEventListener("rcp:refresh-questions", heartbeat);
    return () => window.removeEventListener("rcp:refresh-questions", heartbeat);
  }, [load, projectId]);

  const reload = useCallback(async () => {
    if (projectId) await load(projectId);
  }, [load, projectId]);

  useEffect(() => {
    void reload();
  }, [reload, freshness]);

  const digest = snapshot && snapshot.projectId === projectId ? snapshot.digest : null;

  const catchUp = useCallback(async () => {
    if (!projectId || !digest) return;
    setCatchingUp(true);
    // A read issued before the mark moves must not repaint the acknowledged lines.
    fence.current.invalidate();
    try {
      // After the POST, read again only if this project is still the open one;
      // a late reload must not move the fence back from another project.
      const reloadIfOpen = async () => {
        if (activeProject.current === projectId) await load(projectId);
      };
      await catchUpProjectDigest(projectId, digest, {
        mark: markDigestCaughtUp,
        reload: reloadIfOpen,
      });
      onCaughtUp?.();
    } catch (reason) {
      setError({ projectId, message: errorMessage(reason) });
    } finally {
      setCatchingUp(false);
    }
  }, [digest, load, onCaughtUp, projectId]);

  return {
    digest,
    error: error && error.projectId === projectId ? error.message : null,
    catchingUp,
    reload,
    catchUp,
  };
}
