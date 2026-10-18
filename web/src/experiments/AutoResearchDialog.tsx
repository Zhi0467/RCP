import { BrowserToggle } from "../core/BrowserControls";
import {
  AgentConfigChip,
  AgentConfigControls,
  launchProviderReady,
  profileRunConfig,
} from "../core/AgentConfigControls";
import { BranchTag } from "../core/BranchTag";
import type { AgentRunConfig, Episode, GraphRef, ProjectSnapshot } from "../core/types";
import { BranchNameField, branchNameRequest, branchNameValid } from "../graph/BranchNameField";
import { ExperimentCheckout } from "./ExperimentCheckout";
import { AUTO_RESEARCH_ROUTE_PREFIX, experimentBoardHref } from "./experimentBoardModel";
import { GitBranch, LoaderCircle, Telescope, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

const MODAL_FOCUSABLE_SELECTOR =
  'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

interface ModalKeyEvent {
  key: string;
  shiftKey: boolean;
  preventDefault(): void;
}

export function handleAutoResearchDialogKeyDown(
  event: ModalKeyEvent,
  dialog: HTMLElement,
  activeElement: Element | null,
  busy: boolean,
  onClose: () => void,
): boolean {
  if (event.key === "Escape") {
    if (busy) return false;
    event.preventDefault();
    onClose();
    return true;
  }
  if (event.key !== "Tab") return false;
  const focusable = [...dialog.querySelectorAll<HTMLElement>(MODAL_FOCUSABLE_SELECTOR)].filter(
    (element) =>
      !element.hasAttribute("disabled") &&
      element.getAttribute("aria-hidden") !== "true" &&
      element.tabIndex >= 0,
  );
  if (focusable.length === 0) {
    event.preventDefault();
    dialog.focus();
    return true;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const activeInside = activeElement !== null && dialog.contains(activeElement);
  const destination = event.shiftKey
    ? !activeInside || activeElement === first
      ? last
      : null
    : !activeInside || activeElement === last
      ? first
      : null;
  if (!destination) return false;
  event.preventDefault();
  destination.focus();
  return true;
}

export function makeAutoResearchDialogBackgroundInert(dialog: HTMLElement): () => void {
  const previous = new Map<HTMLElement, boolean>();
  let branch = dialog;
  while (branch.parentElement) {
    const parent = branch.parentElement;
    for (const sibling of parent.children) {
      if (sibling === branch || !("inert" in sibling)) continue;
      const element = sibling as HTMLElement;
      previous.set(element, element.inert);
      element.inert = true;
    }
    branch = parent;
  }
  return () => {
    for (const [element, wasInert] of previous) element.inert = wasInert;
  };
}

export function restoreAutoResearchDialogFocus(element: HTMLElement | null): void {
  if (element?.isConnected) element.focus();
}

interface Props {
  open: boolean;
  busy: boolean;
  error: string | null;
  initialInvocationCeiling: number;
  /** Offers the orchestrator agent picker, defaulting from the Orchestrator profile. */
  project?: ProjectSnapshot;
  onClose: () => void;
  /** `codeWorktree` false opts out; true leaves the choice to the server's eligibility check.
   * A null `branchName` lets the server assign the date fallback. */
  onAuthorize: (
    invocationCeiling: number,
    startingInstruction: string | null,
    codeWorktree: boolean,
    browserRequested: boolean,
    launchConfig: AgentRunConfig | undefined,
    branchName: string | null,
    bulletinEnabled: boolean,
  ) => void;
  /** The server's branch-name refusal code from the last start, if any. */
  branchNameRefusal?: string | null;
  branchNamesInUse?: readonly string[];
  /** Auto-research live on other branches; a new start runs beside them. */
  otherLiveRuns?: readonly Episode[];
  graphRefs?: readonly GraphRef[] | null;
  /** On an Auto-research branch the dialog continues it instead of starting another. */
  continueBranch?: { name: string; episode: Episode } | null;
  /** Adds turns to an ended orchestrator without a message. */
  onContinue?: (episodeId: string, invocationCeiling: number) => Promise<void>;
  /** Mails the orchestrator; a ceiling also continues an ended one. */
  onSendMessage?: (
    episodeId: string,
    body: string,
    invocationCeiling?: number,
    requestId?: string,
  ) => Promise<void>;
}

export function AutoResearchDialog({
  open,
  busy,
  error,
  initialInvocationCeiling,
  project,
  onClose,
  onAuthorize,
  branchNameRefusal = null,
  branchNamesInUse = [],
  otherLiveRuns = [],
  graphRefs = null,
  continueBranch = null,
  onContinue,
  onSendMessage,
}: Props) {
  const titleId = useId();
  const liveId = useId();
  const dialog = useRef<HTMLFormElement>(null);
  const budgetInput = useRef<HTMLInputElement>(null);
  const [budget, setBudget] = useState(String(initialInvocationCeiling));
  const [instruction, setInstruction] = useState("");
  const [browserRequested, setBrowserRequested] = useState(false);
  const [bulletinEnabled, setBulletinEnabled] = useState(true);
  const [codeWorktree, setCodeWorktree] = useState(true);
  const [branch, setBranch] = useState("");
  const [addTurns, setAddTurns] = useState("3");
  const [continueError, setContinueError] = useState<string | null>(null);
  // A retried send of the same message reuses its request id.
  const messageRequest = useRef<{ key: string; id: string } | null>(null);
  const launchProfile = project?.agent_profiles.orchestrator;
  // Only an explicit pick is state; untouched, the picker follows the current profile.
  const [pickedConfig, setLaunchConfig] = useState<AgentRunConfig | null>(null);
  const [launchOpen, setLaunchOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    setLaunchConfig(null);
    setLaunchOpen(false);
  }, [open, project?.id]);
  const launchConfig =
    pickedConfig ?? (launchProfile ? profileRunConfig(launchProfile) : undefined);
  const launchEffectiveModel =
    launchConfig && launchProfile && launchConfig.provider === launchProfile.provider
      ? launchProfile.effective_model
      : "";

  useEffect(() => {
    if (!open) return;
    setBudget(String(initialInvocationCeiling));
    setInstruction("");
    setCodeWorktree(true);
    setBrowserRequested(false);
    setBulletinEnabled(true);
    setBranch("");
    setAddTurns("3");
    setContinueError(null);
    const returnFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const restoreBackground = dialog.current
      ? makeAutoResearchDialogBackgroundInert(dialog.current)
      : () => undefined;
    const frame = window.requestAnimationFrame(() => budgetInput.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      restoreBackground();
      restoreAutoResearchDialogFocus(returnFocus);
    };
  }, [initialInvocationCeiling, open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!dialog.current) return;
      handleAutoResearchDialogKeyDown(event, dialog.current, document.activeElement, busy, onClose);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose, open]);

  if (!open) return null;
  const invocationCeiling = Number(budget);
  const budgetIsValid =
    budget.trim().length > 0 && Number.isSafeInteger(invocationCeiling) && invocationCeiling >= 1;
  const continued = continueBranch?.episode ?? null;
  // An ended orchestrator takes a message only with new turns.
  const orchestratorEnded = Boolean(continued?.message_requires_continuation);
  const turns = Number(addTurns);
  const turnsValid = addTurns.trim().length > 0 && Number.isSafeInteger(turns) && turns >= 1;
  const message = instruction.trim();
  const canSubmit = continued
    ? orchestratorEnded
      ? turnsValid && (message ? continued.can_message : continued.can_continue)
      : Boolean(message) && continued.can_message
    : budgetIsValid && branchNameValid(branch);
  const submitContinuation = async (episode: Episode) => {
    setContinueError(null);
    try {
      if (!message) {
        await onContinue?.(episode.episode_id, turns);
      } else {
        const ceiling = orchestratorEnded ? turns : undefined;
        const key = `${episode.episode_id}:${message}:${ceiling ?? ""}`;
        if (messageRequest.current?.key !== key)
          messageRequest.current = { key, id: crypto.randomUUID() };
        await onSendMessage?.(episode.episode_id, message, ceiling, messageRequest.current.id);
        messageRequest.current = null;
      }
      onClose();
    } catch (cause) {
      setContinueError(cause instanceof Error ? cause.message : String(cause));
    }
  };
  const shownError = continued ? continueError : error;

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <form
        ref={dialog}
        className="campaign-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onSubmit={(event) => {
          event.preventDefault();
          if (!canSubmit || busy) return;
          if (continued) {
            void submitContinuation(continued);
            return;
          }
          onAuthorize(
            invocationCeiling,
            message || null,
            codeWorktree,
            browserRequested,
            launchConfig,
            branchNameRequest(branch),
            bulletinEnabled,
          );
        }}
      >
        <header>
          <span className="campaign-dialog-mark" aria-hidden="true">
            <Telescope size={20} />
          </span>
          <h2 id={titleId} className={continueBranch ? "auto-research-continue-title" : undefined}>
            {continueBranch ? (
              <>
                Continue <GitBranch size={16} aria-hidden="true" />
                <span className="auto-research-continue-name">{continueBranch.name}</span>
              </>
            ) : (
              "Authorize auto-research"
            )}
          </h2>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </header>
        <div className="campaign-dialog-fields">
          {continued ? (
            <>
              {orchestratorEnded && (
                <label className="campaign-budget-field">
                  <span>Add turns</span>
                  <input
                    ref={budgetInput}
                    type="number"
                    min={1}
                    step={1}
                    inputMode="numeric"
                    value={addTurns}
                    disabled={busy}
                    onChange={(event) => setAddTurns(event.target.value)}
                  />
                </label>
              )}
              <label>
                <span>Message to the orchestrator</span>
                <textarea
                  rows={5}
                  value={instruction}
                  disabled={busy}
                  onChange={(event) => setInstruction(event.target.value)}
                />
              </label>
            </>
          ) : (
            <>
              {project && otherLiveRuns.length > 0 && (
                <div className="auto-research-live-elsewhere" role="group" aria-labelledby={liveId}>
                  <span id={liveId}>Live on other branches</span>
                  <ul>
                    {otherLiveRuns.map((run) => (
                      <li key={run.episode_id} data-overlap-episode-id={run.episode_id}>
                        <a
                          href={experimentBoardHref(
                            project.id,
                            `${AUTO_RESEARCH_ROUTE_PREFIX}${run.episode_id}`,
                          )}
                          onClick={onClose}
                        >
                          <BranchTag refs={graphRefs} target={run.graph_target} />
                        </a>
                        <span>{run.authorized_by?.display_name || "a member"}</span>
                        <ExperimentCheckout
                          checkout={run.checkout}
                          detailed={run.checkout.available}
                        />
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <label className="campaign-budget-field">
                <span>Operational invocation ceiling</span>
                <input
                  ref={budgetInput}
                  type="number"
                  min={1}
                  step={1}
                  inputMode="numeric"
                  value={budget}
                  disabled={busy}
                  onChange={(event) => setBudget(event.target.value)}
                />
              </label>
              <label>
                <span>Starting instruction (optional)</span>
                <textarea
                  rows={5}
                  value={instruction}
                  disabled={busy}
                  onChange={(event) => setInstruction(event.target.value)}
                />
              </label>
              <div className="campaign-isolation" role="group" aria-label="Isolation">
                <BranchNameField
                  mode="auto_research"
                  value={branch}
                  disabled={busy}
                  refusal={branchNameRefusal}
                  namesInUse={branchNamesInUse}
                  onChange={setBranch}
                />
              </div>
            </>
          )}
          {!continued && launchOpen && project && launchProfile && launchConfig && (
            <AgentConfigControls
              project={project}
              value={launchConfig}
              onChange={setLaunchConfig}
              effectiveModel={launchEffectiveModel}
              workLikeCapable={launchProfile.work_like_capable}
              locked={busy}
              showRunOn={false}
              compact
            >
              <p className="agent-config-note">
                Experiments it spawns default to the Settings Node chat profile.
              </p>
              <div className="agent-config-options" role="group" aria-label="Launch options">
                <label>
                  <input
                    type="checkbox"
                    role="switch"
                    checked={bulletinEnabled}
                    disabled={busy}
                    onChange={(event) => setBulletinEnabled(event.target.checked)}
                  />
                  Bulletin
                </label>
                <BrowserToggle
                  checked={browserRequested}
                  disabled={busy}
                  onChange={setBrowserRequested}
                />
                <label>
                  <input
                    type="checkbox"
                    checked={codeWorktree}
                    disabled={busy}
                    onChange={(event) => setCodeWorktree(event.target.checked)}
                  />
                  Code worktree
                </label>
              </div>
            </AgentConfigControls>
          )}
        </div>
        {shownError && (
          <div className="campaign-dialog-error" role="alert">
            {shownError}
          </div>
        )}
        <footer>
          {/* A continuation keeps its episode's agent; only a new start picks one. */}
          {!continued && project && launchConfig && (
            <AgentConfigChip
              project={project}
              value={launchConfig}
              effectiveModel={launchEffectiveModel}
              open={launchOpen}
              disabled={busy}
              label="Orchestrator agent"
              workLike={launchProfile?.work_like_capable}
              optionCount={Number(browserRequested) + Number(codeWorktree)}
              onToggle={() => setLaunchOpen((current) => !current)}
            />
          )}
          <button className="button secondary" type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="button primary"
            type="submit"
            disabled={
              busy ||
              !canSubmit ||
              Boolean(
                !continued &&
                project &&
                launchConfig &&
                !launchProviderReady(project, launchConfig, launchProfile?.work_like_capable),
              )
            }
          >
            {busy ? <LoaderCircle className="spin" size={14} /> : <Telescope size={14} />}
            {continued
              ? orchestratorEnded
                ? "Continue branch"
                : "Send to orchestrator"
              : busy
                ? "Starting…"
                : "Start auto-research"}
          </button>
        </footer>
      </form>
    </div>
  );
}
