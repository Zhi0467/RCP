import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  FileCode2,
  FolderGit2,
  LoaderCircle,
  LockKeyhole,
  Plus,
  ShieldCheck,
  Trash2,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import {
  modelChange,
  modelOptions,
  modelsFor,
  providerChange,
  providerOptions,
  reasoningOptions,
} from "../providers";
import type {
  AgentSurface,
  ProviderReadiness,
  ProjectCard,
  ProjectSetupRequest,
  SetupAgentProfile,
  SetupAgents,
  SetupPreview,
  SetupRepository,
} from "../types";

interface Props {
  onCancel: () => void;
  onCreated: (projectId: string) => void;
}

interface DraftRepository extends SetupRepository {
  id: number;
}

const steps = [
  ["01", "Project"],
  ["02", "Truth boundary"],
  ["03", "Agent roles"],
  ["04", "Verify"],
] as const;

let repositorySequence = 1;

const agentSurfaces: Array<{ id: AgentSurface; label: string }> = [
  { id: "seed", label: "Seed" },
  { id: "refresh", label: "Refresh" },
  { id: "node_chat", label: "Node chat" },
  { id: "project_chat", label: "Project chat" },
  { id: "paper_coach", label: "Paper coach" },
];

// The provider is filled from the registry once it answers; the backend lists
// its default first. Hardcoding one here is what this whole change removes.
const defaultAgentProfile = (model = ""): SetupAgentProfile => ({
  provider: "",
  model,
  reasoning: "medium",
  location: "local",
  host: "",
});

