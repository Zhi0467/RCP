import {
  Check,
  GitBranch,
  HardDrive,
  LoaderCircle,
  Minus,
  Plus,
  RotateCcw,
  ScanSearch,
  Save,
  Server,
  Trash2,
  TriangleAlert,
  Type,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, clearProjectCaches } from "../api";
import { AgentConfigControls, profileRunConfig } from "../components/AgentConfigControls";
import { AgentUsageWidgets } from "../components/AgentUsageWidgets";
import {
  deserializeSettingsDraft,
  machineProviderPathUpdates,
  machineProviderPathsFrom,
  mergeMachineProviderPaths,
  serializeSettingsDraft,
  settingsDraftStorageKey,
  type MachineProviderPaths,
} from "../settingsDraft";
import { TEXT_SCALE_MAX, TEXT_SCALE_MIN } from "../textScale";
import type {
  AgentRunConfig,
  AgentSurface,
  AgentUsageSnapshot,
  CacheMetric,
  ProjectCacheMetrics,
  ProjectSettingsRequest,
  ProjectSnapshot,
  ProviderId,
  ProviderPathResolution,
  ProviderReadiness,
} from "../types";

interface Props {
  apiBase: string;
  project: ProjectSnapshot;
  usage: AgentUsageSnapshot | null;
  onRefreshUsage: () => Promise<void>;
  cacheClearDisabled: boolean;
  writesDisabled?: boolean;
  onSaved: (project: ProjectSnapshot, preserveReadiness?: boolean) => void;
  onCacheMetricsChange: (metrics: ProjectCacheMetrics) => void;
  onRefreshReadiness: () => Promise<void>;
  showDisplaySettings: boolean;
  textScale: number;
  onTextScaleChange: (action: "decrease" | "increase" | "reset") => void;
}

const surfaces: Array<{ id: AgentSurface; label: string }> = [
  { id: "seed", label: "Seed" },
  { id: "refresh", label: "Refresh" },
  { id: "node_chat", label: "Node chat" },
  { id: "project_chat", label: "Project chat" },
  { id: "paper_coach", label: "Paper coach" },
];

function profilesFrom(project: ProjectSnapshot): Record<AgentSurface, AgentRunConfig> {
  const canonicalMachine =
    project.repositories.find((repository) => repository.alias === project.state_repository)
      ?.machine ?? project.run_on;
  return Object.fromEntries(
    surfaces.map(({ id }) => {
      const profile = profileRunConfig(project.agent_profiles[id]);
      return [id, id === "paper_coach" ? profile : { ...profile, run_on: canonicalMachine }];
    }),
  ) as Record<AgentSurface, AgentRunConfig>;
}

/** The staged edits for this project, or the manifest's values when none exist. */
function stagedOrSaved(project: ProjectSnapshot) {
  const saved = {
    scope: project.default_run_truth_scope,
    profiles: profilesFrom(project),
    providerPaths: machineProviderPathsFrom(project.machines),
  };
  const staged = deserializeSettingsDraft(
    localStorage.getItem(settingsDraftStorageKey(project.id)),
  );
  if (!staged) return saved;
  // Merge over the manifest's profiles so a surface added since the draft was
  // written is still present.
  return {
    scope: staged.scope,
    profiles: { ...saved.profiles, ...staged.profiles },
    providerPaths: mergeMachineProviderPaths(saved.providerPaths, staged.providerPaths),
  };
}

