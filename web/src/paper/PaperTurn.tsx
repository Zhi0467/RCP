import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, TriangleAlert, X } from "lucide-react";
import { api, ApiError } from "../core/api";
import {
  CommentComposer,
  CommentPopover,
  type CommentOffer,
} from "../comments/react/CommentComposer";
import { CommentThreadCard } from "../comments/react/CommentThreadCard";
import { useComments, type SendThreads } from "../comments/react/useComments";
import type { CommentThread } from "../comments/types.ts";
import {
  diffLineLabel,
  diffRows,
  textLines,
  turnTotals,
  type DiffRow,
  type PaperTurn,
  type PaperTurnFile,
} from "./turnDiff";
import "./paper-turn.css";

/** Rows shown before the rest of a long list folds under a count. */
const SHOWN_ROWS = 6;

function turnPath(apiBase: string, operationId: string) {
  return `${apiBase}/paper/turns/${encodeURIComponent(operationId)}`;
}

function filePath(apiBase: string, operationId: string, path: string) {
  return `${turnPath(apiBase, operationId)}/files/${path.split("/").map(encodeURIComponent).join("/")}`;
}

function Counts({ added, removed }: { added: number | null; removed: number | null }) {
  if (added === null || removed === null) return null;
  return (
    <span className="paper-turn-counts">
      <span className="paper-turn-added">+{added}</span>
      <span className="paper-turn-removed">−{removed}</span>
    </span>
  );
}

/** A finished Paper turn's changed files, folded under "N files changed"; clashes first. */
export function PaperTurnChanges({
  apiBase,
  operationId,
  openPath,
  onOpen,
}: {
  apiBase: string;
  operationId: string;
  openPath: string | null;
  onOpen: (path: string) => void;
}) {
  const [turn, setTurn] = useState<PaperTurn | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api<PaperTurn>(turnPath(apiBase, operationId), { signal: controller.signal })
      .then((value) => {
        setTurn(value);
        setError(null);
      })
      .catch((cause) => {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiError && cause.status === 404) setTurn(null);
        else setError(String(cause));
      });
    return () => controller.abort();
  }, [apiBase, operationId, attempt]);
  if (error)
    return (
      <button
        type="button"
        className="paper-turn paper-turn-head paper-failure"
        title={error}
        onClick={() => setAttempt((value) => value + 1)}
      >
        Retry changed files
      </button>
    );
  if (!turn?.files.length) return null;
  const totals = turnTotals(turn.files);
  const rows = all ? turn.files : turn.files.slice(0, SHOWN_ROWS);
  const count = turn.files.length;
  return (
    <section className="paper-turn" data-open={open || undefined}>
      <button
        type="button"
        className="paper-turn-head"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <strong>
          {count} {count === 1 ? "file" : "files"} changed
        </strong>
        <Counts {...totals} />
      </button>
      {open && (
        <ul className="paper-turn-files">
          {rows.map((row) => (
            <li key={row.path}>
              <button
                type="button"
                className={`paper-turn-file${row.clash ? " clash" : ""}`}
                aria-pressed={openPath === row.path}
                title={row.clash ? "Your text was kept" : undefined}
                onClick={() => onOpen(row.path)}
              >
                {row.clash && <TriangleAlert size={14} aria-label="Clash" />}
                <span className="paper-turn-path">{row.path}</span>
                <Counts added={row.added} removed={row.removed} />
              </button>
            </li>
          ))}
          {!all && count > SHOWN_ROWS && (
            <li>
              <button type="button" className="paper-turn-more" onClick={() => setAll(true)}>
                <ChevronRight size={14} />
                {count - SHOWN_ROWS} more
              </button>
            </li>
          )}
        </ul>
      )}
    </section>
  );
}

type LineRow = Exclude<DiffRow, { kind: "gap" }>;

/** One file of a Paper turn beside the chat: the agent's edits, each line taking a
 *  shared comment on the turn's reply, sent with the chat's own composer. */
