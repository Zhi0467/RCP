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
  formatCommandArgv,
  invalidProjectProvisioningHash,
  projectProvisioningHash,
  projectProvisioningRequestId,
  routeProvedBy,
} from "./projectSetupModel";
import { OperatorActionPanel } from "./OperatorActionPanel";
import type {
  AgentExecutionProfile,
  ProjectProvisioningCreateRequest,
  ProjectProvisioningResponse,
  ProviderReadiness,
  SetupAgentProfile,
  SetupAgents,
} from "../core/types";

interface Props {
  intentChooser: ReactNode;
  onCancel: () => void;
  onCreated: (projectId: string) => void;
}

interface TeamMachineDraft {
  id: number;
  alias: string;
  location: "local" | "ssh";
  host: string;
  os_account: string;
  central_root: string;
}

interface TeamRepositoryDraft {
  id: number;
  alias: string;
  source: string;
  machine_alias: string;
  default_read: boolean;
}

const steps = [
  ["01", "Project"],
  ["02", "Machines & repositories"],
  ["03", "Agent roles"],
  ["04", "Server setup"],
] as const;

const agentProfiles: Array<{ id: AgentExecutionProfile; label: string }> = [
  { id: "seed", label: "Seed" },
  { id: "refresh", label: "Refresh" },
  { id: "node_chat", label: "Node chat" },
  { id: "project_chat", label: "Project chat" },
  { id: "paper_coach", label: "Paper coach" },
  { id: "orchestrator", label: "Auto-research orchestrator" },
];

