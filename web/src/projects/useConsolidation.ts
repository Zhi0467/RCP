import { useCallback } from "react";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { consolidationProfile, projectIdFromApiBase, useProjectResource } from "./projectResources";

export function useConsolidation(apiBase: string, _freshness: string) {
  const { store } = useRefreshRuntime();
  const profile = apiBase ? consolidationProfile(projectIdFromApiBase(apiBase)) : null;
  const id = profile ? resourceKeyId(profile.key) : "";
  const resource = useProjectResource(profile);
  const refresh = useCallback(() => {
    store.beginMutation(crypto.randomUUID(), new Map([[id, (value: unknown) => value]])).settle();
  }, [store, id]);
  return {
    consolidation: resource.value ?? null,
    error: resource.error ? errorMessage(resource.error) : null,
    refresh,
  };
}
