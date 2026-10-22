import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  ArchiveRestore,
  Check,
  ChevronDown,
  GitBranch,
  GitMerge,
  LoaderCircle,
  Pencil,
  Plus,
  RotateCcw,
  X,
} from "lucide-react";
import { ApiError, mergeGraphFork, renameGraphBranch, setGraphBranchArchived } from "../core/api";
import {
  branchName,
  branchNameProblem,
  submittedBranchName,
  typedBranchName,
} from "../core/branchNames";
import { sameGraphTarget } from "../core/graphTarget";
import { rovingFocusTarget } from "../core/rovingFocus";
import type { GraphHeadRef, GraphRef, GraphTargetRef } from "../core/types";
import {
  graphPickerOptions,
  graphRefCanMerge,
  graphRefDepth,
  graphRefParentLabel,
  graphRefTarget,
  refNeedsYou,
} from "./graphPickerModel";
import "./GraphPicker.css";

interface GraphPickerProps {
  /** Undefined while loading; null when the list could not be read. */
  refs: readonly GraphRef[] | null | undefined;
  activeRef: GraphTargetRef;
  projectId: string;
  onSelect: (target: GraphTargetRef) => void;
  /** Forks `head` into a new branch and switches to it; the name may be empty. */
  onCreate?: (name: string, requestId: string, head: GraphHeadRef) => Promise<void>;
  /** The head being viewed, which a new branch starts from. */
  viewedHead?: GraphHeadRef | null;
  createDisabled?: boolean;
  onOpen?: () => void;
  /** The last read failed: names stay, choices are disabled until a read succeeds. */
  unavailable?: boolean;
  onRefUpdated: (ref: GraphRef) => void;
  compact?: boolean;
  /** Names the trigger instead of the current branch, for a picker that moves something;
   * the trigger then reads as a plain menu row, without the branch icon. */
  label?: string;
  /** A line above the list saying what picking does. */
  heading?: string;
  /** Lists branches to choose or create, without merge, rename, or archive. */
  pickOnly?: boolean;
}

type Branch = Extract<GraphRef, { kind: "branch" }>;

// Matches the popover's CSS width, min(360px, 100vw - 16px).
const POPOVER_WIDTH = 360;
const VIEWPORT_GUTTER = 8;

/** Right-aligned under the trigger, shifted left only as far as the viewport allows. */
function popoverPlacement(trigger: HTMLElement | null) {
  const rect = trigger?.getBoundingClientRect();
  if (!rect) return null;
  const width = Math.min(POPOVER_WIDTH, window.innerWidth - 2 * VIEWPORT_GUTTER);
  const right = Math.max(VIEWPORT_GUTTER, window.innerWidth - rect.right);
  return {
    top: rect.bottom + 4,
    right: Math.min(right, window.innerWidth - VIEWPORT_GUTTER - width),
  };
}

/** The popover's choosable rows: each branch name and New branch. */
function rowButtons(popover: HTMLElement | null) {
  return [
    ...(popover?.querySelectorAll<HTMLButtonElement>("button.branch-picker-name:not(:disabled)") ??
      []),
  ];
}

