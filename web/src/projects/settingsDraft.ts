import { memberDraftKey } from "../core/draftStorage.ts";
import type { AgentExecutionProfile, AgentProfileSettings, SkillDefaults } from "../core/types";

export interface SettingsDraft {
  version: 4;
  scope: string[];
  profiles: Partial<Record<AgentExecutionProfile, AgentProfileSettings>>;
  autoResearchInvocationCeiling?: number;
  skillDefaults?: SkillDefaults;
}

export function mergeAgentProfiles(
  saved: Record<AgentExecutionProfile, AgentProfileSettings>,
  staged: SettingsDraft["profiles"],
): Record<AgentExecutionProfile, AgentProfileSettings> {
  return { ...saved, ...staged };
}

/**
 * A comparable form of one settings form state.
 *
 * Whether the form is dirty is decided by comparing text, and the two sides
 * reach it by different routes: reading a project sorts its field names, while
 * a save response returns them in declaration order. Sorting every key here
 * keeps the comparison about values, which is all the researcher changed.
 */
export function settingsFingerprint(state: unknown): string {
  return JSON.stringify(withSortedKeys(state));
}

function withSortedKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(withSortedKeys);
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((key) => [key, withSortedKeys(value[key])]),
  );
}

export function settingsDraftStorageKey(actorId: string | null, projectId: string): string | null {
  return memberDraftKey(actorId, `settings-draft:${projectId}`);
}

export function serializeSettingsDraft(draft: SettingsDraft): string {
  return JSON.stringify(draft);
}

export function deserializeSettingsDraft(value: string | null): SettingsDraft | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      !isRecord(parsed) ||
      (parsed.version !== 1 && parsed.version !== 2 && parsed.version !== 3 && parsed.version !== 4)
    )
      return null;
    if (!Array.isArray(parsed.scope) || parsed.scope.some((item) => typeof item !== "string"))
      return null;
    if (!isRecord(parsed.profiles)) return null;
    dropProfilesWithoutRuntime(parsed.profiles);
    if (parsed.skillDefaults !== undefined && !isSkillDefaults(parsed.skillDefaults)) return null;
    // Machine settings once staged here; the space machine card saves them
    // directly now. v3 also briefly persisted compute connections. Drop all of
    // them on read, regardless of shape.
    for (const key of [
      "providerPaths",
      "machineCompute",
      "machineComputeEdits",
      "accessHintEdits",
      "computeConnections",
    ])
      delete parsed[key];

    if (parsed.version === 4) {
      if (parsed.campaignInvocationCeiling !== undefined) return null;
      if (!isInvocationCeiling(parsed.autoResearchInvocationCeiling)) return null;
      return parsed as unknown as SettingsDraft;
    }

    if (parsed.version === 2 || parsed.version === 3) {
      if (parsed.campaignInvocationCeiling !== undefined) return null;
      if (!isInvocationCeiling(parsed.autoResearchInvocationCeiling)) return null;
      return {
        ...(parsed as unknown as Omit<SettingsDraft, "version">),
        version: 4,
      };
    }

    if (!isInvocationCeiling(parsed.campaignInvocationCeiling)) return null;
    return {
      version: 4,
      scope: parsed.scope,
      profiles: parsed.profiles as SettingsDraft["profiles"],
      ...(parsed.campaignInvocationCeiling === undefined
        ? {}
        : { autoResearchInvocationCeiling: parsed.campaignInvocationCeiling }),
      ...(parsed.skillDefaults === undefined
        ? {}
        : { skillDefaults: parsed.skillDefaults as SkillDefaults }),
    };
  } catch {
    return null;
  }
}

/**
 * A draft written before runtime selection carries a provider and no runtime.
 * Provider and runtime are one choice, and nothing here can name the missing
 * half, so the incomplete profile is dropped and the manifest's own values are
 * restored for that surface. Staging an empty runtime instead would show the
 * researcher a nameless blank where a real runtime belongs.
 */
function dropProfilesWithoutRuntime(profiles: Record<string, unknown>): void {
  for (const [surface, profile] of Object.entries(profiles)) {
    if (!isRecord(profile) || typeof profile.runtime !== "string" || !profile.runtime) {
      delete profiles[surface];
    }
  }
}

function isInvocationCeiling(value: unknown): value is number | undefined {
  return value === undefined || (Number.isSafeInteger(value) && (value as number) >= 1);
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
