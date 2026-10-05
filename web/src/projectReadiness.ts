import type { loadProjectReadiness } from "./core/api";
import type { ProjectReadinessRetention } from "./graph/projectSession";
import type { ProjectSnapshot } from "./core/types";

export const PROVIDER_SKILL_READINESS_POLL_DELAY_MS = 1_000;
const PROVIDER_SKILL_READINESS_MAX_FOLLOW_UPS = 20;

export interface ProviderReadinessRequestState {
  pending: boolean;
  providerError: string | null;
  computeError: string | null;
}

export type ProjectReadinessSnapshot = Awaited<ReturnType<typeof loadProjectReadiness>>;

export interface ProviderReadinessInFlight {
  refresh: boolean;
  generation: ProjectReadinessGeneration;
  request: Promise<ProjectReadinessSnapshot | null>;
}

/** Separate request generations for the provider and compute readiness slices. */
export interface ProjectReadinessGeneration {
  provider: number;
  compute: number;
}

const INITIAL_PROJECT_READINESS_GENERATION: ProjectReadinessGeneration = {
  provider: 0,
  compute: 0,
};

export function currentProjectReadinessGeneration(
  generations: ReadonlyMap<string, ProjectReadinessGeneration>,
  projectId: string,
): ProjectReadinessGeneration {
  return generations.get(projectId) ?? INITIAL_PROJECT_READINESS_GENERATION;
}

/** Advance the request generation of every readiness slice this retention drops. */
export function invalidateProjectReadinessGenerations(
  generations: Map<string, ProjectReadinessGeneration>,
  projectId: string,
  retention: ProjectReadinessRetention,
): ProjectReadinessGeneration {
  const current = currentProjectReadinessGeneration(generations, projectId);
  const next = {
    provider: current.provider + (retention.provider ? 0 : 1),
    compute: current.compute + (retention.compute ? 0 : 1),
  };
  generations.set(projectId, next);
  return next;
}

/**
 * Which slices of one readiness response are still current.
 *
 * A resolve invalidates provider readiness alone, so an in-flight compute probe
 * that answers afterwards still carries the live matrix.
 */
export function projectReadinessResponseApplies(
  generations: ReadonlyMap<string, ProjectReadinessGeneration>,
  projectId: string,
  requestGeneration: ProjectReadinessGeneration,
): ProjectReadinessRetention {
  const current = currentProjectReadinessGeneration(generations, projectId);
  return {
    provider: current.provider === requestGeneration.provider,
    compute: current.compute === requestGeneration.compute,
  };
}

export function projectReadinessUpdate(
  readiness: ProjectReadinessSnapshot,
  applies: ProjectReadinessRetention,
): Partial<ProjectReadinessSnapshot> {
  return {
    ...(applies.compute ? { compute_status: readiness.compute_status } : {}),
    ...(applies.provider
      ? {
          provider_logins: readiness.provider_logins,
          provider_readiness: readiness.provider_readiness,
          providers: readiness.providers,
          provider_skill_inventories: readiness.provider_skill_inventories,
          // Profiles resolve their unnamed model against this catalog, so a
          // refresh that changes the head re-exports them with it.
          agent_profiles: readiness.agent_profiles,
        }
      : {}),
  };
}

/**
 * Whether one failed readiness response may still write shared request state.
 *
 * A failure carries no slice data, so it speaks for the project only while it
 * is the registered request. A replaced request must stay silent: the `finally`
 * that clears `pending` runs only for the registered request, so a late failure
 * would otherwise leave readiness controls disabled until reload.
 */
export function projectReadinessFailureApplies(
  registered: boolean,
  applies: ProjectReadinessRetention,
): boolean {
  return registered && (applies.provider || applies.compute);
}

/**
 * The request state after one failed readiness response.
 *
 * The failure reports only for the slices this request still owns. A slice a
 * later edit superseded keeps whatever its own newer decision left behind.
 * `pending` stays true because only a registered request reaches here.
 */
export function projectReadinessFailureState(
  previous: ProviderReadinessRequestState | undefined,
  applies: ProjectReadinessRetention,
  message: string,
): ProviderReadinessRequestState {
  return {
    pending: true,
    providerError: applies.provider ? message : (previous?.providerError ?? null),
    computeError: applies.compute ? message : (previous?.computeError ?? null),
  };
}

export function shouldPollProviderSkillReadiness(
  inventories: ProjectSnapshot["provider_skill_inventories"] | undefined,
  completedFollowUps: number,
): boolean {
  return (
    inventories !== undefined &&
    completedFollowUps < PROVIDER_SKILL_READINESS_MAX_FOLLOW_UPS &&
    Object.values(inventories).some((providers) =>
      Object.values(providers).some((inventory) => inventory?.status === "refreshing"),
    )
  );
}

export function shouldRequestProviderReadiness(
  readiness: ProjectSnapshot["provider_readiness"],
  pending: boolean,
): boolean {
  return (
    !pending && !Object.values(readiness).some((providers) => Object.keys(providers).length > 0)
  );
}