export function PaperTurnDiff({
  projectId,
  apiBase,
  chatId,
  operationId,
  path,
  send,
  canPost,
  onClose,
}: {
  projectId: string;
  apiBase: string;
  chatId: string;
  operationId: string;
  path: string;
  send: SendThreads | null;
  canPost: boolean;
  onClose: () => void;
}) {
  const [file, setFile] = useState<PaperTurnFile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [side, setSide] = useState<"coach" | "current">("coach");
  const [offer, setOffer] = useState<CommentOffer | null>(null);
  const [shown, setShown] = useState<{ thread: string; point: { x: number; y: number } } | null>(
    null,
  );
  useEffect(() => {
    const controller = new AbortController();
    setFile(null);
    setSide("coach");
    api<PaperTurnFile>(filePath(apiBase, operationId, path), { signal: controller.signal })
      .then((value) => {
        setFile(value);
        setError(null);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(String(cause));
      });
    return () => controller.abort();
  }, [apiBase, operationId, path]);
  const comments = useComments({
    projectId,
    kind: "reply",
    ids: [operationId],
    chatId,
    canPost,
    send,
    picker: Boolean(offer || shown),
  });
  const threads = useMemo(() => {
    const byLabel = new Map<string, CommentThread[]>();
    for (const thread of comments.threads)
      if (
        thread.state === "open" &&
        thread.anchor.kind === "element" &&
        thread.anchor.path === path
      )
        byLabel.set(thread.anchor.label, [...(byLabel.get(thread.anchor.label) ?? []), thread]);
    return byLabel;
  }, [comments.threads, path]);
  const rows = useMemo(() => (file ? diffRows(file) : []), [file]);
  const comment = (row: LineRow, target: HTMLElement) => {
    if (!canPost) return;
    const box = target.getBoundingClientRect();
    setShown(null);
    setOffer({
      target: { kind: "reply", id: operationId },
      anchor: { kind: "element", path, label: diffLineLabel(row), text: row.text, comment: "" },
      drawnVersion: null,
      point: { x: box.left + 48, y: box.bottom },
    });
  };
  const shownThread = shown && comments.threads.find((item) => item.thread_id === shown.thread);
  const binary = file !== null && (file.before === null || file.coach === null);
  return (
    <aside className="paper-turn-diff" aria-label={`Changes to ${path}`}>
      <header className="paper-bar paper-turn-diff-bar">
        <strong className="paper-turn-path" title={path}>
          {path}
        </strong>
        {file?.clash && file.current !== null && (
          <div className="segmented" role="group" aria-label="Version">
            <button aria-pressed={side === "coach"} onClick={() => setSide("coach")}>
              Coach
            </button>
            <button aria-pressed={side === "current"} onClick={() => setSide("current")}>
              Current
            </button>
          </div>
        )}
        <button className="icon-button compact" aria-label="Close diff" onClick={onClose}>
          <X size={15} />
        </button>
      </header>
      <div className="paper-turn-code" role="list">
        {error && (
          <p className="paper-failure" role="alert">
            {error}
          </p>
        )}
        {!file && !error && <div className="paper-turn-loading" aria-label="Loading diff" />}
        {binary && <p className="paper-turn-binary">Binary file</p>}
        {file &&
          side === "current" &&
          textLines(file.current ?? "").map((text, index) => (
            <div key={index} className="paper-turn-line" role="listitem">
              <b>{index + 1}</b>
              <span>{text}</span>
            </div>
          ))}
        {file &&
          side === "coach" &&
          rows.map((row, index) => {
            if (row.kind === "gap")
              return (
                <div key={index} className="paper-turn-line gap" role="listitem">
                  <b />
                  <span>⋯</span>
                </div>
              );
            const pinned = threads.get(diffLineLabel(row)) ?? [];
            const selected =
              offer?.anchor.kind === "element" && offer.anchor.label === diffLineLabel(row);
            return (
              <div
                key={index}
                role="listitem"
                className={`paper-turn-line ${row.kind}${selected ? " selected" : ""}`}
              >
                <b>{row.line}</b>
                <button
                  type="button"
                  className="paper-turn-text"
                  aria-label={`Comment on ${diffLineLabel(row)}`}
                  disabled={!canPost}
                  onClick={(event) => comment(row, event.currentTarget)}
                >
                  {row.kind === "added" ? "+ " : row.kind === "removed" ? "− " : "  "}
                  {row.text}
                </button>
                {pinned.length > 0 && (
                  <button
                    type="button"
                    className="rcp-comment-pin paper-turn-pin"
                    aria-label={`${pinned.length} comment${pinned.length === 1 ? "" : "s"}`}
                    onClick={(event) => {
                      const box = event.currentTarget.getBoundingClientRect();
                      setOffer(null);
                      setShown({
                        thread: pinned[0].thread_id,
                        point: { x: box.left, y: box.bottom },
                      });
                    }}
                  >
                    {pinned.length}
                  </button>
                )}
              </div>
            );
          })}
      </div>
      {offer && (
        <CommentComposer
          offer={offer}
          comments={comments}
          agentLabel="This chat"
          canSend={Boolean(send)}
          onClose={() => setOffer(null)}
        />
      )}
      {shownThread && shown && (
        <CommentPopover point={shown.point} label="Comment" onClose={() => setShown(null)}>
          <CommentThreadCard
            thread={shownThread}
            comments={comments}
            agentLabel="This chat"
            wholeLabel={path}
            selected={false}
            onLocate={() => undefined}
          />
        </CommentPopover>
      )}
    </aside>
  );
}
