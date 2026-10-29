import { useCallback, useState } from "react";
import { markDigestCaughtUp } from "../core/api";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { catchUpProjectDigest } from "./projectDigest";
import { digestProfile, useProjectResource } from "./projectResources";
import type { GraphTargetRef } from "../core/types";

export function useProjectDigest(
  projectId: string | null,
  target: GraphTargetRef,
  _freshness: string,
  onCaughtUp?: () => void,
) {
  const { store } = useRefreshRuntime();
  const profile = projectId ? digestProfile(projectId, target) : null;
  const id = profile ? resourceKeyId(profile.key) : "";
  const resource = useProjectResource(profile);
  const [failure, setFailure] = useState<{ id: string; message: string } | null>(null);
  const reload = useCallback(async () => store.invalidate(id), [store, id]);
  const digest = resource.value ?? null;
  const catchUp = useCallback(async () => {
    if (!projectId || !digest || target.kind !== "main") return;
    const mutation = store.beginMutation(
      crypto.randomUUID(),
      new Map([[id, (value: unknown) => value]]),
    );
    setFailure(null);
    try {
      await catchUpProjectDigest(projectId, digest, {
        mark: markDigestCaughtUp,
        reload: async () => mutation.settle(),
      });
      onCaughtUp?.();
    } catch (reason) {
      mutation.fail();
      setFailure({ id, message: errorMessage(reason) });
    }
  }, [projectId, digest, target.kind, store, id, onCaughtUp]);
  return {
    digest,
    loaded: profile === null || !resource.loading || resource.error != null,
    error:
      failure?.id === id ? failure.message : resource.error ? errorMessage(resource.error) : null,
    catchingUp: resource.pending.length > 0,
    reload,
    catchUp,
  };
}
