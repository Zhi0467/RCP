import {
  BookOpen,
  Check,
  GitBranch,
  HardDrive,
  LoaderCircle,
  RotateCcw,
  ScanSearch,
  Save,
  Server,
  Sparkles,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, clearProjectCaches, removeProjectMachine } from "../core/api";
import { computeProbePresentation } from "../experiments/compute";
import { RepositoryRequests } from "./RepositoryRequests";
import { ProjectMembers } from "./ProjectMembers";
import { MachineCard } from "./MachineCard";
import { ProjectNotifications } from "../desktop/ProjectNotifications";
import { ProjectConsolidation } from "./ProjectConsolidation";
import { ProjectLessons } from "./ProjectLessons";
import { AddMachineTile, MachineTile, type MachineSignal } from "./MachineTile";
import { AddProjectMachine } from "./AddProjectMachine";
import { useSpaceMachines } from "./useSpaceMachines";
import { machineHostLabel, spaceMachineForProject } from "./spaceMachines";
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
  { id: "seed", label: "Seed" },
  { id: "refresh", label: "Refresh" },
  { id: "node_chat", label: "Node chat" },
  { id: "project_chat", label: "Project chat" },
  { id: "paper_coach", label: "Paper coach" },
  { id: "orchestrator", label: "Auto-research orchestrator" },
];

