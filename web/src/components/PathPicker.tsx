import { ChevronRight, Folder, LoaderCircle, LockKeyhole } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { listMachineDirectory } from "../api";
import { errorMessage } from "../errors";
import {
  EMPTY_PATH_PICKER,
  applyDirectoryPage,
  canPickFolder,
  directoryRequest,
  filterMoveIsCurrent,
  pathBreadcrumbs,
  type PathPickerMove,
  type PathPickerState,
} from "../pathPicker";
import type { MachineDirectoryListing, MachineDirectoryRequest } from "../types";

const FILTER_DELAY_MS = 250;

interface Props {
  machineId: string;
  /** The folder to open first; null opens the account's home folder. */
  initialPath?: string | null;
  pickLabel?: string;
  /** A rejection's message is shown in the picker, so the server's reason reaches the person. */
  onPick: (path: string) => Promise<void> | void;
  onClose: () => void;
  list?: (
    machineId: string,
    request: MachineDirectoryRequest,
    signal?: AbortSignal,
  ) => Promise<MachineDirectoryListing>;
}

/** One folder level of one machine at a time, with a name filter and paging. */
export function PathPicker({
  machineId,
  initialPath = null,
  pickLabel = "Use this folder",
  onPick,
  onClose,
  list = listMachineDirectory,
}: Props) {
  const [state, setState] = useState<PathPickerState>(EMPTY_PATH_PICKER);
  const [filterDraft, setFilterDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const generation = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const filterTimer = useRef<number | null>(null);
  // Refs, not state, so a click in the same frame as a navigation sees it.
  const navigating = useRef(false);
  const pickingRef = useRef(false);

  const cancelPendingFilter = () => {
    if (filterTimer.current !== null) window.clearTimeout(filterTimer.current);
    filterTimer.current = null;
  };

  const move = async (next: PathPickerMove) => {
    if (next.kind !== "filter") cancelPendingFilter();
    if (!filterMoveIsCurrent(stateRef.current, next)) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    const request = ++generation.current;
    const current = stateRef.current;
    navigating.current = true;
    setLoading(true);
    setError(null);
    try {
      const page = await list(machineId, directoryRequest(current, next), controller.signal);
      if (request !== generation.current) return;
      setState(applyDirectoryPage(stateRef.current, next, page));
      if (next.kind === "open") setFilterDraft("");
    } catch (failure) {
      if (request !== generation.current) return;
      setError(errorMessage(failure));
    } finally {
      if (request === generation.current) {
        navigating.current = false;
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    void move({ kind: "open", path: initialPath });
    return () => {
      generation.current += 1;
      abort.current?.abort();
      cancelPendingFilter();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open the initial path once per machine; move reads refs and is recreated every render
  }, [machineId]);

  useEffect(() => {
    const path = stateRef.current.path;
    if (path === null || filterDraft === stateRef.current.filter) return;
    cancelPendingFilter();
    filterTimer.current = window.setTimeout(() => {
      filterTimer.current = null;
      void move({ kind: "filter", filter: filterDraft, path });
    }, FILTER_DELAY_MS);
    return cancelPendingFilter;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce on the draft only; move reads refs and is recreated every render
  }, [filterDraft]);

  const pick = async () => {
    const current = stateRef.current;
    if (
      !canPickFolder(current, { navigating: navigating.current, picking: pickingRef.current }) ||
      current.path === null
    )
      return;
    pickingRef.current = true;
    setPicking(true);
    setError(null);
    try {
      await onPick(current.path);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      pickingRef.current = false;
      setPicking(false);
    }
  };

  return (
    <section className="path-picker" aria-label="Choose a folder" data-path-picker={machineId}>
      <header>
        <nav className="path-picker-crumbs" aria-label="Folder path">
          {state.path !== null &&
            pathBreadcrumbs(state.path).map((crumb, index, crumbs) => (
              <span key={crumb.path}>
                {index > 0 && <ChevronRight size={12} aria-hidden="true" />}
                <button
                  type="button"
                  aria-current={index === crumbs.length - 1 ? "location" : undefined}
                  disabled={loading || index === crumbs.length - 1}
                  onClick={() => void move({ kind: "open", path: crumb.path })}
                >
                  {crumb.label}
                </button>
              </span>
            ))}
          {loading && <LoaderCircle className="spin" size={14} aria-label="Loading folders" />}
        </nav>
        <div className="path-picker-actions">
          <button
            className="button primary compact"
            type="button"
            data-path-picker-action="pick"
            disabled={!canPickFolder(state, { navigating: loading, picking })}
            onClick={() => void pick()}
          >
            {picking ? <LoaderCircle className="spin" size={14} /> : null}
            {pickLabel}
          </button>
          <button className="button secondary compact" type="button" onClick={onClose}>
            Close
          </button>
        </div>
      </header>
      <input
        className="path-picker-filter"
        type="search"
        aria-label="Filter folders"
        placeholder="Filter folders"
        value={filterDraft}
        disabled={state.path === null}
        onChange={(event) => setFilterDraft(event.target.value)}
      />
      {error && (
        <p className="path-picker-error" role="alert">
          {error}
        </p>
      )}
      <div className="path-picker-list">
        {state.parent && (
          <button
            type="button"
            data-path-picker-entry="parent"
            disabled={loading}
            onClick={() => void move({ kind: "open", path: state.parent })}
          >
            <Folder size={14} aria-hidden="true" />
            <strong>..</strong>
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        )}
        {state.entries.map((entry) =>
          entry.protected ? (
            <div
              className="path-picker-locked"
              key={entry.path}
              data-path-picker-entry="protected"
              title="RCP's own data stays read-only"
            >
              <LockKeyhole size={14} aria-hidden="true" />
              <strong>{entry.name}</strong>
              <span>RCP data</span>
            </div>
          ) : (
            <button
              type="button"
              key={entry.path}
              data-path-picker-entry="folder"
              disabled={loading}
              onClick={() => void move({ kind: "open", path: entry.path })}
            >
              <Folder size={14} aria-hidden="true" />
              <strong>{entry.name}</strong>
              <ChevronRight size={14} aria-hidden="true" />
            </button>
          ),
        )}
        {state.path !== null && !state.entries.length && !loading && (
          <div className="path-picker-empty">No folders.</div>
        )}
      </div>
      {state.nextOffset !== null && (
        <button
          className="path-picker-more"
          type="button"
          data-path-picker-action="more"
          disabled={loading}
          onClick={() => void move({ kind: "more" })}
        >
          Load more ({state.entries.length} of {state.total})
        </button>
      )}
    </section>
  );
}
