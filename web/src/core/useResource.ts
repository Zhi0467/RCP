import { createContext, useContext, useEffect, useMemo, useSyncExternalStore } from "react";

import { RefreshScheduler, type RefreshHealth } from "./refreshScheduler";
import {
  ResourceStore,
  resourceKeyId,
  type ResourceProfile,
  type ResourceView,
} from "./resourceStore";

export interface RefreshRuntime {
  store: ResourceStore;
  scheduler: RefreshScheduler;
}

export const RefreshRuntimeContext = createContext<RefreshRuntime | null>(null);

export function useRefreshRuntime(): RefreshRuntime {
  const runtime = useContext(RefreshRuntimeContext);
  if (!runtime) throw new Error("RefreshRuntimeContext is missing");
  return runtime;
}

/**
 * Reads one resource for `owner` (a project tab id). The interest lasts while
 * the component is mounted; tab-level interests that must outlive a view are
 * registered by the tab owner with `store.register` directly.
 */
export function useResource<T>(owner: string, profile: ResourceProfile<T> | null): ResourceView<T> {
  const { store } = useRefreshRuntime();
  const id = profile ? resourceKeyId(profile.key) : null;
  useEffect(() => {
    if (!profile || !id) return;
    store.register(owner, profile);
    return () => store.release(owner, [id]);
    // The key id names the profile; a new closure for the same key is not a new resource.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, owner, id]);
  const getSnapshot = useMemo(() => () => store.view<T>(id ?? ""), [store, id]);
  return useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot);
}

export function useRefreshHealth(): RefreshHealth {
  const { scheduler } = useRefreshRuntime();
  return useSyncExternalStore(scheduler.subscribe, scheduler.getHealth, scheduler.getHealth);
}
