import { AlertTriangle, Play, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { AgentExecutionProfile, AgentRunConfig, ProjectSnapshot } from "../types";
import { AgentConfigControls, profileRunConfig } from "./AgentConfigControls";
import { RepositoryScope } from "./RepositoryScope";

interface Props {
  open: boolean;
  kind: AgentExecutionProfile;
  project: ProjectSnapshot;
  initialScope: string[];
  initialConfig?: AgentRunConfig;
  mode?: "start" | "retry";
  /** A run pinned to its machine. Only a retry that nothing is anchored to may move. */
  runOnLocked?: boolean;
  busy: boolean;
  onClose: () => void;
  onRun: (config: AgentRunConfig, scope: string[], message: string | null) => void;
}

/** Whose binding this dialog is about to change, in that run's own words. */
function switchTitle(kind: AgentExecutionProfile): string {
  if (kind === "node_chat") return "Switch Experiment provider";
  if (kind === "orchestrator") return "Switch Auto-research provider";
  return "Switch provider";
}

export function RunDialog({
  open,
  kind,
  project,
  initialScope,
  initialConfig,
  mode = "start",
  runOnLocked = true,
  busy,
  onClose,
  onRun,
}: Props) {
  const [scope, setScope] = useState(initialScope);
  const [config, setConfig] = useState(() => profileRunConfig(project.agent_profiles[kind]));
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!open) return;
    setScope(initialScope);
    setConfig(initialConfig || profileRunConfig(project.agent_profiles[kind]));
  }, [open, initialConfig, initialScope, kind, project.id]);

  useEffect(() => {
    if (open) setMessage("");
  }, [open]);

  if (!open) return null;
  // A run that can retry as-is only reaches this dialog through a switch
  // control, so opening it means the human wants a different binding and an
  // unchanged selection is not a submission. Seed and Refresh have no other
  // retry path, so requiring a change there would block a plain retry.
  const switching = mode === "retry" && kind !== "seed" && kind !== "refresh";
  const switchSelectionUnchanged = Boolean(
    switching &&
    initialConfig &&
    !agentSelectionChanged(config, initialConfig) &&
    // Moving off a machine that is gone is the whole reason a standalone
    // retry may change one, so it counts as the change this dialog wants.
    (runOnLocked || config.run_on === initialConfig.run_on),
  );
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  // Which runtime this run will use. A request cannot override the profile's
  // runtime, and the backend swaps in the provider default only when the run
  // overrides the provider — so a provider override silently moves the runtime
  // too. The override is a draft this dialog owns, which is why the answer is
  // assembled here rather than exported.
  const profileRuntime =
    config.provider === project.agent_profiles[kind].provider
      ? project.agent_profiles[kind].runtime
      : readiness?.default_runtime;
  const providerReady =
    readiness === undefined || Boolean(readiness.installed && readiness.authenticated);
  // Switching the orchestrator rebinds one turn. Children it spawns resolve
  // their own binding from the project's node_chat profile, so a human moving
  // off an exhausted provider has to move that profile too, in Settings, and
  // has to do it before the orchestrator spawns again.
  const childProfile = project.agent_profiles.node_chat;
  const childBindingStays = Boolean(
    switching &&
    kind === "orchestrator" &&
    childProfile &&
    childProfile.provider !== config.provider,
  );
  const crossMachineRepositories = project.repositories.filter(
    (repository) => scope.includes(repository.alias) && repository.machine !== config.run_on,
  );
  const machinesByAlias = new Map(project.machines.map((machine) => [machine.alias, machine]));
  const hostlessRepositories = crossMachineRepositories.filter(
    (repository) => !machinesByAlias.get(repository.machine)?.host.trim(),
  );
  const sshRepositories = crossMachineRepositories.filter((repository) =>
    Boolean(machinesByAlias.get(repository.machine)?.host.trim()),
  );

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="run-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="run-dialog-title"
      >
        <header>
          <h2 id="run-dialog-title">
            {switching
              ? switchTitle(kind)
              : mode === "retry"
                ? kind === "seed"
                  ? "Retry seed"
                  : "Retry refresh"
                : kind === "seed"
                  ? "Seed the project graph"
                  : "Refresh project understanding"}
          </h2>
          <button className="icon-button" onClick={onClose} disabled={busy} aria-label="Close">
            <X size={17} />
          </button>
        </header>
        {mode === "start" && (
          <>
            <div className="run-dialog-section">
              <span className="field-label">Truth input subset</span>
              <RepositoryScope
                repositories={project.repositories}
                projectScope={project.project_truth_scope}
                stateRepository={project.state_repository}
                selected={scope}
                onChange={setScope}
              />
            </div>
            <div className="run-dialog-section">
              <label className="node-edit-field">
                <span>Additional message (optional)</span>
                <textarea
                  rows={4}
                  value={message}
                  disabled={busy}
                  onChange={(event) => setMessage(event.target.value)}
                />
              </label>
            </div>
          </>
        )}
        <AgentConfigControls
          project={project}
          value={config}
          effectiveModel={
            config.provider === project.agent_profiles[kind].provider
              ? project.agent_profiles[kind].effective_model
              : ""
          }
          onChange={setConfig}
          workLikeCapable={project.agent_profiles[kind]?.work_like_capable ?? true}
          runtime={profileRuntime ? { value: profileRuntime, locked: true } : undefined}
          runOnLocked={runOnLocked}
          collapsible
        />
        {childBindingStays && (
          <div className="run-staging-warning">
            <AlertTriangle size={15} />
            <span>
              <strong>
                Children this orchestrator spawns stay on {childProfile.provider}
                {childProfile.effective_model ? ` · ${childProfile.effective_model}` : ""}.
              </strong>
              {" They take the project's Node chat profile, which this switch does not change." +
                " Change it in Settings first if that provider is also unavailable."}
            </span>
          </div>
        )}
        {hostlessRepositories.length > 0 && (
          <div className="run-staging-warning">
            <AlertTriangle size={15} />
            <span>
              <strong>
                {hostlessRepositories.map((repository) => repository.alias).join(", ")} cannot be
                read from {config.run_on}.
              </strong>
              {" Their machines have no SSH host; remove them from this run."}
            </span>
          </div>
        )}
        {sshRepositories.length > 0 && (
          <div className="run-staging-warning">
            <AlertTriangle size={15} />
            <span>
              <strong>
                {sshRepositories.map((repository) => repository.alias).join(", ")} will be read over
                SSH at their declared paths.
              </strong>
              {" Repositories are never copied."}
            </span>
          </div>
        )}
        <footer>
          <button className="button secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="button primary"
            disabled={
              busy ||
              (mode === "start" && scope.length === 0) ||
              switchSelectionUnchanged ||
              !providerReady ||
              hostlessRepositories.length > 0
            }
            onClick={() => onRun(config, scope, message.trim() || null)}
          >
            <Play size={14} />{" "}
            {mode === "retry"
              ? busy
                ? switching
                  ? "Switching…"
                  : "Retrying…"
                : switching
                  ? "Switch provider"
                  : "Retry"
              : busy
                ? kind === "seed"
                  ? "Seeding…"
                  : "Refreshing…"
                : kind === "seed"
                  ? "Start seed"
                  : "Start refresh"}
          </button>
        </footer>
      </section>
    </div>
  );
}

export function agentSelectionChanged(current: AgentRunConfig, initial: AgentRunConfig): boolean {
  return (
    current.provider !== initial.provider ||
    current.model !== initial.model ||
    current.reasoning !== initial.reasoning
  );
}
