import type { AgentRunConfig, AgentSurface, Machine, ProviderId } from "./types";

export type MachineProviderPaths = Record<string, Record<ProviderId, string>>;

export interface SettingsDraft {
  version: 1;
  scope: string[];
  profiles: Record<AgentSurface, AgentRunConfig>;
  providerPaths?: MachineProviderPaths;
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
    if (!isRecord(parsed) || parsed.version !== 1) return null;
    if (!Array.isArray(parsed.scope) || parsed.scope.some((item) => typeof item !== "string"))
      return null;
    if (!isRecord(parsed.profiles)) return null;
    if (parsed.providerPaths !== undefined && !isMachineProviderPaths(parsed.providerPaths))
      return null;
    return parsed as unknown as SettingsDraft;
  } catch {
    return null;
  }
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
