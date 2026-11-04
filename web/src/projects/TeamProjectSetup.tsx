import {
  ArrowLeft,
  ArrowRight,
  Check,
  Clipboard,
  FolderGit2,
  LoaderCircle,
  Plus,
  Server,
  ShieldCheck,
  SquareTerminal,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { EXPERIMENT_BOARD_POLL_DELAY_MS } from "./useProjectTabs";
import {
  api,
  cancelProjectProvisioningRequest,
  completeProjectProvisioningRequest,
  createTeamProjectProvisioning,
  loadProjectProvisioningRequest,
  loadProjectProvisioningRequests,
  loadSpaceMachines,
} from "../core/api";
import {
  configureDesktopServerOperatorRoute,
  isDesktopRuntime,
  listDesktopTeamConnections,
  openDesktopProjectProvisionTerminal,
  probeDesktopServerOperator,
  runDesktopProjectProvision,
  type ServerCommandEvent,
  type ServerOperatorMode,
  type ServerOperatorProbe,
  type TeamConnectionMetadata,
} from "../core/desktopRuntime";
import {
  modelChange,
  modelOptions,
  providerChange,
  providerOptions,
  readinessFor,
  reasoningOptions,
  runtimeOptions,
} from "./providers";
import {
  buildTeamProvisioningRequest,
  defaultRepositoryName,
  formatCommandArgv,
  githubRepositoryName,
  invalidProjectProvisioningHash,
  machineCardName,
  projectProvisioningHash,
  projectProvisioningRequestId,
  routeProvedBy,
  routeGroupId,
  stateRepositoryAfterRename,
  withDefaultRepositoryName,
} from "./projectSetupModel";
import { OperatorActionPanel } from "./OperatorActionPanel";
import { NewMachineForm } from "./MachineCard";
import { AddMachineTile, MachineTile } from "./MachineTile";
import { machineHostLabel } from "./spaceMachines";
import { hasRow, rowOf } from "../core/agentRows";
import type {
  AgentExecutionProfile,
  ProjectProvisioningCreateRequest,
  ProjectProvisioningResponse,
  ProviderReadiness,
  SetupAgentProfile,
  SetupAgents,
  SpaceMachine,
} from "../core/types";

interface Props {
  intentChooser: ReactNode;
  onCancel: () => void;
  onCreated: (projectId: string) => void;
}

interface TeamMachinePick {
  machine_id: string;
  central_root: string;
}

interface TeamRepositoryDraft {
  id: number;
  alias: string;
  /** A typed name stays; otherwise the name follows the GitHub URL, else the project name. */
  nameEdited: boolean;
  source: string;
  machine_id: string;
}

const steps = [
  ["01", "Project"],
  ["02", "Machines & repositories"],
  ["03", "Agent roles"],
  ["04", "Server setup"],
] as const;

const agentProfiles: Array<{ id: AgentExecutionProfile; label: string }> = [
  { id: "seed", label: "Seed and refresh" },
  { id: "refresh", label: "Refresh" },
  { id: "node_chat", label: "Node chat" },
  { id: "project_chat", label: "Project chat" },
  { id: "paper_coach", label: "Paper coach" },
  { id: "orchestrator", label: "Auto-research orchestrator" },
];
const agentRows = agentProfiles.filter(({ id }) => hasRow(id));

let repositorySequence = 1;

const defaultProfile = (model = ""): SetupAgentProfile => ({
  provider: "",
  runtime: "",
  model,
  reasoning: "medium",
  location: "local",
  host: "",
});

export function TeamProjectSetup({ intentChooser, onCancel, onCreated }: Props) {
  const desktop = isDesktopRuntime();
  const initialHash = typeof window === "undefined" ? "#/projects/new" : window.location.hash;
  const invalidResumeLink = invalidProjectProvisioningHash(initialHash);
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [spaceMachines, setSpaceMachines] = useState<SpaceMachine[]>([]);
  const [machines, setMachines] = useState<TeamMachinePick[]>([]);
  const [creatingMachine, setCreatingMachine] = useState(false);
  const [repositories, setRepositories] = useState<TeamRepositoryDraft[]>([
    {
      id: repositorySequence,
      alias: "research",
      nameEdited: false,
      source: "",
      machine_id: "",
    },
  ]);
  const [stateRepository, setStateRepository] = useState("research");
  const [agents, setAgents] = useState<SetupAgents>({
    seed: defaultProfile(),
    refresh: defaultProfile(),
    node_chat: defaultProfile(),
    project_chat: defaultProfile(),
    paper_coach: defaultProfile("gpt-5.6-luna"),
    orchestrator: defaultProfile(),
  });
  const [defaultAutoResearchInvocationCeiling, setDefaultAutoResearchInvocationCeiling] =
    useState(10);
  const [providers, setProviders] = useState<ProviderReadiness[]>([]);
  const [request, setRequest] = useState<ProjectProvisioningResponse | null>(null);
  const [savedRequests, setSavedRequests] = useState<ProjectProvisioningResponse[]>([]);
  const [connection, setConnection] = useState<TeamConnectionMetadata | null>(null);
  const [operatorTarget, setOperatorTarget] = useState("");
  const [probe, setProbe] = useState<ServerOperatorProbe | null>(null);
  const [events, setEvents] = useState<ServerCommandEvent[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [resumeRouteActive, setResumeRouteActive] = useState(
    () => projectProvisioningRequestId(initialHash) !== null,
  );
  const [resumeLoading, setResumeLoading] = useState(
    () => projectProvisioningRequestId(initialHash) !== null,
  );
  const [error, setError] = useState<string | null>(() =>
    invalidResumeLink
      ? "This project setup link has an invalid provisioning request identity."
      : null,
  );

  useEffect(() => {
    let stopped = false;
    void api<ProviderReadiness[]>("/api/providers")
      .then((known) => {
        if (stopped) return;
        setProviders(known);
        const fallback = known[0]?.provider;
        if (!fallback) return;
        setAgents(
          (current) =>
            Object.fromEntries(
              Object.entries(current).map(([surface, profile]) => [
                surface,
                known.some((item) => item.provider === profile.provider)
                  ? profile
                  : {
                      ...profile,
                      provider: fallback,
                      runtime: readinessFor(known, fallback)?.default_runtime ?? "",
                    },
              ]),
            ) as SetupAgents,
        );
      })
      .catch((caught) => {
        if (!stopped) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      stopped = true;
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    void loadSpaceMachines()
      .then((cards) => {
        if (stopped) return;
        setSpaceMachines(cards);
        const server = cards.find((card) => card.host === "");
        if (!server) return;
        setMachines((current) =>
          current.length ? current : [{ machine_id: server.machine_id, central_root: "" }],
        );
        setRepositories((current) =>
          current.map((item) =>
            item.machine_id ? item : { ...item, machine_id: server.machine_id },
          ),
        );
      })
      .catch((caught) => {
        if (!stopped) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      stopped = true;
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    const hash = typeof window === "undefined" ? "#/projects/new" : window.location.hash;
    if (invalidProjectProvisioningHash(hash)) {
      return () => {
        stopped = true;
      };
    }
    const requestId = projectProvisioningRequestId(hash);
    const pending = requestId
      ? loadProjectProvisioningRequest(requestId).then((loaded) => {
          if (!stopped) {
            setRequest(loaded);
            setStep(3);
          }
        })
      : loadProjectProvisioningRequests().then((loaded) => {
          if (!stopped) setSavedRequests(loaded);
        });
    void pending
      .catch((caught) => {
        if (!stopped) setError(caught instanceof Error ? caught.message : String(caught));
      })
      .finally(() => {
        if (!stopped) setResumeLoading(false);
      });
    return () => {
      stopped = true;
    };
  }, []);

  useEffect(() => {
    if (!desktop) return;
    let stopped = false;
    void listDesktopTeamConnections()
      .then(async (saved) => {
        const currentOrigin = window.location.origin;
        const current = saved.find((item) => {
          try {
            return new URL(item.local_origin).origin === currentOrigin;
          } catch {
            return false;
          }
        });
        if (!current || stopped) return;
        setConnection(current);
        // An operator route is a separate capability the human grants, so the
        // connection's own login is only offered as the draft until they check it.
        setOperatorTarget(current.operator_route?.ssh_target ?? current.ssh_target);
        if (current.operator_route) {
          const checked = await probeDesktopServerOperator(current.connection_id);
          if (!stopped) setProbe(checked);
        }
      })
      .catch((caught) => {
        if (!stopped) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      stopped = true;
    };
  }, [desktop]);

  // While the request waits on the server, follow it here instead of asking
  // the human to refresh after each terminal step.
  const watchedRequestId = request?.can_run_setup ? request.request_id : null;
  const watchedRevision = useRef(request?.revision);
  watchedRevision.current = request?.revision;
  useEffect(() => {
    if (watchedRequestId === null) return;
    let active = true;
    let timer = 0;
    let failures = 0;
    const poll = async () => {
      try {
        const next = await loadProjectProvisioningRequest(watchedRequestId);
        if (failures >= 3 && active) setError(null);
        failures = 0;
        // Only a newer revision replaces the view, so a poll that overlapped
        // Run setup cannot put an older answer back.
        if (active && next.revision > (watchedRevision.current ?? -1)) setCurrentRequest(next);
      } catch (caught) {
        // Keep polling, but say so once a few checks in a row have failed,
        // since the page has no other way to notice progress.
        failures += 1;
        if (failures === 3 && active) {
          setError(
            `Cannot reach the server to check setup (${caught instanceof Error ? caught.message : String(caught)}). Still retrying.`,
          );
        }
      } finally {
        if (active) timer = window.setTimeout(() => void poll(), EXPERIMENT_BOARD_POLL_DELAY_MS);
      }
    };
    timer = window.setTimeout(() => void poll(), EXPERIMENT_BOARD_POLL_DELAY_MS);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [watchedRequestId]);

  const canonicalMachine = useMemo(() => {
    const repository = repositories.find((item) => item.alias === stateRepository);
    return repository?.machine_id ?? machines[0]?.machine_id ?? "";
  }, [machines, repositories, stateRepository]);

  const cardFor = (machineId: string) =>
    spaceMachines.find((card) => card.machine_id === machineId);

  const pickMachine = (machineId: string) => {
    setMachines((current) =>
      current.some((item) => item.machine_id === machineId)
        ? current
        : [...current, { machine_id: machineId, central_root: "" }],
    );
    setRepositories((current) =>
      current.map((item) => (item.machine_id ? item : { ...item, machine_id: machineId })),
    );
    setError(null);
  };

  const unpickMachine = (machineId: string) => {
    const remaining = machines.filter((item) => item.machine_id !== machineId);
    setMachines(remaining);
    setRepositories((current) =>
      current.map((item) =>
        item.machine_id === machineId
          ? { ...item, machine_id: remaining[0]?.machine_id ?? "" }
          : item,
      ),
    );
  };

  const nameRepository = (
    drafts: TeamRepositoryDraft[],
    repository: TeamRepositoryDraft,
    projectName: string,
  ) =>
    withDefaultRepositoryName(drafts, repository.id, [
      githubRepositoryName(repository.source),
      projectName,
    ]);

  const commitRepositories = (next: TeamRepositoryDraft[]) => {
    setStateRepository((selected) => stateRepositoryAfterRename(repositories, next, selected));
    setRepositories(next);
    setError(null);
  };

  const updateRepository = (id: number, patch: Partial<TeamRepositoryDraft>) => {
    const next = repositories.map((item) => (item.id === id ? { ...item, ...patch } : item));
    const updated = next.find((item) => item.id === id);
    commitRepositories(
      patch.source !== undefined && updated ? nameRepository(next, updated, name) : next,
    );
  };

  const updateAgent = (profile: AgentExecutionProfile, patch: Partial<SetupAgentProfile>) => {
    setAgents((current) => ({ ...current, [profile]: { ...current[profile], ...patch } }));
    setError(null);
  };

  const validate = (targetStep: number): string | null => {
    if (!name.trim()) return "Give this shared project a name.";
    if (targetStep < 1) return null;
    const machineIds = machines.map((machine) => machine.machine_id);
    const repositoryAliases = repositories.map((repository) => repository.alias.trim());
    if (machineIds.length === 0) return "Pick at least one machine.";
    if (repositoryAliases.some((alias) => !/^[a-z][a-z0-9-]{0,47}$/.test(alias))) {
      return "Repository names must use lowercase letters, numbers, or hyphens.";
    }
    if (new Set(repositoryAliases).size !== repositoryAliases.length) {
      return "Each repository needs a unique name.";
    }
    if (
      machines.some(
        (machine) =>
          machine.central_root.trim() &&
          (!machine.central_root.trim().startsWith("/") || machine.central_root.trim() === "/"),
      )
    ) {
      return "An explicit SSH central root must be a specific absolute path.";
    }
    if (repositories.some((repository) => !machineIds.includes(repository.machine_id))) {
      return "Every repository must name one picked machine.";
    }
    if (!repositoryAliases.includes(stateRepository)) {
      return "Choose a canonical state repository.";
    }
    if (targetStep < 2) return null;
    if (
      !Number.isSafeInteger(defaultAutoResearchInvocationCeiling) ||
      defaultAutoResearchInvocationCeiling < 1
    ) {
      return "Set the default auto-research ceiling to at least 1 operational invocation.";
    }
    if (agentProfiles.some(({ id }) => !agents[id].provider || !agents[id].runtime)) {
      return "Choose an available provider and runtime for every agent role.";
    }
    return null;
  };

  const provisioningBody = (): ProjectProvisioningCreateRequest =>
    buildTeamProvisioningRequest({
      name,
      stateRepository,
      defaultAutoResearchInvocationCeiling,
      machines: machines.map(({ machine_id, central_root }) => ({
        machine_id,
        central_root: cardFor(machine_id)?.host && central_root.trim() ? central_root.trim() : null,
      })),
      repositories: repositories.map(({ id: _id, nameEdited: _nameEdited, ...repository }) => ({
        ...repository,
        alias: repository.alias.trim(),
        source: repository.source.trim() || null,
      })),
      providerChecks: agentProfiles.map(({ id }) => ({
        profile: id,
        provider: agents[rowOf(id)].provider,
        runtime_id: agents[rowOf(id)].runtime,
        model: agents[rowOf(id)].model,
        reasoning: agents[rowOf(id)].reasoning,
        machine_id: canonicalMachine,
      })),
    });

  const setCurrentRequest = (next: ProjectProvisioningResponse) => {
    setRequest(next);
    setSavedRequests((current) => [
      next,
      ...current.filter((item) => item.request_id !== next.request_id),
    ]);
    setStep(3);
    if (typeof window !== "undefined") {
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${window.location.search}${projectProvisioningHash(next.request_id, routeGroupId(window.location.hash))}`,
      );
    }
  };

  const advance = async () => {
    const problem = validate(step);
    if (problem) {
      setError(problem);
      return;
    }
    if (step < 2) {
      setStep((current) => current + 1);
      setError(null);
      return;
    }
    setBusy("create-request");
    setError(null);
    try {
      setCurrentRequest(await createTeamProjectProvisioning(provisioningBody()));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const cancelRequest = async () => {
    if (!request) return;
    setBusy("cancel");
    setError(null);
    try {
      setCurrentRequest(await cancelProjectProvisioningRequest(request.request_id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const completeRequest = async () => {
    if (!request?.final_review) return;
    setBusy("complete");
    setError(null);
    try {
      const completed = await completeProjectProvisioningRequest(
        request.request_id,
        request.final_review.digest,
      );
      setCurrentRequest(completed);
      onCreated(completed.proposed_project_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const copyCommand = async () => {
    if (!request) return;
    setError(null);
    try {
      await navigator.clipboard.writeText(formatCommandArgv(request.operator_argv));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const saveAndProbeRoute = async () => {
    if (!connection) return;
    if (!operatorTarget.trim()) {
      setError("Enter the exact SSH target for the server operator route.");
      return;
    }
    setBusy("probe");
    setError(null);
    try {
      const updated = await configureDesktopServerOperatorRoute(connection.connection_id, {
        ssh_target: operatorTarget.trim(),
        mode: serverOperatorModeFor(operatorTarget),
      });
      setConnection(updated);
      setProbe(await probeDesktopServerOperator(updated.connection_id));
    } catch (caught) {
      setProbe(null);
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const runSetup = async () => {
    if (!request || !connection || !probe?.available) return;
    setBusy("run");
    setError(null);
    setEvents([]);
    try {
      await runDesktopProjectProvision(connection.connection_id, request.request_id, (event) =>
        setEvents((current) => [...current, event]),
      );
      setCurrentRequest(await loadProjectProvisioningRequest(request.request_id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const openTerminal = async () => {
    if (!request || !connection) return;
    setBusy("terminal");
    setError(null);
    try {
      await openDesktopProjectProvisionTerminal(connection.connection_id, request.request_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const goBack = () => {
    if (step === 0) {
      onCancel();
      return;
    }
    if (step === 3 && request) {
      setRequest(null);
      setEvents([]);
      setResumeRouteActive(false);
      setStep(0);
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${window.location.search}#/projects/new`,
      );
      return;
    }
    setStep((current) => current - 1);
    setError(null);
  };

  const createModeAvailable = projectProvisioningCreateModeAvailable(
    resumeLoading,
    resumeRouteActive,
    invalidResumeLink,
  );

  return (
    <main className="setup-layout">
      <nav className="setup-steps" aria-label="Project setup progress">
        <span className="eyebrow">Configuration route</span>
        {steps.map(([number, label], index) => (
          <button
            className={
              index === step
                ? "setup-step active"
                : index < step
                  ? "setup-step complete"
                  : "setup-step"
            }
            disabled={index > step || (step === 3 && Boolean(request))}
            aria-current={index === step ? "step" : undefined}
            key={number}
            onClick={() => {
              if (index < step && !request) setStep(index);
            }}
          >
            <span>{index < step ? <Check size={14} /> : number}</span>
            <strong>{label}</strong>
          </button>
        ))}
      </nav>

      <section className="setup-sheet">
        {resumeLoading && (
          <div className="setup-section setup-resume-loading" role="status">
            <LoaderCircle className="spin" size={20} />
            <strong>Loading the existing setup request</strong>
          </div>
        )}

        {createModeAvailable && step === 0 && (
          <div className="setup-section">
            {intentChooser}
            <SectionHeading
              eyebrow="Shared project"
              title="Name the project and its first repository."
            />
            <label className="setup-field">
              <span>Project name</span>
              <input
                autoFocus
                value={name}
                onChange={(event) => {
                  const projectName = event.target.value;
                  setName(projectName);
                  commitRepositories(
                    repositories.reduce(
                      (drafts, repository) => nameRepository(drafts, repository, projectName),
                      repositories,
                    ),
                  );
                }}
                placeholder="Continual RL Plasticity"
              />
            </label>
            <label className="setup-field">
              <span>GitHub URL (optional)</span>
              <input
                value={repositories[0].source}
                onChange={(event) =>
                  updateRepository(repositories[0].id, { source: event.target.value })
                }
                placeholder="https://github.com/lab/research.git"
              />
            </label>
            {!repositories[0].source.trim() && <p>Server only · Its code is not backed up.</p>}
            <p>
              New repositories start on main with an empty “Start RCP project” commit by RCP. For an
              empty GitHub repository, setup pushes it after you add the deploy key.
            </p>
            {savedRequests.length > 0 && (
              <div className="provisioning-resume-list">
                <strong>Existing setup requests</strong>
                {savedRequests.map((saved) => (
                  <button
                    type="button"
                    key={saved.request_id}
                    onClick={() => setCurrentRequest(saved)}
                  >
                    <span>{saved.name ?? saved.proposed_project_id}</span>
                    <strong>{saved.status_label}</strong>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {!resumeLoading && !invalidResumeLink && step === 1 && (
          <div className="setup-section">
            <SectionHeading
              eyebrow="Team-owned execution"
              title="Place every central checkout on its exact machine account."
            />
            {creatingMachine ? (
              <NewMachineForm
                accountRequired
                onCancel={() => setCreatingMachine(false)}
                onCreated={(machine) => {
                  setSpaceMachines((current) => [...current, machine]);
                  pickMachine(machine.machine_id);
                  setCreatingMachine(false);
                }}
              />
            ) : (
              <div className="machine-tiles" role="group" aria-label="Space machines">
                {spaceMachines.map((machine) => {
                  const picked = machines.some((item) => item.machine_id === machine.machine_id);
                  return (
                    <MachineTile
                      key={machine.machine_id}
                      machineId={machine.machine_id}
                      name={machine.name}
                      hostLabel={machineHostLabel(machine.host, "team")}
                      account={machine.os_account}
                      selected={picked}
                      onSelect={() =>
                        picked ? unpickMachine(machine.machine_id) : pickMachine(machine.machine_id)
                      }
                    />
                  );
                })}
                <AddMachineTile label="New machine" onSelect={() => setCreatingMachine(true)} />
              </div>
            )}
            {machines.some((machine) => cardFor(machine.machine_id)?.host) && (
              <div className="team-machine-fields">
                {machines
                  .filter((machine) => cardFor(machine.machine_id)?.host)
                  .map((machine) => (
                    <label className="wide" key={machine.machine_id}>
                      {machineCardName(spaceMachines, machine.machine_id)} central root (optional)
                      <input
                        value={machine.central_root}
                        onChange={(event) => {
                          const central_root = event.target.value;
                          setMachines((current) =>
                            current.map((item) =>
                              item.machine_id === machine.machine_id
                                ? { ...item, central_root }
                                : item,
                            ),
                          );
                          setError(null);
                        }}
                        placeholder="Defaults under the verified account home"
                      />
                    </label>
                  ))}
              </div>
            )}

            <div className="repository-stack team-repository-stack">
              {repositories.map((repository) => (
                <TeamRepositoryEditor
                  key={repository.id}
                  repository={repository}
                  machines={machines.map(({ machine_id }) => ({
                    machine_id,
                    name: machineCardName(spaceMachines, machine_id),
                  }))}
                  canonical={repository.alias === stateRepository}
                  only={repositories.length === 1}
                  onCanonical={() => setStateRepository(repository.alias)}
                  onChange={(patch) => updateRepository(repository.id, patch)}
                  onRemove={() => {
                    let remaining = repositories.filter((item) => item.id !== repository.id);
                    // A lone repository shows no name field, so its name is derived again.
                    if (remaining.length === 1) {
                      const lone = { ...remaining[0], nameEdited: false };
                      remaining = nameRepository([lone], lone, name);
                    }
                    setRepositories(remaining);
                    if (remaining.length === 1) setStateRepository(remaining[0].alias);
                    else if (repository.alias === stateRepository)
                      setStateRepository(remaining[0]?.alias ?? "");
                  }}
                />
              ))}
            </div>
            <button
              className="add-repository"
              type="button"
              onClick={() => {
                repositorySequence += 1;
                setRepositories((current) => [
                  ...current,
                  {
                    id: repositorySequence,
                    alias: defaultRepositoryName(
                      [name],
                      current.map((item) => item.alias.trim()),
                    ),
                    nameEdited: false,
                    source: "",
                    machine_id: machines[0]?.machine_id ?? "",
                  },
                ]);
              }}
            >
              <Plus size={16} /> <strong>Add repository</strong>
            </button>
          </div>
        )}

        {!resumeLoading && !invalidResumeLink && step === 2 && (
          <div className="setup-section">
            <SectionHeading
              eyebrow="Agent roles"
              title="Use provider authentication already present on each machine account."
            />
            <label className="agent-auto-research-default">
              <span>Default auto-research ceiling</span>
              <input
                type="number"
                min={1}
                step={1}
                value={defaultAutoResearchInvocationCeiling}
                onChange={(event) =>
                  setDefaultAutoResearchInvocationCeiling(Number(event.target.value))
                }
              />
            </label>
            <div className="agent-role-stack">
              {agentRows.map(({ id, label }) => {
                const profile = agents[id];
                const readiness = readinessFor(providers, profile.provider);
                const models = readiness?.models ?? [];
                const runtimes = runtimeOptions(readiness, profile.runtime);
                return (
                  <article className="agent-role-card" key={id}>
                    <header>
                      <strong>{label}</strong>
                      <span className="tag">
                        {id === "paper_coach"
                          ? "read-only coach"
                          : id === "orchestrator"
                            ? "auto-research"
                            : "graph patch only"}
                      </span>
                    </header>
                    <div className="agent-role-fields">
                      <label>
                        Provider
                        <select
                          value={profile.provider}
                          onChange={(event) => {
                            const next = readinessFor(providers, event.target.value);
                            updateAgent(id, {
                              ...providerChange(
                                next?.models ?? [],
                                event.target.value,
                                profile.reasoning,
                              ),
                              ...(next ? { runtime: next.default_runtime } : {}),
                            });
                          }}
                        >
                          {providerOptions(providers, profile.provider).map((option) => (
                            <option key={option.id} value={option.id}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Runtime
                        <select
                          value={profile.runtime}
                          disabled={runtimes.length < 2}
                          onChange={(event) => updateAgent(id, { runtime: event.target.value })}
                        >
                          {runtimes.map((option) => (
                            <option key={option.id} value={option.id}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Model
                        <select
                          value={profile.model}
                          onChange={(event) =>
                            updateAgent(
                              id,
                              modelChange(models, event.target.value, profile.reasoning),
                            )
                          }
                        >
                          {!profile.model && <option value="" disabled />}
                          {modelOptions(models, profile.model).map((option) => (
                            <option key={option.id} value={option.id}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Reasoning
                        <select
                          value={profile.reasoning}
                          onChange={(event) => updateAgent(id, { reasoning: event.target.value })}
                        >
                          {reasoningOptions(models, profile.model, profile.reasoning).map(
                            (option) => (
                              <option key={option.id} value={option.id}>
                                {option.label}
                              </option>
                            ),
                          )}
                        </select>
                      </label>
                    </div>
                  </article>
                );
              })}
            </div>
          </div>
        )}

        {!resumeLoading && !invalidResumeLink && step === 3 && request && (
          <ProvisioningStatus
            request={request}
            spaceMachines={spaceMachines}
            events={events}
            desktop={desktop}
            connection={connection}
            operatorTarget={operatorTarget}
            probe={probe}
            busy={busy}
            onOperatorTarget={(value) => {
              setOperatorTarget(value);
              setProbe(null);
            }}
            onSaveAndProbe={() => void saveAndProbeRoute()}
            onCopy={() => void copyCommand()}
            onRun={() => void runSetup()}
            onTerminal={() => void openTerminal()}
            onCancel={() => void cancelRequest()}
            onComplete={() => void completeRequest()}
          />
        )}

        {error && (
          <div className="setup-error" role="alert">
            <TriangleAlert size={16} />
            <span>{error}</span>
          </div>
        )}
        {!resumeLoading && !invalidResumeLink && (
          <footer className="setup-actions">
            <button
              className="button secondary"
              type="button"
              disabled={busy !== null}
              onClick={goBack}
            >
              <ArrowLeft size={16} /> Back
            </button>
            {step < 3 && createModeAvailable && (
              <button
                className="button primary"
                type="button"
                disabled={busy !== null}
                onClick={() => void advance()}
              >
                {busy === "create-request" ? (
                  <LoaderCircle className="spin" size={16} />
                ) : step === 2 ? (
                  <ShieldCheck size={16} />
                ) : null}
                {busy === "create-request"
                  ? "Creating request"
                  : step === 2
                    ? "Create setup request"
                    : "Continue"}
                {!busy && step < 2 && <ArrowRight size={16} />}
              </button>
            )}
          </footer>
        )}
      </section>

      {!resumeLoading && !request && !invalidResumeLink && (
        <aside className="boundary-ledger">
          <span className="eyebrow">Team boundary</span>
          <h2>What this setup means</h2>
          <LedgerItem
            number="A"
            label="Canonical state"
            value={stateRepository || "Not selected"}
          />
          <LedgerItem
            number="B"
            label="Central checkout owner"
            value={
              canonicalMachine ? machineCardName(spaceMachines, canonicalMachine) : "Not selected"
            }
          />
          <LedgerItem
            number="C"
            label="Repository identity"
            value={repositories
              .map(
                (repository) => `${repository.alias}: ${repository.source.trim() || "Server only"}`,
              )
              .join(" · ")}
          />
          <LedgerItem
            number="D"
            label="Provider authentication"
            value="Existing auth on each execution account"
          />
        </aside>
      )}
    </main>
  );
}

function TeamRepositoryEditor({
  repository,
  machines,
  canonical,
  only,
  onCanonical,
  onChange,
  onRemove,
}: {
  repository: TeamRepositoryDraft;
  machines: Array<{ machine_id: string; name: string }>;
  canonical: boolean;
  only: boolean;
  onCanonical: () => void;
  onChange: (patch: Partial<TeamRepositoryDraft>) => void;
  onRemove: () => void;
}) {
  return (
    <article className={canonical ? "repository-editor canonical" : "repository-editor"}>
      <header>
        <span className="repository-number">
          <FolderGit2 size={16} />
        </span>
        <strong>{repository.alias || "Unnamed repository"}</strong>
        {canonical && <span className="repository-state">Canonical state</span>}
        {!only && (
          <button
            className="icon-button compact"
            type="button"
            aria-label={`Remove ${repository.alias}`}
            onClick={onRemove}
          >
            <Trash2 size={14} />
          </button>
        )}
      </header>
      <div className="team-repository-fields">
        {!only && (
          <label>
            Name
            <input
              value={repository.alias}
              onChange={(event) => onChange({ alias: event.target.value, nameEdited: true })}
            />
          </label>
        )}
        <label className="wide">
          GitHub URL (optional)
          <input
            value={repository.source}
            onChange={(event) => onChange({ source: event.target.value })}
            placeholder="git@github.com:lab/research.git"
          />
        </label>
        <label>
          Central machine
          <select
            value={repository.machine_id}
            onChange={(event) => onChange({ machine_id: event.target.value })}
          >
            {!repository.machine_id && <option value="" disabled />}
            {machines.map((machine) => (
              <option key={machine.machine_id} value={machine.machine_id}>
                {machine.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!repository.source.trim() && <p>Server only · Its code is not backed up.</p>}
      <footer>
        <label className="radio-control">
          <input
            type="radio"
            name="team-canonical-repository"
            checked={canonical}
            onChange={onCanonical}
          />
          <span>Canonical state</span>
        </label>
      </footer>
    </article>
  );
}

/**
 * How a target reaches the service account follows from who it signs in as:
 * `rcp@host` already is it, and any other login enters it with sudo.
 */
export function serverOperatorModeFor(operatorTarget: string): ServerOperatorMode {
  const target = operatorTarget.trim();
  return target.includes("@") && target.split("@")[0] === "rcp" ? "direct_rcp" : "sudo_rcp";
}

export function serverOperatorProbeMatchesDraft(
  probe: ServerOperatorProbe | null,
  operatorTarget: string,
): boolean {
  return (
    probe?.available === true &&
    probe.route.ssh_target === operatorTarget.trim() &&
    probe.route.mode === serverOperatorModeFor(operatorTarget)
  );
}

/** One line naming who the project is for and what the ledger above proved. */
export function finalReviewSummary(request: ProjectProvisioningResponse): string {
  const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  const { machines_total, repositories_total, providers_total } = request.readiness;
  const owner =
    request.final_review?.authorized_by.display_name ?? request.authorized_by.display_name;
  return (
    `Prepared for ${owner}: ${count(machines_total, "machine", "machines")}, ` +
    `${count(repositories_total, "repository", "repositories")}, and ` +
    `${count(providers_total, "provider role", "provider roles")}, all checked above.`
  );
}

export function providerChipTone(provider: {
  ready: boolean;
  diagnostic: string | null;
}): "ready" | "failed" | "waiting" {
  if (provider.ready) return "ready";
  return provider.diagnostic ? "failed" : "waiting";
}

export function gitWriteFact(writeVerified: boolean): string {
  return writeVerified ? "Git write verified" : "Git write not verified";
}

export function projectProvisioningCreateModeAvailable(
  resumeLoading: boolean,
  resumeRouteActive: boolean,
  invalidResumeLink: boolean,
): boolean {
  return !resumeLoading && !resumeRouteActive && !invalidResumeLink;
}

export function ProvisioningStatus({
  request,
  spaceMachines = [],
  events,
  desktop,
  connection,
  operatorTarget,
  probe,
  busy,
  onOperatorTarget,
  onSaveAndProbe,
  onCopy,
  onRun,
  onTerminal,
  onCancel,
  onComplete,
}: {
  request: ProjectProvisioningResponse;
  spaceMachines?: SpaceMachine[];
  events: ServerCommandEvent[];
  desktop: boolean;
  connection: TeamConnectionMetadata | null;
  operatorTarget: string;
  probe: ServerOperatorProbe | null;
  busy: string | null;
  onOperatorTarget: (value: string) => void;
  onSaveAndProbe: () => void;
  onCopy: () => void;
  onRun: () => void;
  onTerminal: () => void;
  onCancel: () => void;
  onComplete: () => void;
}) {
  const operatorRouteReady = serverOperatorProbeMatchesDraft(probe, operatorTarget);
  const [overrideOpen, setOverrideOpen] = useState(false);
  return (
    <div className="setup-section provisioning-status">
      <SectionHeading
        eyebrow={request.target_project_id ? "Repository setup" : "Adding your RCP project"}
        title={request.name ?? "Shared project setup"}
      />
      <div className="provisioning-status-banner">
        <strong>{request.status_label}</strong>
        {request.next_action && <span>{request.next_action}</span>}
      </div>
      {request.diagnostic && (
        <div className="setup-error" role="alert">
          <TriangleAlert size={16} />
          <span>{request.diagnostic}</span>
        </div>
      )}
      <dl className="provisioning-summary">
        <div>
          <dt>Reserved project</dt>
          <dd>{request.proposed_project_id}</dd>
        </div>
        <div>
          <dt>Authorized by</dt>
          <dd>{request.authorized_by.display_name}</dd>
        </div>
        <div>
          <dt>Readiness</dt>
          <dd>
            {request.readiness.machines_ready}/{request.readiness.machines_total} machines ·{" "}
            {request.readiness.repositories_ready}/{request.readiness.repositories_total}{" "}
            repositories · {request.readiness.providers_ready}/{request.readiness.providers_total}{" "}
            provider roles
          </dd>
        </div>
      </dl>

      {request.can_run_setup &&
        request.kind !== "connect_repository" &&
        request.kind !== "incoming_transfer" && (
          <p>
            Empty repositories start on main with an empty “Start RCP project” commit by RCP.
            {request.repositories.some((repository) => repository.source_kind !== "server_only") &&
              " Setup pushes it to GitHub after you add the deploy key."}
          </p>
        )}
      {request.can_run_setup && (
        <p className="provisioning-hint">
          Copy the server command and run it in a shell on the server, logged in as yourself. It
          runs as rcp through sudo.
        </p>
      )}
      {desktop && connection && request.can_run_setup && !overrideOpen && (
        <p className="provisioning-hint">
          {connection.operator_route
            ? `The app runs setup over ${connection.operator_route.ssh_target}.`
            : `The app can run setup for you over your SSH login, ${connection.ssh_target}, if it can use sudo there without a password.`}{" "}
          Use a different login only if this one cannot use sudo on the server, such as when an
          admin account does setup.{" "}
          {!connection.operator_route && (
            <button
              className="button ghost tiny"
              type="button"
              disabled={busy !== null}
              onClick={onSaveAndProbe}
            >
              Check this login
            </button>
          )}
          <button className="button ghost tiny" type="button" onClick={() => setOverrideOpen(true)}>
            Use a different SSH login
          </button>
        </p>
      )}
      {desktop && connection && request.can_run_setup && overrideOpen && (
        <section className="operator-route-card">
          <header>
            <strong>SSH login for server setup</strong>
            <span>{operatorRouteReady ? "Ready" : "Not checked"}</span>
          </header>
          <div>
            <label>
              SSH target
              <input
                value={operatorTarget}
                onChange={(event) => onOperatorTarget(event.target.value)}
                placeholder="operator@server"
              />
            </label>
            <button
              className="button secondary"
              type="button"
              disabled={busy !== null}
              onClick={onSaveAndProbe}
            >
              {busy === "probe" ? (
                <LoaderCircle className="spin" size={14} />
              ) : (
                <ShieldCheck size={14} />
              )}{" "}
              Use this login
            </button>
          </div>
          <p className="provisioning-hint">
            {serverOperatorModeFor(operatorTarget) === "direct_rcp"
              ? "Signs in as rcp and runs the setup command directly."
              : "Signs in as this account, then runs the setup command as rcp with sudo. If sudo asks for a password, use Open in Terminal."}
          </p>
          {probe?.diagnostic && <p role="alert">{probe.diagnostic}</p>}
        </section>
      )}

      <div className="provisioning-controls">
        {request.can_run_setup && (
          <button
            className="button secondary"
            type="button"
            disabled={busy !== null}
            onClick={onCopy}
          >
            <Clipboard size={14} /> Copy server command
          </button>
        )}
        {desktop && connection && request.can_run_setup && operatorRouteReady && (
          <button className="button primary" type="button" disabled={busy !== null} onClick={onRun}>
            {busy === "run" ? <LoaderCircle className="spin" size={14} /> : <Server size={14} />}{" "}
            Run setup now
          </button>
        )}
        {desktop &&
          connection?.operator_route &&
          request.can_run_setup &&
          probe &&
          !probe.available && (
            <button
              className="button secondary"
              type="button"
              disabled={busy !== null}
              onClick={onTerminal}
            >
              <SquareTerminal size={14} /> Open in Terminal
            </button>
          )}
        {request.can_cancel && (
          <button
            className="button danger"
            type="button"
            disabled={busy !== null}
            onClick={onCancel}
          >
            Cancel request
          </button>
        )}
      </div>

      {events.length > 0 && (
        <section
          className="provisioning-events"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="Current server command progress"
        >
          {events.map((event, index) =>
            event.event === "plan" ? (
              <div key={`plan-${index}`}>
                <strong>Plan</strong>
                <span>
                  {event.steps.map((item) => `${item.number}. ${item.title}`).join(" · ")}
                </span>
              </div>
            ) : (
              <div key={`step-${index}`}>
                <strong>
                  {event.step.number}. {event.step.title}
                </strong>
                <span>{event.step.message}</span>
              </div>
            ),
          )}
        </section>
      )}

      {request.operator_action && (
        <OperatorActionPanel
          step={request.operator_action}
          route={connection?.operator_route ?? null}
          routeProved={routeProvedBy(probe, connection?.connection_id, connection?.operator_route)}
        />
      )}

      <section className="provisioning-ledger">
        <h2>Machines</h2>
        {request.machines.map((machine) => {
          const card = spaceMachines.find((item) => item.machine_id === machine.machine_id);
          return (
            <article key={machine.machine_id}>
              <strong>
                {card?.name ?? machine.machine_id} · {machine.status_label}
              </strong>
              {card && (
                <span>
                  {machineHostLabel(card.host, "team")}
                  {card.os_account ? ` · ${card.os_account}` : ""}
                </span>
              )}
              <code>
                {machine.resolved_central_root ??
                  machine.intended_central_root ??
                  "Home-derived path pending"}
              </code>
            </article>
          );
        })}
        <h2>Repositories</h2>
        {request.repositories.map((repository) => (
          <article key={repository.alias}>
            <strong>
              {repository.alias} · {repository.status_label}
            </strong>
            <span>{repository.repository?.identity ?? "Server only"}</span>
            {request.kind === "add_repository" && (
              <span>
                {repository.count_as_project_truth
                  ? "Counts as project truth"
                  : "Outside project truth"}
              </span>
            )}
            {repository.source_kind === "server_only" ? (
              <p>Its code is not backed up.</p>
            ) : (
              <span>{gitWriteFact(repository.write_verified)}</span>
            )}
            <code>{repository.resolved_path ?? repository.intended_path ?? "Path pending"}</code>
            {repository.diagnostic && <p>{repository.diagnostic}</p>}
          </article>
        ))}
        <h2>Provider roles</h2>
        <div className="provider-role-chips">
          {request.provider_checks.map((provider) => (
            <span
              key={provider.profile}
              className={`provider-role-chip ${providerChipTone(provider)}`}
              title={[
                provider.status_label,
                `${provider.provider} · ${provider.runtime_id} · ${machineCardName(spaceMachines, provider.machine_id)}`,
                provider.execution_account &&
                  `${provider.execution_account} · ${provider.binary_path}`,
              ]
                .filter(Boolean)
                .join("\n")}
            >
              {provider.profile}
            </span>
          ))}
        </div>
        {request.provider_checks
          .filter((provider) => provider.diagnostic)
          .map((provider) => (
            <p key={provider.profile} className="provider-role-diagnostic">
              <strong>{provider.profile}</strong> {provider.diagnostic}
            </p>
          ))}
      </section>

      {request.final_review && (
        <section className="provisioning-final-review">
          <h2>Final review</h2>
          <p>{finalReviewSummary(request)}</p>
          {request.can_review && (
            <button
              className="button primary"
              type="button"
              disabled={busy !== null}
              onClick={onComplete}
            >
              {busy === "complete" ? (
                <LoaderCircle className="spin" size={14} />
              ) : (
                <Check size={14} />
              )}{" "}
              {request.kind === "add_repository" || request.kind === "connect_repository"
                ? "Confirm repository"
                : "Confirm and create project"}
            </button>
          )}
        </section>
      )}
    </div>
  );
}

function SectionHeading({ eyebrow, title }: { eyebrow: string; title: string }) {
  return (
    <header className="setup-section-heading">
      <span className="eyebrow">{eyebrow}</span>
      <h1>{title}</h1>
    </header>
  );
}

function LedgerItem({ number, label, value }: { number: string; label: string; value: string }) {
  return (
    <div className="ledger-item">
      <span>{number}</span>
      <div>
        <small>{label}</small>
        <strong>{value}</strong>
      </div>
    </div>
  );
}
