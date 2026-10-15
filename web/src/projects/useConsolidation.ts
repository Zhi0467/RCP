import type { ConsolidationView, EvidenceFlag } from "../core/types";
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
  const updateFlag = useCallback(
    (flag: EvidenceFlag) => {
      const current = store.view<ConsolidationView>(id).value;
      if (current)
        store.write(id, {
          ...current,
          evidence_flags: current.evidence_flags
            .map((item) => (item.flag_id === flag.flag_id ? flag : item))
            .filter((item) => item.state !== "dismissed"),
        });
    },
    [store, id],
  );
  return {
    updateFlag,
    consolidation: resource.value ?? null,
    error: resource.error ? errorMessage(resource.error) : null,
    refresh,
  };
}
