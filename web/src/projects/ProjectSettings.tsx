import {
  Bell,
  BookOpen,
  Bot,
  Check,
  Gauge,
  GitBranch,
  HardDrive,
  LoaderCircle,
  Moon,
  RotateCcw,
  ScanSearch,
  Save,
  Server,
  Sparkles,
  Trash2,
  TriangleAlert,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, clearProjectCaches, removeProjectMachine } from "../core/api";
import { computeProbePresentation } from "../experiments/compute";
import { RepositoryRequests } from "./RepositoryRequests";
import { ProjectMembers } from "./ProjectMembers";
import { MachineCard } from "./MachineCard";
import { ProjectNotifications } from "../desktop/ProjectNotifications";
import { ProjectConsolidation } from "./ProjectConsolidation";
import { AddMachineTile, MachineTile, type MachineSignal } from "./MachineTile";
import { AddProjectMachine } from "./AddProjectMachine";
import { useSpaceMachines } from "./useSpaceMachines";
import { machineHostLabel, spaceMachineById } from "./spaceMachines";
import { EMPTY_SKILL_SELECTION } from "../core/skillPickerModel";
import { AgentConfigControls, profileRunConfig } from "../core/AgentConfigControls";
import { AgentUsageWidgets } from "../agents/AgentUsageWidgets";
import { SkillPackageInspector } from "../agents/SkillPackageInspector";
import {
  deserializeSettingsDraft,
  mergeAgentProfiles,
  serializeSettingsDraft,
  settingsDraftStorageKey,
  settingsFingerprint,
} from "./settingsDraft";
import { providerPathPresentation } from "./providerPaths";
import { SettingsRail, type SettingsSection } from "../ui/SettingsRail";
import { hasRow, rowOf } from "../core/agentRows";
import type { ProjectReadinessRetention } from "../graph/projectSession";
import type {
  AgentExecutionProfile,
  AgentProfileSettings,
  AgentUsageSnapshot,
  CacheMetric,
  ConsolidationView,
  ProjectCacheMetrics,
  ProjectSettingsRequest,
  IdentityResponse,
  ProjectSnapshot,
  SkillDefaults,
} from "../core/types";

interface Props {
  apiBase: string;
  project: ProjectSnapshot;
  identity: IdentityResponse | null;
  onLeftProject: () => void;
  usage: AgentUsageSnapshot | null;
  onRefreshUsage: () => Promise<void>;
  cacheClearDisabled: boolean;
  writesDisabled?: boolean;
  onSaved: (project: ProjectSnapshot, retention?: ProjectReadinessRetention) => void;
  onCacheMetricsChange: (metrics: ProjectCacheMetrics) => void;
  onRefreshReadiness: () => Promise<void>;
  readinessRequest?: {
    pending: boolean;
    providerError: string | null;
  };
  spaceKind: "personal" | "team";
  onMovePersonalProjectToTeam?: (sourceProjectId: string) => void;
  consolidation: ConsolidationView | null;
  consolidationError: string | null;
  onConsolidationChanged: () => void;
  /** The pane to open on; absent or unknown opens the first. */
  initialSection?: string;
}

export function publishCacheMetrics(
  metrics: ProjectCacheMetrics,
  setVisibleMetrics: (metrics: ProjectCacheMetrics) => void,
  onCacheMetricsChange: (metrics: ProjectCacheMetrics) => void,
) {
  setVisibleMetrics(metrics);
  onCacheMetricsChange(metrics);
}

const executionProfiles: Array<{ id: AgentExecutionProfile; label: string }> = [
  { id: "seed", label: "Seed and refresh" },
  { id: "refresh", label: "Refresh" },
  { id: "node_chat", label: "Node chat" },
  { id: "project_chat", label: "Project chat" },
  { id: "paper_coach", label: "Paper coach" },
  { id: "orchestrator", label: "Auto-research orchestrator" },
];
const executionRows = executionProfiles.filter(({ id }) => hasRow(id));

/** Every agent runs on the base: the machine that holds the canonical state repository. */
function baseMachine(project: ProjectSnapshot): string {
  return (
    project.repositories.find((repository) => repository.alias === project.state_repository)
      ?.machine ?? project.run_on
  );
}