function profilesFrom(
  project: ProjectSnapshot,
): Record<AgentExecutionProfile, AgentProfileSettings> {
  const canonicalMachine =
    project.repositories.find((repository) => repository.alias === project.state_repository)
      ?.machine ?? project.run_on;
  return Object.fromEntries(
    executionProfiles.map(({ id }) => {
      const storedProfile =
        id === "orchestrator"
          ? (project.agent_profiles.orchestrator ?? project.agent_profiles.refresh)
          : project.agent_profiles[id];
      const settings = { ...profileRunConfig(storedProfile), runtime: storedProfile.runtime };
      return [id, id === "paper_coach" ? settings : { ...settings, run_on: canonicalMachine }];
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
    scope: project.default_run_truth_scope,
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
    scope: staged.scope,
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
}: Props) {
  const skillCatalog = skillCatalogFrom(project);
  const savedSkillDefaults = skillDefaultsFrom(project);
  const actorId = identity?.user.user_id ?? null;
  // eslint-disable-next-line react-hooks/exhaustive-deps -- restore once per project; a snapshot refresh must not reset the form
  const restoredSettings = useMemo(() => stagedOrSaved(project, actorId), [project.id]);
  const [connectAlias, setConnectAlias] = useState<string | null>(null);
  const [scope, setScope] = useState<string[]>(() => restoredSettings.scope);
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
    setScope(restoredSettings.scope);
    setAutoResearchInvocationCeiling(restoredSettings.autoResearchInvocationCeiling);
    setProfiles(restoredSettings.profiles);
    setSkillDefaults(restoredSettings.skillDefaults);
    setSaving(false);
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
        scope: project.default_run_truth_scope,
        autoResearchInvocationCeiling: project.default_auto_research_invocation_ceiling,
        profiles: profilesFrom(project),
        skillDefaults: skillDefaultsFrom(project),
      }),
    [project],
  );
  const current = settingsFingerprint({
    scope,
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
            scope,
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
  const machineByAlias = Object.fromEntries(
    project.machines.map((machine) => [machine.alias, machine]),
  );
  // Aliases that reach the same machine account, such as a laptop and its
  // remote that both became the team server on a move, share one tile and card.
  const machineGroups: { key: string; machines: typeof project.machines }[] = [];
  for (const machine of project.machines) {
    const group = machineGroups.find(
      ({ machines: [first] }) =>
        first.host === machine.host && first.os_account === machine.os_account,
    );
    if (group) group.machines.push(machine);
    else machineGroups.push({ key: machine.alias, machines: [machine] });
  }
  const providerCatalog = Object.values(project.providers).sort((left, right) =>
    (left.label || left.provider).localeCompare(right.label || right.provider),
  );
  const workflowCatalog = skillCatalog.filter((entry) => entry.kind === "workflow");
  const directSkillCatalog = skillCatalog.filter((entry) => entry.kind === "skill");

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
    setScope(project.default_run_truth_scope);
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
      default_run_truth_scope: scope,
      default_auto_research_invocation_ceiling: autoResearchInvocationCeiling,
      agent_profiles: profiles,
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
    const record = spaceMachines.machines
      ? spaceMachineForProject(spaceMachines.machines, project.id, machine)
      : null;
    const signals: MachineSignal[] = providerCatalog.map((provider) => {
      const recorded = record?.provider_paths[provider.provider] ?? "";
      const state = providerPathPresentation(
        project.provider_readiness[machine.alias]?.[provider.provider],
        recorded,
        recorded,
      );
      return { label: provider.label || provider.provider, tone: state.kind };
    });
    const probe = machine.compute_probes?.[machine.compute?.job_manager ? "scheduler" : "helper"];
    signals.push({
      label: machine.compute?.job_manager === "slurm" ? "Slurm" : "Jobs",
      tone: probe ? computeProbePresentation(probe).tone : "warning",
    });
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

  return (
    <section className="settings-page" data-settings-level="project">
      <AgentUsageWidgets usage={usage} providers={project.providers} />

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
      <article className="settings-section boundary-settings">
        <header>
          <span>
            <GitBranch size={16} />
          </span>
          <h2>Repos</h2>
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
        {spaceKind === "team" && (
          <RepositoryRequests
            key={project.id}
            project={project}
            disabled={writesDisabled}
            connectAlias={connectAlias}
            onCloseConnect={() => setConnectAlias(null)}
          />
        )}
      </article>

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
          {machineGroups.map(({ key, machines }) => (
            <MachineTile
              key={key}
              name={machines.map((machine) => machine.alias).join(", ")}
              hostLabel={machineHostLabel(machines[0].host, spaceKind)}
              account={machines[0].os_account}
              signals={[
                ...new Map(
                  machines.flatMap(machineSignals).map((signal) => [signal.label, signal]),
                ).values(),
              ]}
              selected={openMachine === key}
              onSelect={() => {
                setAddingMachine(false);
                setOpenMachine((current) => (current === key ? null : key));
              }}
            />
          ))}
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
          {machineGroups
            .filter((group) => group.key === openMachine)
            .map(({ key, machines: group }) => {
              const record = spaceMachines.machines
                ? spaceMachineForProject(spaceMachines.machines, project.id, group[0])
                : null;
              const removable = group.every((machine) => machine.removable);
              return (
                <MachineCard
                  key={`${project.id}:${record?.machine_id ?? key}`}
                  title={record?.name ?? group.map((item) => item.alias).join(", ")}
                  hostLabel={machineHostLabel(group[0].host, spaceKind)}
                  osAccount={group[0].os_account}
                  record={record}
                  writesDisabled={writesDisabled}
                  onRecordChange={spaceMachines.replace}
                  onRemoveFromProject={
                    removable
                      ? async () => {
                          for (const machine of group) {
                            const saved = await removeProjectMachine(project.id, machine.alias);
                            if (!requestIsCurrent()) return;
                            onSaved(saved, { provider: false });
                          }
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
            takenAliases={project.machines.map((machine) => machine.alias)}
            onClose={() => setAddingMachine(false)}
            machines={spaceMachines.machines ?? []}
            writesDisabled={writesDisabled}
            onCreated={spaceMachines.replace}
            onAdded={(saved, alias) => {
              if (!requestIsCurrent()) return;
              onSaved(saved, { provider: false });
              void spaceMachines.reload();
              void onRefreshReadiness().catch(() => {});
              setAddingMachine(false);
              setOpenMachine(alias);
            }}
          />
        )}
      </section>

      <ProjectNotifications key={`notifications:${project.id}`} projectId={project.id} api={api} />
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
      <ProjectLessons
        key={`lessons:${project.id}`}
        apiBase={apiBase}
        writesDisabled={!consolidation?.can_write}
      />

      <div className="settings-section agent-defaults">
        <header className="agent-defaults-heading">
          <div>
            <h2>Agent defaults</h2>
          </div>
          <label className="agent-auto-research-default">
            <span>
              Auto-research ceiling
              <small>Operational invocations per newly authorized episode</small>
            </span>
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
          {executionProfiles.map(({ id, label }) => (
            <article className="settings-agent" key={id}>
              <header>
                <span>
                  <strong>{label}</strong>
                </span>
                <span>
                  {id === "paper_coach"
                    ? "read-only coach"
                    : id === "orchestrator"
                      ? "auto-research"
                      : "graph patch only"}
                </span>
              </header>
              <AgentConfigControls
                project={project}
                value={profiles[id]}
                // The exported head describes the saved provider on the saved
                // machine; a draft that moved either shows blank until saved.
                effectiveModel={
                  profiles[id].provider === project.agent_profiles[id]?.provider &&
                  profiles[id].run_on === project.agent_profiles[id]?.run_on
                    ? project.agent_profiles[id]?.effective_model
                    : ""
                }
                locked={writesDisabled}
                runOnLocked={id !== "paper_coach"}
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
                onChange={(value) => {
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

      {inspectedPackage && (
        <SkillPackageInspector entry={inspectedPackage} onClose={() => setInspectedPackage(null)} />
      )}

      <footer className="settings-savebar">
        <div className={status ? `settings-save-status ${status.kind}` : "settings-save-status"}>
          {status?.kind === "error" && <TriangleAlert size={16} />}
          {status?.kind === "saved" && <Check size={16} />}
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
          disabled={writesDisabled || !dirty || saving || !autoResearchInvocationCeilingIsValid}
          onClick={() => void save()}
        >
          {saving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}
          {saving ? "Saving" : "Save"}
        </button>
      </footer>
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
