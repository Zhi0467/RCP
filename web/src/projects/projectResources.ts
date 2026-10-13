import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { api } from "../core/api";
import { graphTargetUrl } from "../core/graphTarget";
import {
  resourceKeyId,
  type ResourceStore,
  type ResourceKey,
  type ResourceProfile,
} from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import type {
  AgentQuestion,
  ConsolidationView,
  GraphTargetRef,
  Lesson,
  ProjectDigest,
  ProjectMember,
  ProjectSnapshot,
  ProviderLoginState,
  SpaceUserSummary,
} from "../core/types";

/** The runtime itself is isolated by backend and member. */
export function projectResourceKey(
  projectId: string,
  name: string,
  options: Pick<ResourceKey, "ref" | "entity" | "query"> = {},
): ResourceKey {
  return { backend: "", member: "", project: projectId, name, ...options };
}

export function projectIdFromApiBase(apiBase: string): string {
  return decodeURIComponent(apiBase.split("/api/projects/")[1]?.split("/")[0] ?? "");
}

const mountedViews = new WeakMap<ResourceStore, Map<string, number>>();

/** Shared tab interest survives hiding; the last active-view subscriber suspends it. */
export function retainProjectView<T>(
  store: ResourceStore,
  profile: ResourceProfile<T>,
): () => void {
  const id = store.register(profile.key.project, profile);
  let counts = mountedViews.get(store);
  if (!counts) {
    counts = new Map();
    mountedViews.set(store, counts);
  }
  counts.set(id, (counts.get(id) ?? 0) + 1);
  return () => {
    const remaining = (counts.get(id) ?? 1) - 1;
    if (remaining > 0) counts.set(id, remaining);
    else {
      counts.delete(id);
      if (store.isActiveProject(profile.key.project)) store.suspend(profile.key.project, [id]);
    }
  };
}

/** Tab ownership survives a hidden/unmounted view; closing the tab releases it. */
export function useProjectResource<T>(profile: ResourceProfile<T> | null) {
  const { store } = useRefreshRuntime();
  const id = profile ? resourceKeyId(profile.key) : "";
  const previous = useRef(new Map<string, { store: typeof store; id: string }>());
  useEffect(() => {
    if (!profile) return;
    const old = previous.current.get(profile.key.project);
    if (old?.store === store && old.id !== id) store.suspend(profile.key.project, [old.id]);
    previous.current.set(profile.key.project, { store, id });
    return retainProjectView(store, profile);
    // A key fully identifies the read; closures for the same key are equivalent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, id]);
  const snapshot = useMemo(() => () => store.view<T>(id), [store, id]);
  return useSyncExternalStore(store.subscribe, snapshot, snapshot);
}

export function consolidationProfile(projectId: string): ResourceProfile<ConsolidationView> {
  return {
    key: projectResourceKey(projectId, "consolidation"),
    fetch: (signal) =>
      api(`/api/projects/${encodeURIComponent(projectId)}/consolidation`, { signal }),
  };
}

export function digestProfile(
  projectId: string,
  target: GraphTargetRef,
): ResourceProfile<ProjectDigest> {
  return {
    key: projectResourceKey(projectId, "digest", {
      query: target.kind === "main" ? "main" : `branch:${target.branch_id}`,
    }),
    fetch: (signal) =>
      api(graphTargetUrl(`/api/projects/${encodeURIComponent(projectId)}/digest`, target), {
        signal,
      }),
  };
}

export function lessonsProfile(projectId: string): ResourceProfile<{ lessons: Lesson[] }> {
  return {
    key: projectResourceKey(projectId, "lessons"),
    fetch: (signal) => api(`/api/projects/${encodeURIComponent(projectId)}/lessons`, { signal }),
  };
}

export interface MembersResource {
  members: ProjectMember[];
  spaceUsers: SpaceUserSummary[];
}
export function membersProfile(projectId: string): ResourceProfile<MembersResource> {
  return {
    key: projectResourceKey(projectId, "members"),
    fetch: async (signal) => {
      const [members, spaceUsers] = await Promise.all([
        api<ProjectMember[]>(`/api/projects/${encodeURIComponent(projectId)}/members`, { signal }),
        api<SpaceUserSummary[]>("/api/space/users", { signal }),
      ]);
      return { members, spaceUsers };
    },
  };
}

export type SettingsResource = Pick<
  ProjectSnapshot,
  | "name"
  | "state_repository"
  | "run_on"
  | "default_run_truth_scope"
  | "default_auto_research_invocation_ceiling"
  | "repositories"
  | "machines"
  | "compute_connections"
  | "paper_coach"
  | "skill_catalog"
  | "skill_defaults"
  | "compute_status"
  | "provider_logins"
  | "provider_readiness"
  | "providers"
  | "provider_skill_inventories"
  | "agent_profiles"
>;
export function settingsProfile(projectId: string): ResourceProfile<SettingsResource> {
  return {
    key: projectResourceKey(projectId, "settings"),
    fetch: async (signal) => {
      const base = `/api/projects/${encodeURIComponent(projectId)}`;
      const [project, readiness] = await Promise.all([
        api<ProjectSnapshot>(base, { signal }),
        api<
          Pick<
            SettingsResource,
            | "compute_status"
            | "provider_logins"
            | "provider_readiness"
            | "providers"
            | "provider_skill_inventories"
            | "agent_profiles"
          >
        >(`${base}/readiness`, { signal }),
      ]);
      return {
        name: project.name,
        state_repository: project.state_repository,
        run_on: project.run_on,
        default_run_truth_scope: project.default_run_truth_scope,
        default_auto_research_invocation_ceiling: project.default_auto_research_invocation_ceiling,
        repositories: project.repositories,
        machines: project.machines,
        compute_connections: project.compute_connections,
        paper_coach: project.paper_coach,
        skill_catalog: project.skill_catalog,
        skill_defaults: project.skill_defaults,
        ...readiness,
      };
    },
  };
}

export function providerLoginsProfile(projectId: string): ResourceProfile<ProviderLoginState[]> {
  return {
    key: projectResourceKey(projectId, "settings", { query: "provider-logins" }),
    fetch: (signal) => api("/api/providers/logins", { signal }),
  };
}

export function questionsProfile(
  projectId: string,
  kind: "chat" | "episode",
  ownerId: string,
): ResourceProfile<AgentQuestion[]> {
  return {
    key: projectResourceKey(projectId, "questions", { query: `${kind}:${ownerId}` }),
    fetch: (signal) =>
      api(
        `/api/projects/${encodeURIComponent(projectId)}/${kind === "chat" ? "chats" : "episodes"}/${encodeURIComponent(ownerId)}/questions`,
        { signal },
      ),
  };
}