export function ProjectSettings({
  apiBase,
  project,
  usage,
  onRefreshUsage,
  cacheClearDisabled,
  writesDisabled = false,
  onSaved,
  onCacheMetricsChange,
  onRefreshReadiness,
  showDisplaySettings,
  textScale,
  onTextScaleChange,
}: Props) {
  const [scope, setScope] = useState<string[]>(() => stagedOrSaved(project).scope);
  const [profiles, setProfiles] = useState<Record<AgentSurface, AgentRunConfig>>(
    () => stagedOrSaved(project).profiles,
  );
  const [providerPaths, setProviderPaths] = useState<MachineProviderPaths>(
    () => stagedOrSaved(project).providerPaths,
  );
  const [saving, setSaving] = useState(false);
  const [clearingCaches, setClearingCaches] = useState(false);
  const [resolvingProvider, setResolvingProvider] = useState<string | null>(null);
  const [cacheMetrics, setCacheMetrics] = useState(project.cache_metrics);
  const [status, setStatus] = useState<{ kind: "saved" | "error"; text: string } | null>(null);

  // Reload the form only when the project itself changes. Keying this on the
  // whole snapshot discarded in-progress edits every time an unrelated refresh
  // — a finished background run, a cache clear — handed down a new object
  // carrying byte-identical settings.
  useEffect(() => {
    const restored = stagedOrSaved(project);
    setScope(restored.scope);
    setProfiles(restored.profiles);
    setProviderPaths(restored.providerPaths);
  }, [project.id]);

  // Cache metrics are server-owned, so they follow every snapshot.
  useEffect(() => {
    setCacheMetrics(project.cache_metrics);
  }, [project.cache_metrics]);

  useEffect(() => {
    void onRefreshUsage();
  }, [onRefreshUsage]);

  const baseline = useMemo(
    () =>
      JSON.stringify({
        scope: project.default_run_truth_scope,
        profiles: profilesFrom(project),
        providerPaths: machineProviderPathsFrom(project.machines),
      }),
    [project],
  );
  const current = JSON.stringify({ scope, profiles, providerPaths });
  const dirty = current !== baseline;

  // Stage every edit locally so navigating away, or reloading, never loses it.
  // Clearing on a clean form is what makes Save and Reset drop the staged copy.
  useEffect(() => {
    const key = settingsDraftStorageKey(project.id);
    if (dirty) {
      localStorage.setItem(
        key,
        serializeSettingsDraft({ version: 1, scope, profiles, providerPaths }),
      );
    } else {
      localStorage.removeItem(key);
    }
  }, [dirty, current, project.id]);
  const machineByAlias = Object.fromEntries(
    project.machines.map((machine) => [machine.alias, machine]),
  );
  const providerCatalog = Object.values(project.providers).sort((left, right) =>
    (left.label || left.provider).localeCompare(right.label || right.provider),
  );

  const toggleRepository = (alias: string) => {
    setStatus(null);
    setScope((currentScope) => {
      if (!currentScope.includes(alias)) return [...currentScope, alias];
      if (currentScope.length === 1) {
        setStatus({ kind: "error", text: "Keep at least one repository in the default read set." });
        return currentScope;
      }
      return currentScope.filter((item) => item !== alias);
    });
  };

  const reset = () => {
    setScope(project.default_run_truth_scope);
    setProfiles(profilesFrom(project));
    setProviderPaths(machineProviderPathsFrom(project.machines));
    setStatus(null);
  };

  const save = async () => {
    if (!dirty || saving || writesDisabled) return;
    setSaving(true);
    setStatus(null);
    const body: ProjectSettingsRequest = {
      default_run_truth_scope: scope,
      agent_profiles: profiles,
    };
    const pathUpdates = machineProviderPathUpdates(
      machineProviderPathsFrom(project.machines),
      providerPaths,
    );
    if (pathUpdates) body.machine_provider_paths = pathUpdates;
    try {
      const saved = await api<ProjectSnapshot>(`${apiBase}/settings`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      setProviderPaths(machineProviderPathsFrom(saved.machines));
      onSaved(saved);
      try {
        await onRefreshReadiness();
        setStatus({ kind: "saved", text: "Saved." });
      } catch (readinessError) {
        setStatus({
          kind: "error",
          text: `Saved, but readiness refresh failed: ${readinessError instanceof Error ? readinessError.message : String(readinessError)}`,
        });
      }
    } catch (caught) {
      setStatus({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setSaving(false);
    }
  };

  const resolveProviderPath = async (machine: string, provider: ProviderId) => {
    const key = `${machine}:${provider}`;
    if (resolvingProvider || writesDisabled) return;
    setResolvingProvider(key);
    setStatus(null);
    try {
      const result = await api<ProviderPathResolution>(
        `${apiBase}/machines/${encodeURIComponent(machine)}/providers/${encodeURIComponent(provider)}/resolve`,
        { method: "POST" },
      );
      setProviderPaths((currentPaths) => ({
        ...currentPaths,
        [result.machine]: {
          ...currentPaths[result.machine],
          [result.provider]: result.binary_path ?? "",
        },
      }));
      const coachMachine = project.agent_profiles.paper_coach.run_on;
      const resolvedProject: ProjectSnapshot = {
        ...result.project,
        provider_readiness: {
          ...project.provider_readiness,
          [result.machine]: {
            ...project.provider_readiness[result.machine],
            [result.provider]: result.readiness,
          },
        },
        providers:
          coachMachine === result.machine
            ? { ...project.providers, [result.provider]: result.readiness }
            : project.providers,
      };
      onSaved(resolvedProject, false);
      setStatus({
        kind: "saved",
        text: `${result.readiness.label || result.provider} resolved on ${result.machine}.`,
      });
    } catch (caught) {
      setStatus({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setResolvingProvider(null);
    }
  };

  const clearCaches = async () => {
    if (cacheClearDisabled || clearingCaches) return;
    setClearingCaches(true);
    setStatus(null);
    try {
      const metrics = await clearProjectCaches(apiBase);
      setCacheMetrics(metrics);
      onCacheMetricsChange(metrics);
      setStatus({ kind: "saved", text: "Caches cleared." });
    } catch (caught) {
      setStatus({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setClearingCaches(false);
    }
  };

  return (
    <section className="settings-page">
      <AgentUsageWidgets usage={usage} providers={project.providers} />

      {showDisplaySettings && (
        <section className="settings-section display-settings">
          <header>
            <span>
              <Type size={16} />
            </span>
            <h2>Display</h2>
            <div className="text-scale-controls" role="group" aria-label="Interface text size">
              <button
                className="icon-button"
                type="button"
                disabled={textScale <= TEXT_SCALE_MIN}
                onClick={() => onTextScaleChange("decrease")}
                aria-label="Decrease text size"
              >
                <Minus size={15} />
              </button>
              <button
                className="text-scale-value"
                type="button"
                onClick={() => onTextScaleChange("reset")}
                aria-label="Reset text size to 100 percent"
              >
                {textScale}%
              </button>
              <button
                className="icon-button"
                type="button"
                disabled={textScale >= TEXT_SCALE_MAX}
                onClick={() => onTextScaleChange("increase")}
                aria-label="Increase text size"
              >
                <Plus size={15} />
              </button>
            </div>
          </header>
        </section>
      )}
      <article className="settings-section boundary-settings">
        <header>
          <span>
            <GitBranch size={16} />
          </span>
          <h2>Project boundary</h2>
        </header>
        <div className="settings-repositories">
          {project.repositories.map((repository) => {
            const machine = machineByAlias[repository.machine];
            const selected = scope.includes(repository.alias);
            const canonical = repository.alias === project.state_repository;
            return (
              <label
                className={selected ? "settings-repository selected" : "settings-repository"}
                key={repository.alias}
              >
                <input
                  type="checkbox"
                  checked={selected}
                  disabled={writesDisabled}
                  onChange={() => toggleRepository(repository.alias)}
                />
                <span className="settings-check">{selected && <Check size={12} />}</span>
                <span className="settings-repository-copy">
                  <strong>{repository.alias}</strong>
                  <span className="settings-repository-path">
                    {machine?.host ? `${machine.host}:${repository.path}` : repository.path}
                  </span>
                </span>
                <span className="settings-repository-meta">
                  <Server size={12} /> {machine?.host ? repository.machine : "local"}
                  {canonical && <em>canonical state</em>}
                </span>
              </label>
            );
          })}
        </div>
      </article>

      <section className="settings-section provider-path-settings">
        <header>
          <span>
            <Server size={16} />
          </span>
          <h2>Provider executables</h2>
        </header>
        <div className="provider-machine-list">
          {project.machines.map((machine) => (
            <article className="provider-machine" key={machine.alias}>
              <header>
                <strong>{machine.alias}</strong>
                <span>{machine.host || "This Mac"}</span>
              </header>
              <div className="provider-path-list">
                {providerCatalog.map((provider) => {
                  const recorded = machine.provider_paths[provider.provider] ?? "";
                  const value = providerPaths[machine.alias]?.[provider.provider] ?? "";
                  const readiness = project.provider_readiness[machine.alias]?.[provider.provider];
                  const state = providerPathPresentation(readiness, value, recorded);
                  const resolveKey = `${machine.alias}:${provider.provider}`;
                  return (
                    <div className="provider-path-row" key={provider.provider}>
                      <strong>{provider.label || provider.provider}</strong>
                      <input
                        type="text"
                        aria-label={`${provider.label || provider.provider} executable on ${machine.alias}`}
                        value={value}
                        disabled={writesDisabled}
                        onChange={(event) => {
                          const path = event.target.value;
                          setProviderPaths((currentPaths) => ({
                            ...currentPaths,
                            [machine.alias]: {
                              ...currentPaths[machine.alias],
                              [provider.provider]: path,
                            },
                          }));
                          setStatus(null);
                        }}
                      />
                      <span className={`provider-path-state ${state.kind}`}>{state.label}</span>
                      <button
                        className="button secondary compact"
                        type="button"
                        disabled={writesDisabled || Boolean(resolvingProvider)}
                        onClick={() => void resolveProviderPath(machine.alias, provider.provider)}
                      >
                        {resolvingProvider === resolveKey ? (
                          <LoaderCircle className="spin" size={13} />
                        ) : (
                          <ScanSearch size={13} />
                        )}
                        Resolve
                      </button>
                    </div>
                  );
                })}
              </div>
            </article>
          ))}
        </div>
      </section>

      <div className="settings-section agent-defaults">
        <header className="agent-defaults-heading">
          <div>
            <h2>Agent defaults</h2>
          </div>
        </header>
        <div className="settings-agent-list">
          {surfaces.map(({ id, label }) => (
            <article className="settings-agent" key={id}>
              <header>
                <span>
                  <strong>{label}</strong>
                </span>
                <span>{id === "paper_coach" ? "read-only coach" : "graph patch only"}</span>
              </header>
              <AgentConfigControls
                project={project}
                value={profiles[id]}
                locked={writesDisabled}
                runOnLocked={id !== "paper_coach"}
                onRefreshReadiness={onRefreshReadiness}
                onChange={(value) => {
                  setProfiles((currentProfiles) => ({ ...currentProfiles, [id]: value }));
                  setStatus(null);
                }}
              />
            </article>
          ))}
        </div>
      </div>

      <section className="settings-section cache-settings">
        <header>
          <span>
            <HardDrive size={16} />
          </span>
          <h2>Storage</h2>
          <button
            className="button secondary compact"
            disabled={cacheClearDisabled || clearingCaches}
            aria-label="Clear rebuildable caches"
            onClick={() => void clearCaches()}
          >
            {clearingCaches ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}
            {clearingCaches ? "Clearing" : "Clear"}
          </button>
        </header>
        <div className="cache-meter-list">
          <CacheMeter label="Remote sources" metric={cacheMetrics.remote_sources} />
          <CacheMeter label="Session slices" metric={cacheMetrics.session_slices} />
        </div>
      </section>

      <footer className="settings-savebar">
        <div className={status ? `settings-save-status ${status.kind}` : "settings-save-status"}>
          {status?.kind === "error" && <TriangleAlert size={15} />}
          {status?.kind === "saved" && <Check size={15} />}
          <span>
            {status?.text ||
              (dirty ? "Unsaved manifest changes" : "Manifest matches these defaults")}
          </span>
        </div>
        <button className="button secondary" disabled={!dirty || saving} onClick={reset}>
          <RotateCcw size={14} /> Reset
        </button>
        <button
          className="button primary"
          disabled={writesDisabled || !dirty || saving}
          onClick={() => void save()}
        >
          {saving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}
          {saving ? "Saving" : "Save"}
        </button>
      </footer>
    </section>
  );
}

export function providerPathPresentation(
  readiness: ProviderReadiness | undefined,
  value: string,
  recorded: string,
): { label: string; kind: "ready" | "warning" | "error" | "pending" } {
  if (value !== recorded) return { label: "Unsaved", kind: "pending" };
  if (readiness?.path_state === "unreachable")
    return { label: "Machine unreachable", kind: "error" };
  if (readiness?.path_state === "denied") return { label: "Recorded path unusable", kind: "error" };
  if (readiness?.path_state === "missing") {
    return { label: value ? "Recorded path missing" : "Executable missing", kind: "error" };
  }
  if (readiness?.path_state === "resolved") return { label: "Ready", kind: "ready" };
  return { label: "Not recorded", kind: "warning" };
}

function CacheMeter({ label, metric }: { label: string; metric: CacheMetric }) {
  const byteRatio = metric.limits.max_bytes > 0 ? metric.bytes / metric.limits.max_bytes : 0;
  const countRatio = metric.limits.max_count > 0 ? metric.count / metric.limits.max_count : 0;
  const ratio = Math.min(1, Math.max(byteRatio, countRatio));
  return (
    <div className="cache-meter">
      <div className="cache-meter-heading">
        <strong>{label}</strong>
        <span>
          {formatBytes(metric.bytes)} / {formatBytes(metric.limits.max_bytes)}
        </span>
      </div>
      <div
        className="cache-meter-track"
        role="progressbar"
        aria-label={`${label} cache usage`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(ratio * 100)}
      >
        <span style={{ width: `${ratio * 100}%` }} />
      </div>
      <div className="cache-meter-meta">
        <span>
          <em>Items</em>
          {metric.count} / {metric.limits.max_count}
        </span>
        <span>
          <em>TTL</em>
          {formatDuration(metric.limits.ttl_seconds)}
        </span>
        <span>
          <em>Reclaim</em>
          {metric.reclaimable_count} · {formatBytes(metric.reclaimable_bytes)}
        </span>
        <span>
          <em>Oldest</em>
          {metric.oldest_accessed_at
            ? new Date(metric.oldest_accessed_at).toLocaleDateString()
            : "—"}
        </span>
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function formatDuration(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  return `${seconds}s`;
}
