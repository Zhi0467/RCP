import { useLayoutEffect, useMemo, useRef } from "react";
import { resourceKeyId, type ResourceProfile, type ResourceStore } from "../core/resourceStore";

export class ProjectTabResources {
  readonly tabs = new Set<string>();
  readonly views = new Map<string, string[]>();

  constructor(
    readonly store: ResourceStore,
    readonly profiles: Map<string, ResourceProfile<unknown>[]>,
  ) {}

  select(projectId: string, profiles: ResourceProfile<unknown>[]) {
    const previous = new Set(this.views.get(projectId) ?? []);
    const ids = profiles.map((profile) => {
      const id = resourceKeyId(profile.key);
      if (!previous.has(id)) this.store.register(projectId, profile);
      return id;
    });
    this.store.suspend(
      projectId,
      [...previous].filter((id) => !ids.includes(id)),
    );
    this.profiles.set(projectId, profiles);
    this.views.set(projectId, ids);
  }

  restore() {
    // Effect replay can follow a reset of the same runtime (React StrictMode).
    this.tabs.clear();
    this.views.clear();
    for (const [projectId, current] of this.profiles) {
      this.views.set(
        projectId,
        current.map((profile) => this.store.register(projectId, profile)),
      );
    }
  }
}

export function useProjectTabResources(store: ResourceStore) {
  const profiles = useRef(new Map<string, ResourceProfile<unknown>[]>());
  const registrations = useMemo(() => new ProjectTabResources(store, profiles.current), [store]);
  const previousStore = useRef(store);
  useLayoutEffect(() => {
    if (previousStore.current !== store) {
      for (const projectId of profiles.current.keys()) {
        if (previousStore.current.requests(projectId).length === 0)
          profiles.current.delete(projectId);
      }
      previousStore.current = store;
    }
    registrations.restore();
  }, [registrations, store]);
  return registrations;
}