let machineSequence = 1;
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
  const [machines, setMachines] = useState<TeamMachineDraft[]>([
    {
      id: machineSequence,
      alias: "server",
      location: "local",
      host: "",
      os_account: "rcp",
      central_root: "",
    },
  ]);
  const [repositories, setRepositories] = useState<TeamRepositoryDraft[]>([
    {
      id: repositorySequence,
      alias: "research",
      source: "",
      machine_alias: "server",
      default_read: true,
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
  const [paperCoachMachine, setPaperCoachMachine] = useState("server");
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
    return repository?.machine_alias ?? machines[0]?.alias ?? "";
  }, [machines, repositories, stateRepository]);

  const updateAgent = (profile: AgentExecutionProfile, patch: Partial<SetupAgentProfile>) => {
    setAgents((current) => ({ ...current, [profile]: { ...current[profile], ...patch } }));
    setError(null);
  };

  const validate = (targetStep: number): string | null => {
    if (!name.trim()) return "Give this shared project a name.";
    if (targetStep < 1) return null;
    const machineAliases = machines.map((machine) => machine.alias.trim());
    const repositoryAliases = repositories.map((repository) => repository.alias.trim());
    if (machineAliases.some((alias) => !/^[a-z][a-z0-9-]{0,47}$/.test(alias))) {
      return "Machine aliases must use lowercase letters, numbers, or hyphens.";
    }
    if (new Set(machineAliases).size !== machineAliases.length) {
      return "Each machine needs a unique alias.";
    }
    if (repositoryAliases.some((alias) => !/^[a-z][a-z0-9-]{0,47}$/.test(alias))) {
      return "Repository aliases must use lowercase letters, numbers, or hyphens.";
    }
    if (new Set(repositoryAliases).size !== repositoryAliases.length) {
      return "Each repository needs a unique alias.";
    }
    if (machines.some((machine) => machine.location === "ssh" && !machine.host.trim())) {
      return "Every SSH machine needs an SSH host.";
    }
    if (machines.some((machine) => !machine.os_account.trim())) {
      return "Every machine needs its exact Linux account.";
    }
    if (
      machines.some(
        (machine) =>
          machine.location === "ssh" &&
          machine.central_root.trim() &&
          (!machine.central_root.trim().startsWith("/") || machine.central_root.trim() === "/"),
      )
    ) {
      return "An explicit SSH central root must be a specific absolute path.";
    }
    if (repositories.some((repository) => !machineAliases.includes(repository.machine_alias))) {
      return "Every repository must name one configured machine.";
    }
    if (!repositoryAliases.includes(stateRepository)) {
      return "Choose a canonical state repository.";
    }
    if (!repositories.some((repository) => repository.default_read)) {
      return "Select at least one repository for default agent reads.";
    }
    if (targetStep < 2) return null;
    if (
      !Number.isSafeInteger(defaultAutoResearchInvocationCeiling) ||
      defaultAutoResearchInvocationCeiling < 1
    ) {
      return "Set the default auto-research ceiling to at least 1 operational invocation.";
    }
    if (!machines.some((machine) => machine.alias === paperCoachMachine)) {
      return "Choose a configured machine for Paper coach.";
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
      machines: machines.map(({ id: _id, central_root, ...machine }) => ({
        ...machine,
        alias: machine.alias.trim(),
        host: machine.location === "ssh" ? machine.host.trim() : "",
        os_account: machine.location === "local" ? "rcp" : machine.os_account.trim(),
        central_root:
          machine.location === "ssh" && central_root.trim() ? central_root.trim() : null,
      })),
      repositories: repositories.map(({ id: _id, ...repository }) => ({
        ...repository,
        alias: repository.alias.trim(),
        source: repository.source.trim() || null,
      })),
      providerChecks: agentProfiles.map(({ id }) => ({
        profile: id,
        provider: agents[id].provider,
        runtime_id: agents[id].runtime,
        model: agents[id].model,
        reasoning: agents[id].reasoning,
        machine_alias: id === "paper_coach" ? paperCoachMachine : canonicalMachine,
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
        `${window.location.pathname}${window.location.search}${projectProvisioningHash(next.request_id)}`,
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
                  setName(event.target.value);
                  setError(null);
                }}
                placeholder="Continual RL Plasticity"
              />
            </label>
            <label className="setup-field">
              <span>GitHub URL (optional)</span>
              <input
                value={repositories[0].source}
                onChange={(event) => {
                  setRepositories((current) =>
                    current.map((item, index) =>
                      index === 0 ? { ...item, source: event.target.value } : item,
                    ),
                  );
                  setError(null);
                }}
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
            <div className="team-machine-stack">
              {machines.map((machine) => (
                <MachineEditor
                  key={machine.id}
                  machine={machine}
                  only={machines.length === 1}
                  onChange={(patch) => {
                    if (patch.alias !== undefined && patch.alias !== machine.alias) {
                      setRepositories((current) =>
                        current.map((item) =>
                          item.machine_alias === machine.alias
                            ? { ...item, machine_alias: patch.alias! }
                            : item,
                        ),
                      );
                      if (paperCoachMachine === machine.alias) {
                        setPaperCoachMachine(patch.alias);
                      }
                    }
                    setMachines((current) =>
                      current.map((item) =>
                        item.id === machine.id ? { ...item, ...patch } : item,
                      ),
                    );
                    setError(null);
                  }}
                  onRemove={() => {
                    const remaining = machines.filter((item) => item.id !== machine.id);
                    const replacement = remaining[0]?.alias ?? "";
                    setMachines(remaining);
                    setRepositories((current) =>
                      current.map((item) =>
                        item.machine_alias === machine.alias
                          ? { ...item, machine_alias: replacement }
                          : item,
                      ),
                    );
                    if (paperCoachMachine === machine.alias) setPaperCoachMachine(replacement);
                  }}
                />
              ))}
            </div>
            <button
              className="add-repository"
              type="button"
              onClick={() => {
                machineSequence += 1;
                setMachines((current) => [
                  ...current,
                  {
                    id: machineSequence,
                    alias: `machine-${machineSequence}`,
                    location: "ssh",
                    host: "",
                    os_account: "",
                    central_root: "",
                  },
                ]);
              }}
            >
              <Plus size={16} /> <strong>Add machine</strong>
            </button>

            <div className="repository-stack team-repository-stack">
              {repositories.map((repository) => (
                <TeamRepositoryEditor
                  key={repository.id}
                  repository={repository}
                  machines={machines}
                  canonical={repository.alias === stateRepository}
                  only={repositories.length === 1}
                  onCanonical={() => setStateRepository(repository.alias)}
                  onChange={(patch) => {
                    if (patch.alias !== undefined && repository.alias === stateRepository)
                      setStateRepository(patch.alias);
                    setRepositories((current) =>
                      current.map((item) =>
                        item.id === repository.id ? { ...item, ...patch } : item,
                      ),
                    );
                    setError(null);
                  }}
                  onRemove={() => {
                    const remaining = repositories.filter((item) => item.id !== repository.id);
                    setRepositories(remaining);
                    if (repository.alias === stateRepository)
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
                    alias: `repository-${repositorySequence}`,
                    source: "",
                    machine_alias: machines[0]?.alias ?? "",
                    default_read: true,
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
              {agentProfiles.map(({ id, label }) => {
                const profile = agents[id];
                const readiness = readinessFor(providers, profile.provider);
                const models = readiness?.models ?? [];
                const runtimes = runtimeOptions(readiness, profile.runtime);
                const machine = id === "paper_coach" ? paperCoachMachine : canonicalMachine;
                return (
                  <article className="agent-role-card" key={id}>
                    <header>
                      <strong>{label}</strong>
                      <span className="role-permission">
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
                      <label>
                        Run on
                        <select
                          value={machine}
                          disabled={id !== "paper_coach"}
                          onChange={(event) => setPaperCoachMachine(event.target.value)}
                        >
                          {machines.map((item) => (
                            <option key={item.alias} value={item.alias}>
                              {item.alias}
                            </option>
                          ))}
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
            value={canonicalMachine || "Not selected"}
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

function MachineEditor({
  machine,
  only,
  onChange,
  onRemove,
}: {
  machine: TeamMachineDraft;
  only: boolean;
  onChange: (patch: Partial<TeamMachineDraft>) => void;
  onRemove: () => void;
}) {
  return (
    <article className="team-machine-editor">
      <header>
        <Server size={16} />
        <strong>{machine.alias || "Unnamed machine"}</strong>
        {!only && (
          <button
            className="icon-button compact"
            type="button"
            aria-label={`Remove ${machine.alias}`}
            onClick={onRemove}
          >
            <Trash2 size={14} />
          </button>
        )}
      </header>
      <div className="team-machine-fields">
        <label>
          Alias
          <input
            value={machine.alias}
            onChange={(event) => onChange({ alias: event.target.value })}
          />
        </label>
        <label>
          Location
          <select
            value={machine.location}
            onChange={(event) =>
              onChange(
                event.target.value === "local"
                  ? { location: "local", host: "", os_account: "rcp", central_root: "" }
                  : { location: "ssh" },
              )
            }
          >
            <option value="local">RCP server</option>
            <option value="ssh">SSH machine</option>
          </select>
        </label>
        {machine.location === "ssh" ? (
          <>
            <label>
              SSH host
              <input
                value={machine.host}
                onChange={(event) => onChange({ host: event.target.value })}
                placeholder="gpu.example.edu"
              />
            </label>
            <label>
              Linux account
              <input
                value={machine.os_account}
                onChange={(event) => onChange({ os_account: event.target.value })}
                placeholder="alice"
              />
            </label>
            <label className="wide">
              Central root (optional)
              <input
                value={machine.central_root}
                onChange={(event) => onChange({ central_root: event.target.value })}
                placeholder="Defaults under the verified account home"
              />
            </label>
          </>
        ) : (
          <>
            <label>
              Linux account
              <input value="rcp" disabled />
            </label>
            <label className="wide">
              Central root
              <input value="/var/lib/rcp/projects" disabled />
            </label>
          </>
        )}
      </div>
    </article>
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
  machines: TeamMachineDraft[];
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
        <label>
          Alias
          <input
            value={repository.alias}
            onChange={(event) => onChange({ alias: event.target.value })}
          />
        </label>
        <label>
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
            value={repository.machine_alias}
            onChange={(event) => onChange({ machine_alias: event.target.value })}
          >
            {machines.map((machine) => (
              <option key={machine.alias} value={machine.alias}>
                {machine.alias}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!repository.source.trim() && <p>Server only · Its code is not backed up.</p>}
      <footer>
        <label className="check-control">
          <input
            type="checkbox"
            checked={repository.default_read}
            onChange={(event) => onChange({ default_read: event.target.checked })}
          />
          <strong>Default raw input</strong>
        </label>
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
        {request.machines.map((machine) => (
          <article key={machine.alias}>
            <strong>
              {machine.alias} · {machine.status_label}
            </strong>
            <span>
              {machine.location === "ssh"
                ? `${machine.os_account}@${machine.host}`
                : machine.os_account}
            </span>
            <code>
              {machine.resolved_central_root ??
                machine.intended_central_root ??
                "Home-derived path pending"}
            </code>
          </article>
        ))}
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
                `${provider.provider} · ${provider.runtime_id} · ${provider.machine_alias}`,
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
