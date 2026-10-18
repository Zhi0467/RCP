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
  X,
} from "lucide-react";
import { ApiError, mergeGraphFork, renameGraphBranch, setGraphBranchArchived } from "../core/api";
import { branchName, branchNameProblem } from "../core/branchNames";
import { sameGraphTarget } from "../core/graphTarget";
import type { GraphRef, GraphTargetRef } from "../core/types";
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
  onOpen?: () => void;
  /** The last read failed: names stay, choices are disabled until a read succeeds. */
  unavailable?: boolean;
  onRefUpdated: (ref: GraphRef) => void;
  compact?: boolean;
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

export function GraphPicker({
  refs,
  activeRef,
  projectId,
  onSelect,
  onOpen,
  onRefUpdated,
  unavailable = false,
  compact = false,
}: GraphPickerProps) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [warning, setWarning] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
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
        setWarning(null);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || confirm) return;
      event.preventDefault();
      if (editing) setEditing(null);
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
    document.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("resize", positionList);
      document.removeEventListener("scroll", positionList, true);
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", escape);
    };
  }, [open, editing, confirm]);

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
    const problem = name === "" ? "Enter a branch name." : branchNameProblem(name);
    if (problem) {
      setWarning(problem);
      return;
    }
    setBusy(true);
    setWarning(null);
    try {
      onRefUpdated(await renameGraphBranch(projectId, ref.branch_id, name));
      setEditing(null);
    } catch (error) {
      showError(error);
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
  if (activeRef.kind === "main" && refs !== null && !options.some((ref) => ref.kind === "branch"))
    return null;

  return (
    <div className={`branch-picker${compact ? " branch-picker-compact" : ""}`}>
      <button
        ref={button}
        className="branch-picker-trigger"
        aria-label={`Branch: ${currentName}`}
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
          setWarning(null);
        }}
      >
        <GitBranch size={15} />
        <span>{currentName}</span>
        {attention && (
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
            aria-label="Branches"
            style={position}
          >
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
                            setName(event.target.value);
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
                        {ref.kind === "branch" && (
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
