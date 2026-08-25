import type {
  AgentExecutionProfile,
  AgentProfileSettings,
  Machine,
  ProviderId,
  SkillDefaults,
} from "./types";

export type MachineProviderPaths = Record<string, Record<ProviderId, string>>;

export interface SettingsDraft {
  version: 2;
  scope: string[];
  profiles: Partial<Record<AgentExecutionProfile, AgentProfileSettings>>;
  autoResearchInvocationCeiling?: number;
  providerPaths?: MachineProviderPaths;
  skillDefaults?: SkillDefaults;
}

export function mergeAgentProfiles(
  saved: Record<AgentExecutionProfile, AgentProfileSettings>,
  staged: SettingsDraft["profiles"],
): Record<AgentExecutionProfile, AgentProfileSettings> {
  return Object.fromEntries(
    Object.entries(saved).map(([surface, profile]) => {
      const stagedProfile = staged[surface as AgentExecutionProfile];
      const merged = { ...profile, ...stagedProfile };
      // Provider and runtime are one choice. A draft written before runtime
      // selection existed carries a staged provider and no runtime, and the
      // manifest's runtime belongs to the provider it is replacing, so keeping
      // it would build a pair the backend rejects. Empty means provider default.
      if (merged.provider !== profile.provider) {
        merged.runtime = stagedProfile?.runtime ?? "";
      }
      return [surface, merged];
    }),
  ) as Record<AgentExecutionProfile, AgentProfileSettings>;
}

export function settingsDraftStorageKey(projectId: string): string {
  return `rcp:settings-draft:${projectId}`;
}

export function serializeSettingsDraft(draft: SettingsDraft): string {
  return JSON.stringify(draft);
}

export function deserializeSettingsDraft(value: string | null): SettingsDraft | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (!isRecord(parsed) || (parsed.version !== 1 && parsed.version !== 2)) return null;
    if (!Array.isArray(parsed.scope) || parsed.scope.some((item) => typeof item !== "string"))
      return null;
    if (!isRecord(parsed.profiles)) return null;
    if (parsed.providerPaths !== undefined && !isMachineProviderPaths(parsed.providerPaths))
      return null;
    if (parsed.skillDefaults !== undefined && !isSkillDefaults(parsed.skillDefaults)) return null;

    if (parsed.version === 2) {
      if (parsed.campaignInvocationCeiling !== undefined) return null;
      if (!isInvocationCeiling(parsed.autoResearchInvocationCeiling)) return null;
      return parsed as unknown as SettingsDraft;
    }

    if (!isInvocationCeiling(parsed.campaignInvocationCeiling)) return null;
    return {
      version: 2,
      scope: parsed.scope,
      profiles: parsed.profiles as SettingsDraft["profiles"],
      ...(parsed.campaignInvocationCeiling === undefined
        ? {}
        : { autoResearchInvocationCeiling: parsed.campaignInvocationCeiling }),
      ...(parsed.providerPaths === undefined
        ? {}
        : { providerPaths: parsed.providerPaths as MachineProviderPaths }),
      ...(parsed.skillDefaults === undefined
        ? {}
        : { skillDefaults: parsed.skillDefaults as SkillDefaults }),
    };
  } catch {
    return null;
  }
}

function isInvocationCeiling(value: unknown): value is number | undefined {
  return value === undefined || (Number.isSafeInteger(value) && (value as number) >= 1);
}

export function machineProviderPathsFrom(machines: Machine[]): MachineProviderPaths {
  return Object.fromEntries(
    machines.map((machine) => [machine.alias, { ...machine.provider_paths }]),
  );
}

export function machineProviderPathUpdates(
  saved: MachineProviderPaths,
  current: MachineProviderPaths,
): MachineProviderPaths | undefined {
  const updates: MachineProviderPaths = {};
  for (const [machine, providers] of Object.entries(current)) {
    for (const [provider, path] of Object.entries(providers)) {
      if (saved[machine]?.[provider] === path) continue;
      (updates[machine] ??= {})[provider] = path;
    }
  }
  return Object.keys(updates).length ? updates : undefined;
}

export function mergeMachineProviderPaths(
  saved: MachineProviderPaths,
  staged: MachineProviderPaths | undefined,
): MachineProviderPaths {
  if (!staged) return saved;
  const merged: MachineProviderPaths = {};
  for (const machine of new Set([...Object.keys(saved), ...Object.keys(staged)])) {
    merged[machine] = { ...saved[machine], ...staged[machine] };
  }
  return merged;
}

function isMachineProviderPaths(value: unknown): value is MachineProviderPaths {
  if (!isRecord(value)) return false;
  return Object.values(value).every(
    (providers) =>
      isRecord(providers) && Object.values(providers).every((path) => typeof path === "string"),
  );
}

function isSkillDefaults(value: unknown): value is SkillDefaults {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.workflow_ids) &&
    value.workflow_ids.every((item) => typeof item === "string") &&
    Array.isArray(value.skill_ids) &&
    value.skill_ids.every((item) => typeof item === "string")
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