export function GraphPicker({
  refs,
  activeRef,
  projectId,
  onSelect,
  onCreate,
  viewedHead = null,
  createDisabled = false,
  onOpen,
  onRefUpdated,
  unavailable = false,
  compact = false,
  label,
  heading,
  pickOnly = false,
}: GraphPickerProps) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [warning, setWarning] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [nameRefused, setNameRefused] = useState(false);
  const [createFailure, setCreateFailure] = useState<string | null>(null);
  // A retry repeats the whole first request, head included: the server
  // refuses one request id with a different parent head.
  const createRequest = useRef<{ id: string; head: GraphHeadRef } | null>(null);
  const [confirm, setConfirm] = useState<{
    action: "archive" | "merge";
    ref: Branch;
    requestId: string;
  } | null>(null);
  const [position, setPosition] = useState({ top: 0, right: 0 });
  const button = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const listId = useId();
  const options = graphPickerOptions(refs ?? [], activeRef);
  const currentName = branchName(refs, activeRef);
  const attention = (refs ?? []).some(
    (ref) => refNeedsYou(ref) && !sameGraphTarget(graphRefTarget(ref), activeRef),
  );

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (
        !confirm &&
        !list.current?.contains(event.target as Node) &&
        !button.current?.contains(event.target as Node)
      ) {
        setOpen(false);
        setEditing(null);
        setCreating(false);
        setWarning(null);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || confirm) return;
      event.preventDefault();
      if (editing) setEditing(null);
      else if (creating) setCreating(false);
      else {
        setOpen(false);
        button.current?.focus();
      }
      setWarning(null);
    };
    const positionList = () => {
      const placement = popoverPlacement(button.current);
      if (placement) setPosition(placement);
    };
    positionList();
    window.addEventListener("resize", positionList);
    document.addEventListener("scroll", positionList, true);
    document.addEventListener("pointerdown", closeOutside);
    // Capture runs before an enclosing overlay's listener, which skips a handled Escape.
    document.addEventListener("keydown", escape, true);
    return () => {
      window.removeEventListener("resize", positionList);
      document.removeEventListener("scroll", positionList, true);
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open, editing, creating, confirm]);

  // The popover is portaled to the end of the page, so focus moves into it on open.
  useEffect(() => {
    if (!open) return;
    const rows = rowButtons(list.current);
    (rows.find((row) => row.getAttribute("aria-current") === "true") ?? rows[0])?.focus();
  }, [open]);

  useEffect(() => {
    if (confirm) dialog.current?.showModal();
  }, [confirm]);

  // A failed list read withdraws an open Archive confirmation with the list.
  useEffect(() => {
    if (unavailable) setConfirm(null);
  }, [unavailable]);

  function showError(error: unknown) {
    setWarning(
      error instanceof ApiError && error.code === "branch_name_taken"
        ? "That name is already taken."
        : error instanceof ApiError && error.code === "branch_name_invalid"
          ? "Use 2–32 lowercase letters, digits, and hyphens."
          : "Could not update branch. Try again.",
    );
  }

  async function save(ref: Branch) {
    if (busy) return;
    const submitted = submittedBranchName(name);
    const problem = submitted === "" ? "Enter a branch name." : branchNameProblem(submitted);
    if (problem) {
      setWarning(problem);
      return;
    }
    setBusy(true);
    setWarning(null);
    try {
      onRefUpdated(await renameGraphBranch(projectId, ref.branch_id, submitted));
      setEditing(null);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    if (!onCreate || busy || createDisabled) return;
    const submitted = submittedBranchName(newName);
    if (branchNameProblem(submitted)) {
      setNameRefused(true);
      return;
    }
    if (!createRequest.current) {
      if (!viewedHead) return;
      createRequest.current = { id: crypto.randomUUID(), head: viewedHead };
    }
    const request = createRequest.current;
    setBusy(true);
    setNameRefused(false);
    setCreateFailure(null);
    try {
      await onCreate(submitted, request.id, request.head);
      createRequest.current = null;
      setCreating(false);
      setOpen(false);
    } catch (error) {
      // Only a transient failure retries under the same request id.
      if (!(error instanceof TypeError || (error instanceof ApiError && error.status >= 500)))
        createRequest.current = null;
      if (
        error instanceof ApiError &&
        (error.code === "branch_name_taken" || error.code === "branch_name_invalid")
      )
        setNameRefused(true);
      else setCreateFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function merge(ref: Branch, requestId: string) {
    if (busy) return;
    setBusy(true);
    setWarning(null);
    try {
      await mergeGraphFork(projectId, ref.branch_id, requestId);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  }

  async function archive(ref: Branch) {
    if (busy) return;
    setBusy(true);
    setWarning(null);
    try {
      onRefUpdated(await setGraphBranchArchived(projectId, ref.branch_id, !ref.archived));
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  }

  // A failed read keeps the button, disabled and naming the current ref.
  if (
    !onCreate &&
    activeRef.kind === "main" &&
    refs !== null &&
    !options.some((ref) => ref.kind === "branch")
  )
    return null;

  return (
    <div className={`branch-picker${compact ? " branch-picker-compact" : ""}`}>
      <button
        ref={button}
        className="branch-picker-trigger"
        aria-label={label ?? `Branch: ${currentName}`}
        aria-expanded={open}
        aria-controls={listId}
        aria-haspopup="dialog"
        disabled={!refs || unavailable}
        onClick={() => {
          const placement = popoverPlacement(button.current);
          if (placement) setPosition(placement);
          if (!open) onOpen?.();
          setOpen(!open);
          setEditing(null);
          setCreating(false);
          setWarning(null);
        }}
      >
        {!label && <GitBranch size={15} />}
        <span>{label ?? currentName}</span>
        {!label && attention && (
          <span className="branch-picker-attention" aria-label="Another branch needs you" />
        )}
        {!compact && <ChevronDown size={13} />}
      </button>
      {open &&
        !unavailable &&
        createPortal(
          <div
            ref={list}
            id={listId}
            className="branch-picker-popover"
            role="dialog"
            aria-label={label ?? "Branches"}
            style={position}
            onKeyDown={(event) => {
              // Handled keys stop here: an enclosing menu skips a handled key.
              // Tab cycles within the open popover; it moves focus itself, since
              // a chat workspace takes an unhandled Shift+Tab as its mode shortcut.
              if (event.key === "Tab") {
                const focusable = [
                  ...event.currentTarget.querySelectorAll<HTMLElement>(
                    "button:not(:disabled), input:not(:disabled)",
                  ),
                ];
                const index = focusable.indexOf(event.target as HTMLElement);
                const step = event.shiftKey ? -1 : 1;
                event.preventDefault();
                focusable[(index + step + focusable.length) % focusable.length]?.focus();
                return;
              }
              const next = rovingFocusTarget(
                rowButtons(event.currentTarget),
                event.target,
                event.key,
              );
              if (!next) return;
              event.preventDefault();
              next.focus();
            }}
          >
            {heading && <p className="branch-picker-heading">{heading}</p>}
            <ul className="branch-picker-list">
              {options.map((ref) => {
                const current = sameGraphTarget(graphRefTarget(ref), activeRef);
                return (
                  <li
                    className="branch-picker-row"
                    key={ref.branch_id ?? "main"}
                    data-branch-id={ref.branch_id ?? "main"}
                    style={{ paddingLeft: Math.min(graphRefDepth(ref, options), 4) * 12 }}
                  >
                    <span className="branch-picker-leading">
                      {current ? (
                        <Check size={15} aria-label="Current branch" />
                      ) : ref.running ? (
                        <LoaderCircle
                          size={15}
                          className="branch-picker-spinner"
                          aria-label="Running"
                        />
                      ) : null}
                    </span>
                    {ref.kind === "branch" && editing === ref.branch_id ? (
                      <>
                        <input
                          autoFocus
                          className="branch-picker-name-input"
                          aria-label="Branch name"
                          value={name}
                          disabled={busy}
                          onChange={(event) => {
                            setName(typedBranchName(event.target.value));
                            setWarning(null);
                          }}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              void save(ref);
                            }
                          }}
                        />
                        {refNeedsYou(ref) && (
                          <span className="branch-picker-attention" aria-label="Needs you" />
                        )}
                        <button
                          className="branch-picker-action"
                          aria-label="Save branch name"
                          disabled={busy}
                          onClick={() => void save(ref)}
                        >
                          <Check size={15} />
                        </button>
                        <button
                          className="branch-picker-action"
                          aria-label="Cancel rename"
                          disabled={busy}
                          onClick={() => {
                            setEditing(null);
                            setWarning(null);
                          }}
                        >
                          <X size={15} />
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          className="branch-picker-name"
                          aria-current={current ? "true" : undefined}
                          // Picking only moves something; it is already on its own branch.
                          disabled={pickOnly && current}
                          onClick={() => {
                            setOpen(false);
                            setEditing(null);
                            onSelect(graphRefTarget(ref));
                          }}
                        >
                          {ref.name}
                          {graphRefParentLabel(ref, refs ?? []) && (
                            <span className="branch-picker-parent">
                              {" "}
                              · {graphRefParentLabel(ref, refs ?? [])}
                            </span>
                          )}
                        </button>
                        {refNeedsYou(ref) && (
                          <span className="branch-picker-attention" aria-label="Needs you" />
                        )}
                        {ref.kind === "branch" && !pickOnly && (
                          <>
                            {graphRefCanMerge(ref) && (
                              <button
                                className="branch-picker-action"
                                aria-label={`Merge ${ref.name}`}
                                disabled={busy}
                                onClick={() => {
                                  setWarning(null);
                                  setConfirm({
                                    action: "merge",
                                    ref,
                                    requestId: crypto.randomUUID(),
                                  });
                                }}
                              >
                                <GitMerge size={15} />
                              </button>
                            )}
                            <button
                              className="branch-picker-action"
                              aria-label={`Rename ${ref.name}`}
                              disabled={busy}
                              onClick={() => {
                                setEditing(ref.branch_id);
                                setName(ref.name);
                                setWarning(null);
                              }}
                            >
                              <Pencil size={14} />
                            </button>
                            <button
                              className="branch-picker-action"
                              aria-label={`${ref.archived ? "Unarchive" : "Archive"} ${ref.name}`}
                              disabled={busy}
                              onClick={() => {
                                setWarning(null);
                                if (!ref.archived && ref.merge_state !== "merged")
                                  setConfirm({ action: "archive", ref, requestId: "" });
                                else void archive(ref);
                              }}
                            >
                              {ref.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
                            </button>
                          </>
                        )}
                      </>
                    )}
                  </li>
                );
              })}
            </ul>
            {onCreate && (
              <div className={`branch-picker-row${createFailure ? " branch-picker-failed" : ""}`}>
                <span className="branch-picker-leading">
                  <Plus size={15} />
                </span>
                {creating ? (
                  <>
                    <input
                      autoFocus
                      className="branch-picker-name-input"
                      aria-label="New branch name"
                      aria-invalid={nameRefused}
                      value={newName}
                      disabled={busy}
                      onChange={(event) => {
                        setNewName(typedBranchName(event.target.value));
                        setNameRefused(false);
                        setCreateFailure(null);
                        createRequest.current = null;
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          void create();
                        }
                      }}
                    />
                    <button
                      className="branch-picker-action"
                      aria-label={createFailure ? "Retry new branch" : "Create branch"}
                      title={createFailure ?? undefined}
                      disabled={busy || createDisabled}
                      onClick={() => void create()}
                    >
                      {createFailure ? <RotateCcw size={14} /> : <Check size={15} />}
                    </button>
                    <button
                      className="branch-picker-action"
                      aria-label="Cancel new branch"
                      disabled={busy}
                      onClick={() => setCreating(false)}
                    >
                      <X size={15} />
                    </button>
                  </>
                ) : (
                  <button
                    className="branch-picker-name"
                    disabled={busy || createDisabled}
                    onClick={() => {
                      setEditing(null);
                      setWarning(null);
                      setCreating(true);
                      setNewName("");
                      setNameRefused(false);
                      setCreateFailure(null);
                      createRequest.current = null;
                    }}
                  >
                    New branch
                  </button>
                )}
              </div>
            )}
            {warning && (
              <p className="branch-picker-warning" role="alert">
                {warning}
              </p>
            )}
          </div>,
          document.body,
        )}
      {confirm &&
        !unavailable &&
        createPortal(
          <dialog
            ref={dialog}
            className="branch-picker-confirm"
            aria-labelledby={`${listId}-confirm`}
            onCancel={(event) => {
              event.preventDefault();
              if (!busy) setConfirm(null);
            }}
          >
            <h2 id={`${listId}-confirm`}>
              {confirm.action === "merge"
                ? `Merge ${confirm.ref.name} into ${branchName(refs, confirm.ref.parent_head?.target ?? { kind: "main" })}?`
                : `Archive ${confirm.ref.name} without merging?`}
            </h2>
            <div>
              <button
                className="button secondary"
                autoFocus
                disabled={busy}
                onClick={() => setConfirm(null)}
              >
                Cancel
              </button>
              <button
                className="button"
                disabled={busy}
                onClick={() =>
                  void (confirm.action === "merge"
                    ? merge(confirm.ref, confirm.requestId)
                    : archive(confirm.ref))
                }
              >
                {confirm.action === "merge" ? "Merge" : "Archive"}
              </button>
            </div>
          </dialog>,
          document.body,
        )}
    </div>
  );
}