function profilesFrom(
  project: ProjectSnapshot,
): Record<AgentExecutionProfile, AgentProfileSettings> {
  const base = baseMachine(project);
  return Object.fromEntries(
    executionProfiles.map(({ id }) => {
      const storedProfile =
        id === "orchestrator"
          ? (project.agent_profiles.orchestrator ?? project.agent_profiles.refresh)
          : project.agent_profiles[id];
      const { run_on: _runOn, ...settings } = profileRunConfig(storedProfile, base);
      return [id, { ...settings, runtime: storedProfile.runtime }];
    }),
  ) as Record<AgentExecutionProfile, AgentProfileSettings>;
}

type SkillCatalogEntry = ProjectSnapshot["skill_catalog"][number];

function skillDefaultsFrom(project: ProjectSnapshot): SkillDefaults {
  return project.skill_defaults ?? EMPTY_SKILL_SELECTION;
}

function skillCatalogFrom(project: ProjectSnapshot): SkillCatalogEntry[] {
  return project.skill_catalog ?? [];
}

/** The staged edits for this project, or the manifest's values when none exist. */
function stagedOrSaved(project: ProjectSnapshot, actorId: string | null) {
  const saved = {
    autoResearchInvocationCeiling: project.default_auto_research_invocation_ceiling,
    profiles: profilesFrom(project),
    skillDefaults: skillDefaultsFrom(project),
  };
  let staged: ReturnType<typeof deserializeSettingsDraft> = null;
  const key = settingsDraftStorageKey(actorId, project.id);
  try {
    if (key) staged = deserializeSettingsDraft(localStorage.getItem(key));
  } catch {
    // A staged draft is a convenience; storage failures fall back to the manifest.
  }
  if (!staged) return saved;
  // Merge over the manifest's profiles so a surface added since the draft was
  // written is still present.
  return {
    autoResearchInvocationCeiling:
      staged.autoResearchInvocationCeiling ?? saved.autoResearchInvocationCeiling,
    profiles: mergeAgentProfiles(saved.profiles, staged.profiles),
    skillDefaults: staged.skillDefaults ?? saved.skillDefaults,
  };
}

