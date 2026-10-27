import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  CheckCheck,
  Columns2,
  FileDiff,
  FilePenLine,
  FilePlus,
  FileX,
  Image,
  Locate,
  MoveRight,
  RefreshCw,
  RotateCcw,
  Undo2,
  X,
} from "lucide-react";
import { api } from "../core/api";

export interface PaperChange {
  change_id: string;
  group_id: string;
  task_id: string;
  kind: "edit" | "rewrite" | "create" | "rename" | "delete" | "binary";
  file_id: string;
  path: string;
  new_path?: string;
  lines?: { start: number; end: number };
  preview: { removed: string[]; added: string[]; more: number };
  stats?: { added: number; removed: number };
  state: "pending" | "accepted" | "rejected" | "outdated" | "applied" | "undone";
  decided_by?: string;
  decided_at?: string;
}
export type PaperChangeAction = "accept" | "reject" | "undo" | "refresh";
interface ChangeGroup {
  group_id: string;
  task_id: string;
  changes: PaperChange[];
}

export function usePaperChanges(apiBase: string, refreshKey: string | number) {
  const [changes, setChanges] = useState<PaperChange[]>([]);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const current = ++generation.current;
    try {
      const result = await api<{ groups: (ChangeGroup | PaperChange)[] }>(
        `${apiBase}/paper/changes`,
      );
      if (current !== generation.current) return;
      setChanges(result.groups.flatMap((group) => ("changes" in group ? group.changes : [group])));
      setError(null);
    } catch (cause) {
      if (current === generation.current) setError(String(cause));
    }
  }, [apiBase]);
  useEffect(() => {
    void refresh();
    return () => {
      generation.current += 1;
    };
  }, [refresh, refreshKey]);
  const act = useCallback(
    async (groupId: string, action: PaperChangeAction) => {
      await api(`${apiBase}/paper/changes/${encodeURIComponent(groupId)}/${action}`, {
        method: "POST",
        body: "{}",
      });
      await refresh();
    },
    [apiBase, refresh],
  );
  const undoAll = useCallback(
    async (taskId: string) => {
      await api(`${apiBase}/paper/changes/undo-all`, {
        method: "POST",
        body: JSON.stringify({ task_id: taskId }),
      });
      await refresh();
    },
    [apiBase, refresh],
  );
  return { changes, error, refresh, act, undoAll };
}

