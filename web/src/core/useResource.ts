import {
  createContext,
  useContext,
  useId,
  useLayoutEffect,
  useMemo,
  useSyncExternalStore,
} from "react";

import { RefreshScheduler, type RefreshHealth } from "./refreshScheduler";
import {
  ResourceStore,
  resourceKeyId,
  presentedResource,
  type ResourceProfile,
  type ResourcePresentation,
  type ResourceView,
} from "./resourceStore";

export interface RefreshRuntime {
  store: ResourceStore;
  scheduler: RefreshScheduler;
}

export const RefreshRuntimeContext = createContext<RefreshRuntime | null>(null);
/** The surrounding panel adds tab/target/entity identity for persistent children. */
export const ResourcePresentationContext = createContext("");

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
export function useResourcePresentation(
  owner: string,
  activationKey: string,
  ids: readonly string[],
): ResourcePresentation {
  const { store } = useRefreshRuntime();
  // Two mounted hooks may share resource interest but never presentation admission.
  const instance = useId();
  const presentationOwner = `${owner}:${instance}`;
  const identity = JSON.stringify([activationKey, ids]);
  useLayoutEffect(() => {
    const ticket = store.beginPresentation(presentationOwner, identity, ids);
    return () => store.endPresentation(ticket);
    // Resource IDs, rather than array identity, define an entry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, presentationOwner, identity]);
  const snapshot = useMemo(
    () => () => store.presentationView(presentationOwner, identity),
    [store, presentationOwner, identity],
  );
  return useSyncExternalStore(store.subscribe, snapshot, snapshot);
}

export function useResource<T>(
  owner: string,
  profile: ResourceProfile<T> | null,
  activationKey?: string,
): ResourceView<T> {
  const { store } = useRefreshRuntime();
  const contextKey = useContext(ResourcePresentationContext);
  const id = profile ? resourceKeyId(profile.key) : null;
  const presentation = useResourcePresentation(owner, activationKey ?? contextKey, id ? [id] : []);
  useLayoutEffect(() => {
    if (!profile || !id) return;
    store.register(owner, profile);
    return () => store.release(owner, [id]);
    // The key id names the profile; a new closure for the same key is not a new resource.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, owner, id]);
  return useMemo(() => presentedResource<T>(presentation), [presentation]);
}

export function useRefreshHealth(): RefreshHealth {
  const { scheduler } = useRefreshRuntime();
  return useSyncExternalStore(scheduler.subscribe, scheduler.getHealth, scheduler.getHealth);
}