export function ProjectSettings({
  apiBase,
  project,
  identity,
  onLeftProject,
  usage,
  onRefreshUsage,
  cacheClearDisabled,
  writesDisabled = false,
  onSaved,
  onCacheMetricsChange,
  onRefreshReadiness,
  readinessRequest,
  spaceKind,
  onMovePersonalProjectToTeam,
  consolidation,
  consolidationError,
  onConsolidationChanged,
  initialSection,
}: Props) {
  const skillCatalog = skillCatalogFrom(project);
  const savedSkillDefaults = skillDefaultsFrom(project);
  const actorId = identity?.user.user_id ?? null;
  // eslint-disable-next-line react-hooks/exhaustive-deps -- restore once per project; a snapshot refresh must not reset the form
  const restoredSettings = useMemo(() => stagedOrSaved(project, actorId), [project.id]);
  const [connectAlias, setConnectAlias] = useState<string | null>(null);
  const [autoResearchInvocationCeiling, setAutoResearchInvocationCeiling] = useState(
    () => restoredSettings.autoResearchInvocationCeiling,
  );
  const [profiles, setProfiles] = useState<Record<AgentExecutionProfile, AgentProfileSettings>>(
    () => restoredSettings.profiles,
  );
  // A return to the same project is a new visit: requests from the previous
  // visit must not overwrite the restored form or publish into another view.
  const requestOwner = useMemo(() => ({ projectId: project.id }), [project.id]);
  const currentRequestOwner = useRef<typeof requestOwner | null>(requestOwner);
  currentRequestOwner.current = requestOwner;
  const requestIsCurrent = () => currentRequestOwner.current === requestOwner;
  const [skillDefaults, setSkillDefaults] = useState<SkillDefaults>(
    () => restoredSettings.skillDefaults,
  );
  const [inspectedPackage, setInspectedPackage] = useState<SkillCatalogEntry | null>(null);
  const [saving, setSaving] = useState(false);
  const [writableSaving, setWritableSaving] = useState<string | null>(null);
  const [clearingCaches, setClearingCaches] = useState(false);
  const spaceMachines = useSpaceMachines();
  // A settings watermark move (another tab's card edit, a route check) brings a
  // new machine list; the cards follow it, keeping drafts still being typed.
  const machinesSeen = useRef(project.machines);
  const reloadSpaceMachines = spaceMachines.reload;
  useEffect(() => {
    if (machinesSeen.current === project.machines) return;
    machinesSeen.current = project.machines;
    void reloadSpaceMachines();
  }, [project.machines, reloadSpaceMachines]);
  // Machines show as tiles, all closed at first; one opens at a time so the
  // section stays short.
  const [openMachine, setOpenMachine] = useState<string | null>(null);
  const [addingMachine, setAddingMachine] = useState(false);
  const [cacheMetrics, setCacheMetrics] = useState(project.cache_metrics);
  const [status, setStatus] = useState<{ kind: "saved" | "error"; text: string } | null>(null);

  useEffect(() => {
    currentRequestOwner.current = requestOwner;
    return () => {
      if (currentRequestOwner.current === requestOwner) currentRequestOwner.current = null;
    };
  }, [requestOwner]);

  // Reload the form only when the project itself changes. Keying this on the
  // whole snapshot discarded in-progress edits every time an unrelated refresh
  // — a finished background run, a cache clear — handed down a new object
  // carrying byte-identical settings.
  useEffect(() => {
    setAutoResearchInvocationCeiling(restoredSettings.autoResearchInvocationCeiling);
    setProfiles(restoredSettings.profiles);
    setSkillDefaults(restoredSettings.skillDefaults);
    setSaving(false);
    setWritableSaving(null);
    setClearingCaches(false);
    setInspectedPackage(null);
    setStatus(null);
  }, [restoredSettings]);

  // Cache metrics are server-owned, so they follow every snapshot.
  useEffect(() => {
    setCacheMetrics(project.cache_metrics);
  }, [project.cache_metrics]);

  useEffect(() => {
    void onRefreshUsage();
  }, [onRefreshUsage]);

  const baseline = useMemo(
    () =>
      settingsFingerprint({
        autoResearchInvocationCeiling: project.default_auto_research_invocation_ceiling,
        profiles: profilesFrom(project),
        skillDefaults: skillDefaultsFrom(project),
      }),
    [project],
  );
  const current = settingsFingerprint({
    autoResearchInvocationCeiling,
    profiles,
    skillDefaults,
  });
  const dirty = current !== baseline;
  const autoResearchInvocationCeilingIsValid =
    Number.isSafeInteger(autoResearchInvocationCeiling) && autoResearchInvocationCeiling >= 1;

  // Stage the form's edits; clearing a clean form drops the staged copy.
  useEffect(() => {
    const key = settingsDraftStorageKey(actorId, project.id);
    if (!key) return;
    try {
      if (dirty) {
        localStorage.setItem(
          key,
          serializeSettingsDraft({
            version: 4,
            autoResearchInvocationCeiling,
            profiles,
            skillDefaults,
          }),
        );
      } else {
        localStorage.removeItem(key);
      }
    } catch {
      // Staging edits is a convenience; storage failures must not affect Settings.
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- current fingerprints every staged field
  }, [dirty, current, project.id, actorId]);
  const cardFor = (machineId: string) =>
    spaceMachines.machines ? spaceMachineById(spaceMachines.machines, machineId) : null;
  const providerCatalog = Object.values(project.providers).sort((left, right) =>
    (left.label || left.provider).localeCompare(right.label || right.provider),
  );
  const workflowCatalog = skillCatalog.filter((entry) => entry.kind === "workflow");
  const directSkillCatalog = skillCatalog.filter((entry) => entry.kind === "skill");

  // Agents can write: a human approval, applied at once rather than staged for Save.
  const setRepositoryWritable = async (alias: string, writable: boolean) => {
    if (writesDisabled || writableSaving) return;
    setWritableSaving(alias);
    setStatus(null);
    try {
      const saved = await api<ProjectSnapshot>(
        `${apiBase}/repositories/${encodeURIComponent(alias)}/writable`,
        { method: "PUT", body: JSON.stringify({ writable }) },
      );
      if (!requestIsCurrent()) return;
      onSaved(saved);
    } catch (caught) {
      if (!requestIsCurrent()) return;
      setStatus({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      if (requestIsCurrent()) setWritableSaving(null);
    }
  };

  const toggleSkillDefault = (entry: SkillCatalogEntry) => {
    setStatus(null);
    setSkillDefaults((currentDefaults) => {
      const field = entry.kind === "workflow" ? "workflow_ids" : "skill_ids";
      const selected = currentDefaults[field];
      return {
        ...currentDefaults,
        [field]: selected.includes(entry.id)
          ? selected.filter((id) => id !== entry.id)
          : [...selected, entry.id],
      };
    });
  };

  const renderSkillGroup = (title: string, entries: SkillCatalogEntry[], selectedIds: string[]) => (
    <div className="skill-card-group" role="group" aria-label={title}>
      <h3>{title}</h3>
      <div className="skill-card-grid">
        {entries.map((entry) => {
          const selected = selectedIds.includes(entry.id);
          return (
            <div
              className={selected ? "skill-card selected" : "skill-card"}
              key={`${entry.kind}:${entry.id}`}
            >
              <button
                type="button"
                className="skill-card-select"
                aria-pressed={selected}
                disabled={writesDisabled}
                onClick={() => toggleSkillDefault(entry)}
              >
                <span className="settings-check" aria-hidden="true">
                  {selected && <Check size={12} />}
                </span>
                <span>{entry.label}</span>
              </button>
              <button
                type="button"
                className="icon-button skill-card-inspect"
                aria-label={`Inspect ${entry.label}`}
                onClick={() => setInspectedPackage(entry)}
              >
                <BookOpen size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );

  const reset = () => {
    setAutoResearchInvocationCeiling(project.default_auto_research_invocation_ceiling);
    setProfiles(profilesFrom(project));
    setSkillDefaults(savedSkillDefaults);
    setStatus(null);
  };

  const save = async () => {
    if (!dirty || saving || writesDisabled || !autoResearchInvocationCeilingIsValid) return;
    setSaving(true);
    setStatus(null);
    const body: ProjectSettingsRequest = {
      default_auto_research_invocation_ceiling: autoResearchInvocationCeiling,
      agent_profiles: Object.fromEntries(
        executionProfiles.map(({ id }) => [id, profiles[rowOf(id)]]),
      ) as Record<AgentExecutionProfile, AgentProfileSettings>,
      skill_defaults: skillDefaults,
    };
    try {
      const saved = await api<ProjectSnapshot>(`${apiBase}/settings`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      if (!requestIsCurrent()) return;
      // The save response intentionally carries no live probe, and no setting
      // here feeds provider readiness, so the rendered readiness stands.
      onSaved(saved);
      try {
        await onRefreshReadiness();
        if (!requestIsCurrent()) return;
        setStatus({ kind: "saved", text: "Saved." });
      } catch (readinessError) {
        if (!requestIsCurrent()) return;
        setStatus({
          kind: "error",
          text: `Saved, but readiness refresh failed: ${readinessError instanceof Error ? readinessError.message : String(readinessError)}`,
        });
      }
    } catch (caught) {
      if (!requestIsCurrent()) return;
      setStatus({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      if (requestIsCurrent()) setSaving(false);
    }
  };

  // What a tile shows at a glance: each provider, the jobs route, and grants.
  const machineSignals = (machine: (typeof project.machines)[number]): MachineSignal[] => {
    const record = cardFor(machine.machine_id);
    const signals: MachineSignal[] = providerCatalog.map((provider) => {
      const recorded = record?.provider_paths[provider.provider] ?? "";
      const state = providerPathPresentation(
        project.provider_readiness[machine.machine_id]?.[provider.provider],
        recorded,
        recorded,
      );
      return { label: provider.label || provider.provider, tone: state.kind };
    });
    // Slurm is checked on its machine; the helper launches only on the base.
    const slurm = machine.compute?.job_manager === "slurm";
    if (slurm || machine.machine_id === baseMachine(project)) {
      const probe = machine.compute_probes?.[slurm ? "scheduler" : "helper"];
      signals.push({
        label: slurm ? "Slurm" : "Jobs",
        tone: probe ? computeProbePresentation(probe).tone : "warning",
      });
    }
    const grants = record?.writable_paths.length ?? 0;
    if (grants) signals.push({ label: `${grants} writable`, tone: "ready" });
    return signals;
  };

  const clearCaches = async () => {
    if (cacheClearDisabled || clearingCaches) return;
    setClearingCaches(true);
    setStatus(null);
    try {
      const metrics = await clearProjectCaches(apiBase);
      if (!requestIsCurrent()) return;
      publishCacheMetrics(metrics, setCacheMetrics, onCacheMetricsChange);
      setStatus(
        metrics.project_page_rebuilt
          ? { kind: "saved", text: "Project cache cleared." }
          : {
              kind: "error",
              text: "Project cache cleared, but the project page could not be rebuilt; the previous copy is kept.",
            },
      );
    } catch (caught) {
      if (!requestIsCurrent()) return;
      setStatus({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      if (requestIsCurrent()) setClearingCaches(false);
    }
  };

  const sections: SettingsSection[] = [
    {
      id: "usage",
      label: "Usage",
      icon: <Gauge size={16} />,
      content: <AgentUsageWidgets usage={usage} providers={project.providers} />,
    },
    {
      id: "members",
      label: "Members",
      icon: <Users size={16} />,
      content: (
        <ProjectMembers
          key={project.id}
          projectId={project.id}
          identity={identity}
          api={api}
          onLeft={onLeftProject}
          headerAction={
            spaceKind === "personal" && onMovePersonalProjectToTeam ? (
              <button
                className="button secondary project-move-to-team"
                type="button"
                disabled={writesDisabled}
                onClick={() => onMovePersonalProjectToTeam(project.id)}
              >
                <Server size={14} /> Move to team space
              </button>
            ) : null
          }
        />
      ),
    },
    {
      id: "repos",
      label: "Repos",
      icon: <GitBranch size={16} />,
      content: (
        <article className="settings-section boundary-settings">
          <header>
            <span>
              <GitBranch size={16} />
            </span>
            <h2>Repos</h2>
          </header>
          <div className="settings-repositories">
            {project.repositories.map((repository) => {
              const card = cardFor(repository.machine);
              const canonical = repository.alias === project.state_repository;
              const selected = canonical || project.project_truth_scope.includes(repository.alias);
              return (
                <label
                  className={selected ? "settings-repository selected" : "settings-repository"}
                  key={repository.alias}
                  title="Agents can write"
                >
                  <input
                    type="checkbox"
                    aria-label={`Agents can write ${repository.alias}`}
                    checked={selected}
                    disabled={writesDisabled || canonical || writableSaving !== null}
                    onChange={(event) =>
                      void setRepositoryWritable(repository.alias, event.target.checked)
                    }
                  />
                  <span className="settings-check">{selected && <Check size={12} />}</span>
                  <span className="settings-repository-copy">
                    <strong>{repository.alias}</strong>
                    <span className="settings-repository-path">
                      {card?.host ? `${card.host}:${repository.path}` : repository.path}
                    </span>
                  </span>
                  <span className="settings-repository-meta">
                    {card && (
                      <>
                        <Server size={12} /> {card.name}
                      </>
                    )}
                    {canonical && <em>canonical state</em>}
                    {repository.source === "server_only" && <em>server only, not backed up</em>}
                    {repository.can_connect && spaceKind === "team" && (
                      <button
                        type="button"
                        className="button ghost tiny"
                        disabled={writesDisabled}
                        onClick={(event) => {
                          event.preventDefault();
                          setConnectAlias(repository.alias);
                        }}
                      >
                        Connect to GitHub
                      </button>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
          <RepositoryRequests
            key={project.id}
            project={project}
            spaceKind={spaceKind}
            baseMachineId={baseMachine(project) || null}
            disabled={writesDisabled}
            connectAlias={connectAlias}
            onCloseConnect={() => setConnectAlias(null)}
            onAdded={(saved) => {
              if (requestIsCurrent()) onSaved(saved);
            }}
          />
        </article>
      ),
    },
    {
      id: "machines",
      label: "Machines",
      icon: <Server size={16} />,
      content: (
        <section className="settings-section provider-path-settings">
          <header>
            <span>
              <Server size={16} />
            </span>
            <h2>Machines</h2>
            <button
              className="button secondary compact"
              type="button"
              disabled={writesDisabled || readinessRequest?.pending}
              title="Probe machine reachability"
              onClick={() => void onRefreshReadiness().catch(() => {})}
            >
              {readinessRequest?.pending ? (
                <LoaderCircle className="spin" size={14} />
              ) : (
                <ScanSearch size={14} />
              )}
              Probe
            </button>
          </header>
          <div className="machine-tiles" role="group" aria-label="Machines">
            {project.machines.map((machine) => {
              const card = cardFor(machine.machine_id);
              return (
                <MachineTile
                  key={machine.machine_id}
                  machineId={machine.machine_id}
                  name={card?.name ?? ""}
                  hostLabel={card ? machineHostLabel(card.host, spaceKind) : ""}
                  account={card?.os_account ?? ""}
                  base={machine.machine_id === baseMachine(project)}
                  signals={machineSignals(machine)}
                  selected={openMachine === machine.machine_id}
                  onSelect={() => {
                    setAddingMachine(false);
                    setOpenMachine((current) =>
                      current === machine.machine_id ? null : machine.machine_id,
                    );
                  }}
                />
              );
            })}
            <AddMachineTile
              label="Add machine"
              selected={addingMachine}
              disabled={writesDisabled}
              onSelect={() => {
                setOpenMachine(null);
                setAddingMachine((current) => !current);
              }}
            />
          </div>
          <div className="provider-machine-list">
            {project.machines
              .filter((machine) => machine.machine_id === openMachine)
              .map((machine) => {
                const record = cardFor(machine.machine_id);
                return (
                  <MachineCard
                    key={`${project.id}:${machine.machine_id}`}
                    title={record?.name ?? ""}
                    hostLabel={record ? machineHostLabel(record.host, spaceKind) : ""}
                    osAccount={record?.os_account ?? ""}
                    record={record}
                    accountRequired={spaceKind === "team"}
                    writesDisabled={writesDisabled}
                    onRecordChange={spaceMachines.replace}
                    onRemoveFromProject={
                      machine.removable
                        ? async () => {
                            const saved = await removeProjectMachine(
                              project.id,
                              machine.machine_id,
                            );
                            if (!requestIsCurrent()) return;
                            onSaved(saved, { provider: false });
                            void spaceMachines.reload();
                            setOpenMachine(null);
                          }
                        : undefined
                    }
                  />
                );
              })}
          </div>
          {spaceMachines.error && <div className="settings-error">{spaceMachines.error}</div>}
          {addingMachine && (
            <AddProjectMachine
              projectId={project.id}
              spaceKind={spaceKind}
              onClose={() => setAddingMachine(false)}
              machines={spaceMachines.machines ?? []}
              writesDisabled={writesDisabled}
              onCreated={spaceMachines.replace}
              onAdded={(saved, machineId) => {
                if (!requestIsCurrent()) return;
                onSaved(saved, { provider: false });
                void spaceMachines.reload();
                void onRefreshReadiness().catch(() => {});
                setAddingMachine(false);
                setOpenMachine(machineId);
              }}
            />
          )}
        </section>
      ),
    },
    {
      id: "notifications",
      label: "Notifications",
      icon: <Bell size={16} />,
      content: (
        <ProjectNotifications
          key={`notifications:${project.id}`}
          projectId={project.id}
          api={api}
        />
      ),
    },
    {
      id: "consolidation",
      label: "Nightly consolidation",
      icon: <Moon size={16} />,
      content: (
        <ProjectConsolidation
          key={`consolidation:${project.id}:${consolidation?.schedule?.authorization_id ?? ""}`}
          apiBase={apiBase}
          schedule={consolidation?.schedule ?? null}
          recentNights={consolidation?.recent_nights ?? []}
          loaded={consolidation !== null}
          loadError={consolidationError}
          writesDisabled={!consolidation?.can_write}
          onChanged={onConsolidationChanged}
        />
      ),
    },
    {
      id: "agent-defaults",
      drafts: true,
      label: "Agent defaults",
      icon: <Bot size={16} />,
      content: (
        <div className="settings-section agent-defaults">
          <header className="agent-defaults-heading">
            <div>
              <h2>Agent defaults</h2>
            </div>
            <label
              className="agent-auto-research-default"
              title="Operational invocations per newly authorized episode"
            >
              <span>Auto-research ceiling</span>
              <input
                type="number"
                min={1}
                step={1}
                inputMode="numeric"
                value={autoResearchInvocationCeiling}
                disabled={writesDisabled}
                onChange={(event) => {
                  setAutoResearchInvocationCeiling(Number(event.target.value));
                  setStatus(null);
                }}
              />
            </label>
          </header>
          <div className="settings-agent-list">
            {executionRows.map(({ id, label }) => (
              <article className="settings-agent" key={id}>
                <header>
                  <span>
                    <strong>{label}</strong>
                  </span>
                  <span className="tag">
                    {id === "paper_coach"
                      ? "read-only coach"
                      : id === "orchestrator"
                        ? "auto-research"
                        : "graph patch only"}
                  </span>
                </header>
                <AgentConfigControls
                  project={project}
                  value={{ ...profiles[id], run_on: baseMachine(project) }}
                  // The exported head describes the saved provider; a draft that
                  // moved it shows blank until saved.
                  effectiveModel={
                    profiles[id].provider === project.agent_profiles[id]?.provider
                      ? project.agent_profiles[id]?.effective_model
                      : ""
                  }
                  locked={writesDisabled}
                  workLikeCapable={project.agent_profiles[id]?.work_like_capable ?? true}
                  onRefreshReadiness={onRefreshReadiness}
                  readinessPending={readinessRequest?.pending ?? false}
                  readinessError={readinessRequest?.providerError ?? null}
                  runtime={{
                    value: profiles[id].runtime,
                    onChange: (runtime) => {
                      setProfiles((currentProfiles) => ({
                        ...currentProfiles,
                        [id]: { ...currentProfiles[id], runtime },
                      }));
                      setStatus(null);
                    },
                  }}
                  onChange={({ run_on: _runOn, ...value }) => {
                    setProfiles((currentProfiles) => ({
                      ...currentProfiles,
                      [id]: { ...currentProfiles[id], ...value },
                    }));
                    setStatus(null);
                  }}
                />
              </article>
            ))}
          </div>
        </div>
      ),
    },
    {
      id: "skills",
      drafts: true,
      label: "Skills & workflows",
      icon: <Sparkles size={16} />,
      content: (
        <section className="settings-section skill-settings">
          <header>
            <span>
              <Sparkles size={16} />
            </span>
            <h2>Skills &amp; workflows</h2>
          </header>
          <div className="skill-card-groups">
            {renderSkillGroup("Workflows", workflowCatalog, skillDefaults.workflow_ids)}
            {renderSkillGroup("Skills", directSkillCatalog, skillDefaults.skill_ids)}
          </div>
        </section>
      ),
    },
    {
      id: "cache",
      label: "Project cache",
      icon: <HardDrive size={16} />,
      content: (
        <section className="settings-section cache-settings">
          <header>
            <span>
              <HardDrive size={16} />
            </span>
            <h2>Project cache</h2>
            <button
              className="button secondary compact"
              disabled={cacheClearDisabled || clearingCaches}
              aria-label="Clear project cache"
              onClick={() => void clearCaches()}
            >
              {clearingCaches ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
              {clearingCaches ? "Clearing" : "Clear project cache"}
            </button>
          </header>
          <div className="cache-meter-list">
            <CacheMeter label="Remote sources" metric={cacheMetrics.remote_sources} />
            <CacheMeter label="Session slices" metric={cacheMetrics.session_slices} />
          </div>
        </section>
      ),
    },
  ];
  // An invalid ceiling is explained on every pane, since only Agent defaults shows the field.
  const barStatus: typeof status = autoResearchInvocationCeilingIsValid
    ? status
    : {
        kind: "error",
        text: "Agent defaults: the auto-research ceiling must be a whole number of at least 1.",
      };

  return (
    <section className="settings-page" data-settings-level="project">
      <SettingsRail key={project.id} sections={sections} initialSection={initialSection}>
        {/* The bar shows where drafts are edited, or when it has unsaved edits or a result to show. */}
        {(section) =>
          (section.drafts || dirty || barStatus) && (
            <footer className="settings-savebar">
              <div
                className={
                  barStatus ? `settings-save-status ${barStatus.kind}` : "settings-save-status"
                }
              >
                {barStatus?.kind === "error" && <TriangleAlert size={16} />}
                {barStatus?.kind === "saved" && <Check size={16} />}
                <span>{barStatus?.text}</span>
              </div>
              <button className="button secondary" disabled={!dirty || saving} onClick={reset}>
                <RotateCcw size={14} /> Reset
              </button>
              <button
                className="button primary"
                disabled={
                  writesDisabled || !dirty || saving || !autoResearchInvocationCeilingIsValid
                }
                onClick={() => void save()}
              >
                {saving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}
                {saving ? "Saving" : "Save"}
              </button>
            </footer>
          )
        }
      </SettingsRail>
      {inspectedPackage && (
        <SkillPackageInspector entry={inspectedPackage} onClose={() => setInspectedPackage(null)} />
      )}
    </section>
  );
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