export function ProjectSetup({ onCancel, onCreated }: Props) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [repositories, setRepositories] = useState<DraftRepository[]>([
    {
      id: repositorySequence,
      alias: "research",
      location: "local",
      path: "",
      host: "",
      default_read: true,
    },
  ]);
  const [stateRepository, setStateRepository] = useState("research");
  const [agents, setAgents] = useState<SetupAgents>({
    seed: defaultAgentProfile(),
    refresh: defaultAgentProfile(),
    node_chat: defaultAgentProfile(),
    project_chat: defaultAgentProfile(),
    paper_coach: defaultAgentProfile("gpt-5.6-luna"),
  });
  const [preview, setPreview] = useState<SetupPreview | null>(null);
  // Agent defaults are chosen before any manifest exists, so there is no
  // per-machine readiness to read yet; ask the registry what this machine has.
  const [providers, setProviders] = useState<ProviderReadiness[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<"preflight" | "create" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void api<ProviderReadiness[]>("/api/providers")
      .then((known) => {
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
                  : { ...profile, provider: fallback },
              ]),
            ) as SetupAgents,
        );
      })
      .catch(() => setProviders([]));
  }, []);

  const remoteHosts = useMemo(
    () => [
      ...new Set(
        repositories
          .filter((repo) => repo.location === "ssh")
          .map((repo) => repo.host.trim())
          .filter(Boolean),
      ),
    ],
    [repositories],
  );
  const stateRepo = repositories.find((repo) => repo.alias === stateRepository) ?? repositories[0];
  const defaultRead = repositories.filter((repo) => repo.default_read);
  const canonicalExecution = {
    location: stateRepo.location,
    host: stateRepo.location === "ssh" ? stateRepo.host.trim() : "",
  } as const;
  const payload = (): ProjectSetupRequest => ({
    name: name.trim(),
    repositories: repositories.map(({ id: _id, ...repo }) => ({
      ...repo,
      alias: repo.alias.trim(),
      path: repo.path.trim(),
      host: repo.location === "ssh" ? repo.host.trim() : "",
    })),
    state_repository: stateRepository,
    execution: canonicalExecution,
    agents: Object.fromEntries(
      agentSurfaces.map(({ id }) => [
        id,
        {
          ...agents[id],
          ...(id === "paper_coach"
            ? {
                host: agents[id].location === "ssh" ? agents[id].host.trim() : "",
              }
            : canonicalExecution),
        },
      ]),
    ) as SetupAgents,
    confirmed: false,
  });

  const updateRepository = (id: number, patch: Partial<SetupRepository>) => {
    const currentRepository = repositories.find((repo) => repo.id === id);
    if (patch.alias !== undefined && currentRepository?.alias === stateRepository) {
      setStateRepository(patch.alias);
    }
    setRepositories((current) =>
      current.map((repo) => {
        if (repo.id !== id) return repo;
        const next = { ...repo, ...patch };
        if (patch.location === "local") next.host = "";
        return next;
      }),
    );
    setError(null);
  };

  const validate = (targetStep: number): string | null => {
    if (!name.trim()) return "Give this paper-project a name.";
    if (!repositories[0].path.trim()) return "Enter the first repository's absolute path.";
    if (repositories[0].location === "ssh" && !repositories[0].host.trim()) {
      return "Enter the SSH host for the first repository.";
    }
    if (targetStep < 1) return null;
    const aliases = repositories.map((repo) => repo.alias.trim());
    if (aliases.some((alias) => !/^[a-z][a-z0-9-]{0,47}$/.test(alias))) {
      return "Aliases must start with a lowercase letter and use only lowercase letters, numbers, or hyphens.";
    }
    if (new Set(aliases).size !== aliases.length) return "Each repository needs a unique alias.";
    if (repositories.some((repo) => !repo.path.trim()))
      return "Every repository needs an absolute path.";
    if (repositories.some((repo) => repo.location === "ssh" && !repo.host.trim())) {
      return "Every SSH repository needs a host.";
    }
    if (!repositories.some((repo) => repo.default_read)) {
      return "Select at least one repository for default agent reads.";
    }
    if (!aliases.includes(stateRepository)) return "Choose a canonical state repository.";
    if (targetStep < 2) return null;
    const invalidAgent = agentSurfaces.find(
      ({ id }) =>
        id === "paper_coach" &&
        agents[id].location === "ssh" &&
        !remoteHosts.includes(agents[id].host),
    );
    if (invalidAgent) {
      return `Choose one of the repository hosts for the ${invalidAgent.label} agent.`;
    }
    return null;
  };

  const updateAgent = (surface: AgentSurface, patch: Partial<SetupAgentProfile>) => {
    setAgents((current) => {
      const next = { ...current[surface], ...patch };
      if (patch.location === "local") next.host = "";
      return { ...current, [surface]: next };
    });
    setError(null);
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
    setBusy("preflight");
    setError(null);
    setPreview(null);
    try {
      const result = await api<SetupPreview>("/api/project-setup/preflight", {
        method: "POST",
        body: JSON.stringify(payload()),
      });
      setPreview(result);
      setStep(3);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const create = async () => {
    if (!preview?.can_create || !confirmed || busy) return;
    setBusy("create");
    setError(null);
    try {
      const created = await api<ProjectCard>("/api/project-setup/create", {
        method: "POST",
        body: JSON.stringify({ ...payload(), confirmed: true }),
      });
      onCreated(created.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setConfirmed(false);
    } finally {
      setBusy(null);
    }
  };

  const addRepository = () => {
    repositorySequence += 1;
    const alias = `repository-${repositorySequence}`;
    setRepositories((current) => [
      ...current,
      {
        id: repositorySequence,
        alias,
        location: "local",
        path: "",
        host: "",
        default_read: true,
      },
    ]);
  };

  const removeRepository = (id: number) => {
    setRepositories((current) => {
      const removed = current.find((repo) => repo.id === id);
      const next = current.filter((repo) => repo.id !== id);
      if (removed?.alias === stateRepository) setStateRepository(next[0].alias);
      return next;
    });
  };

  const goBack = () => {
    if (step === 0) {
      onCancel();
      return;
    }
    setStep((current) => current - 1);
    setPreview(null);
    setConfirmed(false);
    setError(null);
  };

  return (
    <div className="setup-shell">
      <header className="setup-header">
        <button
          className="rcp-mark setup-brand"
          onClick={onCancel}
          aria-label="Return to project index"
        >
          <span className="rcp-wordmark" aria-hidden="true">
            RCP
          </span>
        </button>
        <span className="setup-header-title">Add project</span>
        <button className="button ghost" onClick={onCancel}>
          Cancel
        </button>
      </header>

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
              disabled={index > step}
              key={number}
              onClick={() => {
                if (index < step) {
                  setStep(index);
                  setPreview(null);
                  setConfirmed(false);
                  setError(null);
                }
              }}
            >
              <span>{index < step ? <Check size={13} /> : number}</span>
              <strong>{label}</strong>
            </button>
          ))}
        </nav>

        <section className="setup-sheet">
          {step === 0 && (
            <div className="setup-section">
              <SectionHeading
                eyebrow="Project identity"
                title="Start with the paper and one repository."
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
              <div className="setup-rule" />
              <RepositoryEditor
                repository={repositories[0]}
                canonical={stateRepository === repositories[0].alias}
                only
                onCanonical={() => setStateRepository(repositories[0].alias)}
                onChange={(patch) => updateRepository(repositories[0].id, patch)}
              />
            </div>
          )}

          {step === 1 && (
            <div className="setup-section">
              <SectionHeading
                eyebrow="Guarded truth boundary"
                title="Which repositories belong to this project?"
              />
              <div className="repository-stack">
                {repositories.map((repository) => (
                  <RepositoryEditor
                    key={repository.id}
                    repository={repository}
                    canonical={stateRepository === repository.alias}
                    only={repositories.length === 1}
                    onCanonical={() => setStateRepository(repository.alias)}
                    onChange={(patch) => updateRepository(repository.id, patch)}
                    onRemove={() => removeRepository(repository.id)}
                  />
                ))}
              </div>
              <button className="add-repository" onClick={addRepository}>
                <Plus size={16} />
                <strong>Add another repository</strong>
              </button>
            </div>
          )}

          {step === 2 && (
            <div className="setup-section">
              <SectionHeading eyebrow="Agent roles" title="Choose the agent behind each surface." />
              <div className="agent-role-stack">
                {agentSurfaces.map(({ id, label }) => {
                  const profile = agents[id];
                  const models = modelsFor(providers, profile.provider);
                  const execution = id === "paper_coach" ? profile : canonicalExecution;
                  const machineValue =
                    execution.location === "local" ? "local" : `ssh:${execution.host}`;
                  return (
                    <article className="agent-role-card" key={id}>
                      <header>
                        <strong>{label}</strong>
                        <span className="role-permission">
                          {id === "paper_coach" ? "read-only coach" : "graph patch only"}
                        </span>
                      </header>
                      <div className="agent-role-fields">
                        <label>
                          Provider
                          <select
                            value={profile.provider}
                            onChange={(event) =>
                              updateAgent(
                                id,
                                providerChange(
                                  modelsFor(providers, event.target.value),
                                  event.target.value,
                                  profile.reasoning,
                                ),
                              )
                            }
                          >
                            {providerOptions(providers, profile.provider).map((option) => (
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
                        <label className={id === "paper_coach" ? undefined : "agent-machine-fixed"}>
                          Run on{" "}
                          {id === "paper_coach" ? null : (
                            <LockKeyhole size={10} aria-hidden="true" />
                          )}
                          <select
                            value={machineValue}
                            disabled={id !== "paper_coach"}
                            onChange={(event) => {
                              const value = event.target.value;
                              updateAgent(
                                id,
                                value === "local"
                                  ? { location: "local", host: "" }
                                  : { location: "ssh", host: value.slice(4) },
                              );
                            }}
                          >
                            <option value="local">This machine</option>
                            {remoteHosts.map((host) => (
                              <option key={host} value={`ssh:${host}`}>
                                {host}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <div className="role-contract">
                        <LockKeyhole size={13} />{" "}
                        {id === "paper_coach"
                          ? "Introduction and project inputs are read-only · no writes"
                          : "Project and run-scope inputs are read-only · graph patch output only"}
                      </div>
                    </article>
                  );
                })}
              </div>
            </div>
          )}

          {step === 3 && preview && (
            <div className="setup-section review-section">
              <SectionHeading
                eyebrow="Read-only preflight complete"
                title={
                  preview.action === "connect"
                    ? "Connect the existing RCP project."
                    : "The project is ready to initialize."
                }
              />
              {preview.action === "connect" && (
                <div className="existing-manifest">
                  <FileCode2 size={19} />
                  <span>
                    Existing manifest: <strong>{preview.existing_project_name}</strong> · setup
                    entries will not overwrite it
                  </span>
                </div>
              )}
              <div className="preflight-checks">
                {preview.checks.map((check, index) => (
                  <div
                    className={`preflight-check ${check.status}`}
                    key={`${check.label}-${index}`}
                  >
                    {check.status === "pass" && <CheckCircle2 size={17} />}
                    {check.status === "warn" && <TriangleAlert size={17} />}
                    {check.status === "fail" && <XCircle size={17} />}
                    <strong>{check.label}</strong>
                    <span className="preflight-check-detail">{check.detail}</span>
                  </div>
                ))}
              </div>
              <details className="manifest-preview">
                <summary>
                  <FileCode2 size={15} /> Manifest{" "}
                  {preview.action === "connect" ? "to connect" : "preview"}
                </summary>
                <pre>{preview.manifest_preview}</pre>
              </details>
              <label
                className={
                  preview.can_create ? "final-confirmation" : "final-confirmation disabled"
                }
              >
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={!preview.can_create}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                <span>
                  {preview.action === "connect"
                    ? "Connect this project"
                    : "Create the project manifest"}{" "}
                  ·{" "}
                  {preview.remote_write
                    ? `RCP may write canonical project state over SSH at ${preview.canonical_location}`
                    : `RCP may initialize canonical project state at ${preview.canonical_location}`}
                </span>
              </label>
            </div>
          )}

          {error && (
            <div className="setup-error" role="alert">
              <TriangleAlert size={16} />
              <span>{error}</span>
            </div>
          )}
          <footer className="setup-actions">
            <button className="button secondary" onClick={goBack}>
              <ArrowLeft size={15} /> Back
            </button>
            {step < 3 && (
              <button
                className="button primary"
                disabled={busy !== null}
                onClick={() => void advance()}
              >
                {busy === "preflight" ? (
                  <LoaderCircle className="spin" size={15} />
                ) : step === 2 ? (
                  <ShieldCheck size={15} />
                ) : null}
                {busy === "preflight"
                  ? "Checking"
                  : step === 2
                    ? "Run read-only preflight"
                    : "Continue"}
                {!busy && step < 2 && <ArrowRight size={15} />}
              </button>
            )}
            {step === 3 && preview && (
              <button
                className="button primary"
                disabled={!preview.can_create || !confirmed || busy !== null}
                onClick={() => void create()}
              >
                {busy === "create" ? (
                  <LoaderCircle className="spin" size={15} />
                ) : (
                  <Check size={15} />
                )}
                {busy === "create"
                  ? "Opening project"
                  : preview.action === "connect"
                    ? "Connect and open"
                    : "Create and open"}
              </button>
            )}
          </footer>
        </section>

        <aside className="boundary-ledger">
          <span className="eyebrow">Live boundary ledger</span>
          <h2>What this setup means</h2>
          <LedgerItem
            number="A"
            label="Canonical state"
            value={stateRepo ? repositoryLocation(stateRepo) : "Not selected"}
          />
          <LedgerItem
            number="B"
            label="Project graph"
            value={`${repositories.length} truth repositor${repositories.length === 1 ? "y" : "ies"}`}
          />
          <LedgerItem
            number="C"
            label="Raw prompt inputs"
            value={
              defaultRead.length
                ? defaultRead.map((repo) => repo.alias || "unnamed").join(", ")
                : "None selected"
            }
          />
          <LedgerItem
            number="D"
            label="Agent roles"
            value={agentSurfaces
              .map(({ id, label }) => {
                const execution = id === "paper_coach" ? agents[id] : canonicalExecution;
                return `${label}: ${agents[id].provider} @ ${execution.location === "ssh" ? execution.host || "remote" : "local"}`;
              })
              .join(" · ")}
          />
        </aside>
      </main>
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

function RepositoryEditor({
  repository,
  canonical,
  only,
  onCanonical,
  onChange,
  onRemove,
}: {
  repository: DraftRepository;
  canonical: boolean;
  only: boolean;
  onCanonical: () => void;
  onChange: (patch: Partial<SetupRepository>) => void;
  onRemove?: () => void;
}) {
  return (
    <article className={canonical ? "repository-editor canonical" : "repository-editor"}>
      <header>
        <span className="repository-number">
          <FolderGit2 size={16} />
        </span>
        <strong>{repository.alias || "Unnamed repository"}</strong>
        {canonical && <span className="repository-state">Canonical state</span>}
        {!only && onRemove && (
          <button
            className="icon-button compact"
            aria-label={`Remove ${repository.alias}`}
            onClick={onRemove}
          >
            <Trash2 size={14} />
          </button>
        )}
      </header>
      <div className="repository-fields">
        <label>
          <span>Alias</span>
          <input
            value={repository.alias}
            onChange={(event) => onChange({ alias: event.target.value })}
            placeholder="research-code"
          />
        </label>
        <div className="location-toggle" aria-label="Repository location">
          <button
            className={repository.location === "local" ? "active" : ""}
            onClick={() => onChange({ location: "local" })}
          >
            Local
          </button>
          <button
            className={repository.location === "ssh" ? "active" : ""}
            onClick={() => onChange({ location: "ssh" })}
          >
            SSH
          </button>
        </div>
        {repository.location === "ssh" && (
          <label>
            <span>SSH host</span>
            <input
              value={repository.host}
              onChange={(event) => onChange({ host: event.target.value })}
              placeholder="gpu.example.edu"
            />
          </label>
        )}
        <label className={repository.location === "ssh" ? "" : "wide"}>
          <span>Absolute repository path</span>
          <input
            value={repository.path}
            onChange={(event) => onChange({ path: event.target.value })}
            placeholder={
              repository.location === "ssh"
                ? "/home/user/research/project"
                : "/Users/you/research/project"
            }
          />
        </label>
      </div>
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
            name="canonical-repository"
            checked={canonical}
            onChange={onCanonical}
          />
          <span>Canonical state</span>
        </label>
      </footer>
    </article>
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

function repositoryLocation(repository: DraftRepository): string {
  if (!repository.path) return "Repository path not entered";
  return repository.location === "ssh"
    ? `${repository.host || "host"}:${repository.path}`
    : repository.path;
}