const icons = {
  edit: FilePenLine,
  rewrite: FileDiff,
  create: FilePlus,
  rename: MoveRight,
  delete: FileX,
  binary: Image,
};
function location(change: PaperChange) {
  const lines = change.lines;
  return [
    change.kind !== "edit" ? change.kind : null,
    lines
      ? lines.start === lines.end
        ? `line ${lines.start}`
        : `lines ${lines.start}–${lines.end}`
      : null,
    change.stats ? `+${change.stats.added} −${change.stats.removed}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function PaperChangeDiff({ change }: { change: PaperChange }) {
  return (
    <div className="paper-change-diff">
      {change.preview.removed.map((line, i) => (
        <div className="removed" key={`r${i}`}>
          − {line}
        </div>
      ))}
      {change.preview.added.map((line, i) => (
        <div className="added" key={`a${i}`}>
          + {line}
        </div>
      ))}
      {change.preview.more > 0 && (
        <div className="paper-change-more">+{change.preview.more} more lines</div>
      )}
    </div>
  );
}

export function PaperChanges({
  changes,
  onAction,
  onUndoAll,
  onLocate,
}: {
  changes: PaperChange[];
  onAction: (groupId: string, action: PaperChangeAction) => Promise<void>;
  onUndoAll: (taskId: string) => Promise<void>;
  onLocate: (change: PaperChange) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{
    id: string;
    error: string;
    retry: () => Promise<void>;
  } | null>(null);
  const [comparison, setComparison] = useState<PaperChange | null>(null);
  const perform = async (id: string, action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      await action();
    } catch (error) {
      setFailure({ id, error: String(error), retry: action });
    } finally {
      setBusy(false);
    }
  };
  const pending = changes.filter((change) => change.state === "pending");
  const applied = changes.filter((change) => change.state === "applied");
  if (!changes.length) return null;
  return (
    <div className="paper-changes" aria-busy={busy}>
      <div className={`paper-change-summary${failure?.id === "all" ? " paper-failure" : ""}`}>
        <span>{changes.length} changes</span>
        {pending.length > 0 && (
          <button
            className="icon-button compact"
            title="Accept all"
            aria-label="Accept all"
            disabled={busy}
            onClick={() =>
              void perform("all", async () => {
                for (const id of new Set(pending.map((change) => change.group_id)))
                  await onAction(id, "accept");
              })
            }
          >
            <CheckCheck size={15} />
          </button>
        )}
        {applied.length > 0 && (
          <button
            className="icon-button compact"
            title="Undo all"
            aria-label="Undo all"
            disabled={busy}
            onClick={() =>
              void perform("all", async () => {
                for (const id of new Set(applied.map((change) => change.task_id)))
                  await onUndoAll(id);
              })
            }
          >
            <Undo2 size={15} />
          </button>
        )}
        {failure?.id === "all" && (
          <button
            className="icon-button compact"
            aria-label="Retry changes"
            title={failure.error}
            onClick={() => void perform("all", failure.retry)}
          >
            <RotateCcw size={15} />
          </button>
        )}
      </div>
      {changes.map((change) => {
        const Icon = icons[change.kind];
        const settled = ["accepted", "rejected", "applied", "undone"].includes(change.state);
        const failed = failure?.id === change.change_id;
        return (
          <article
            key={change.change_id}
            data-change-id={change.change_id}
            data-state={change.state}
            className={`paper-change ${settled ? "settled" : ""} ${change.state}${failed ? " paper-failure" : ""}`}
          >
            <div className="paper-change-heading">
              {settled ? (
                change.state === "rejected" || change.state === "undone" ? (
                  <X size={15} />
                ) : (
                  <Check size={15} />
                )
              ) : (
                <Icon size={15} />
              )}
              <span className="paper-change-name">
                <strong>
                  {change.path}
                  {change.new_path ? ` → ${change.new_path}` : ""}
                </strong>
                <span>
                  {location(change)}
                  {change.state !== "pending" ? ` · ${change.state}` : ""}
                </span>
              </span>
              {failed ? (
                <button
                  className="icon-button compact"
                  aria-label="Retry change"
                  title={failure.error}
                  onClick={() => void perform(change.change_id, failure.retry)}
                >
                  <RotateCcw size={14} />
                </button>
              ) : (
                <>
                  {!settled && change.kind !== "delete" && (
                    <button
                      className="icon-button compact"
                      aria-label={change.kind === "rewrite" ? "Compare" : "Locate change"}
                      title={change.kind === "rewrite" ? "Compare" : "Locate change"}
                      onClick={() =>
                        change.kind === "rewrite" ? setComparison(change) : onLocate(change)
                      }
                    >
                      {change.kind === "rewrite" ? <Columns2 size={14} /> : <Locate size={14} />}
                    </button>
                  )}
                  {change.state === "pending" && (
                    <>
                      <button
                        className="icon-button compact accept"
                        aria-label="Accept change"
                        title="Accept change"
                        disabled={busy}
                        onClick={() =>
                          void perform(change.change_id, () => onAction(change.group_id, "accept"))
                        }
                      >
                        <Check size={14} />
                      </button>
                      <button
                        className="icon-button compact reject"
                        aria-label="Reject change"
                        title="Reject change"
                        disabled={busy}
                        onClick={() =>
                          void perform(change.change_id, () => onAction(change.group_id, "reject"))
                        }
                      >
                        <X size={14} />
                      </button>
                    </>
                  )}
                  {change.state === "outdated" && (
                    <button
                      className="icon-button compact"
                      aria-label="Refresh change"
                      title="Refresh change"
                      disabled={busy}
                      onClick={() =>
                        void perform(change.change_id, () => onAction(change.group_id, "refresh"))
                      }
                    >
                      <RefreshCw size={14} />
                    </button>
                  )}
                  {(change.state === "applied" || change.state === "accepted") && (
                    <button
                      className="icon-button compact"
                      aria-label="Undo change"
                      title="Undo change"
                      disabled={busy}
                      onClick={() =>
                        void perform(change.change_id, () => onAction(change.group_id, "undo"))
                      }
                    >
                      <Undo2 size={14} />
                    </button>
                  )}
                </>
              )}
            </div>
            {!settled && change.kind !== "rewrite" && <PaperChangeDiff change={change} />}
          </article>
        );
      })}
      {comparison && (
        <div className="paper-inspector-backdrop" onClick={() => setComparison(null)}>
          <section
            className="paper-inspector"
            role="dialog"
            aria-modal="true"
            aria-label="Compare change"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="paper-bar">
              <strong>{comparison.path}</strong>
              <button
                className="icon-button"
                aria-label="Close comparison"
                onClick={() => setComparison(null)}
              >
                <X size={16} />
              </button>
            </header>
            <PaperChangeDiff change={comparison} />
          </section>
        </div>
      )}
    </div>
  );
}
