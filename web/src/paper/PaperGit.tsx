import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  GitBranch,
  RefreshCw,
  Unlink,
  X,
} from "lucide-react";
import { copyText } from "../core/MarkdownCodeBlock";
import { PaperChanges, type PaperChange, type PaperChangeAction } from "./PaperChanges";
import { PaperConfirm } from "./PaperConfirm";
import "./paper-coach.css";
import { PaperDialog } from "./PaperDialog";
import {
  deployKeyUrl,
  gitPill,
  paperGit,
  validRepository,
  type PaperGitChangedFile,
  type PaperGitFileConflict,
  type PaperGitFileDiff,
  type PaperGitStatus,
} from "./gitSync";

const refusals: Record<string, string> = {
  retry: "GitHub changed. Push again.",
  conflicts: "Resolve the conflicts first.",
  collision: "A Paper file sits on a path GitHub holds in a form Paper cannot.",
};

export function PaperGit({
  apiBase,
  refreshKey,
  changes,
  onAction,
  onChanged,
  onLocate,
}: {
  apiBase: string;
  refreshKey: string;
  changes: PaperChange[];
  onAction: (groupId: string, action: PaperChangeAction) => Promise<void>;
  onChanged: () => void;
  onLocate: (change: PaperChange) => void;
}) {
  const [status, setStatus] = useState<PaperGitStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [repository, setRepository] = useState("");
  const [branch, setBranch] = useState("main");
  const [message, setMessage] = useState("");
  const [copied, setCopied] = useState(false);
  const [unlinking, setUnlinking] = useState(false);
  const [files, setFiles] = useState<PaperGitChangedFile[] | null>(null);
  const [conflicts, setConflicts] = useState<PaperGitFileConflict[]>([]);
  const generation = useRef(0);
  const fetched = useRef("");
  const changed = useRef(onChanged);
  changed.current = onChanged;
  const read = useCallback(async () => {
    const current = ++generation.current;
    const next = await paperGit<PaperGitStatus>(apiBase, "");
    if (current === generation.current) setStatus(next);
    return next;
  }, [apiBase]);
  useEffect(() => {
    void read()
      .then(async (next) => {
        if (next.state !== "linked" || fetched.current === apiBase) return;
        fetched.current = apiBase;
        try {
          await paperGit(apiBase, "/fetch", "POST");
        } finally {
          await read();
          changed.current();
        }
      })
      .catch(() => undefined);
  }, [apiBase, read, refreshKey]);
  const linked = status?.state === "linked";
  const head = status?.head.commit;
  const held = status?.conflicts ?? 0;
  useEffect(() => {
    if (!open || !linked) return;
    let current = true;
    void Promise.all([
      paperGit<PaperGitChangedFile[]>(apiBase, "/changes"),
      held ? paperGit<PaperGitFileConflict[]>(apiBase, "/conflicts") : [],
    ])
      .then(([nextFiles, nextConflicts]) => {
        if (!current) return;
        setFiles(nextFiles);
        setConflicts(nextConflicts);
      })
      .catch((error) => {
        if (current) setFailure(error instanceof Error ? error.message : String(error));
      });
    return () => {
      current = false;
    };
  }, [apiBase, open, linked, head, held, refreshKey]);
  const run = async (action: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setFailure("");
    try {
      await action();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      await read().catch(() => undefined);
      onChanged();
      setBusy(false);
    }
  };
  const close = () => {
    setOpen(false);
    setFailure("");
  };
  const pill = gitPill(status);
  if (!status) return null;
  return (
    <>
      <button
        className="icon-button paper-git-pill"
        aria-label="Paper Git"
        data-failed={pill.failed}
        title={
          pill.failed
            ? pill.hover
            : pill.hover
              ? `Synced ${new Date(pill.hover).toLocaleString()}`
              : "Link to GitHub"
        }
        onClick={() => setOpen(true)}
      >
        <GitBranch size={15} />
        {pill.branch && <span className="paper-git-branch">{pill.branch}</span>}
        {pill.conflicts > 0 && <span className="paper-git-count">{pill.conflicts}</span>}
        {pill.unpushed && (
          <ArrowUp size={13} className="paper-git-unpushed" aria-label="Unpushed edits" />
        )}
      </button>
      {open && (
        <PaperDialog label="Paper Git" className="paper-box paper-git-dialog" onClose={close}>
          <header className="paper-bar">
            {linked ? (
              <>
                <GitBranch size={15} />
                <strong className="paper-path">{status.repository}</strong>
                <span className="paper-format">{status.branch}</span>
                {head && (
                  <span className="paper-git-head" title={head}>
                    {head.slice(0, 7)}
                    {status.head.author ? ` · ${status.head.author}` : ""}
                  </span>
                )}
              </>
            ) : (
              <strong>Link to GitHub</strong>
            )}
            <span className="paper-grow" />
            {linked && (
              <button
                className="icon-button"
                aria-label="Refresh from GitHub"
                title="Refresh from GitHub"
                disabled={busy}
                onClick={() => void run(() => paperGit(apiBase, "/conflicts/refresh", "POST"))}
              >
                <RefreshCw size={16} />
              </button>
            )}
            {status.state !== "none" && (
              <button
                className="icon-button"
                aria-label="Unlink"
                title="Unlink"
                disabled={busy}
                onClick={() => setUnlinking(true)}
              >
                <Unlink size={16} />
              </button>
            )}
            <button className="icon-button" aria-label="Close Paper Git" onClick={close}>
              <X size={16} />
            </button>
          </header>
          {status.state === "none" && (
            <form
              className="paper-git-body"
              onSubmit={(event) => {
                event.preventDefault();
                void run(() =>
                  paperGit(apiBase, "/key", "POST", { repository, branch: branch.trim() }),
                );
              }}
            >
              <input
                aria-label="Repository"
                placeholder="owner/name"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                value={repository}
                onChange={(event) => setRepository(event.target.value.trim())}
              />
              <input
                aria-label="Branch"
                placeholder="main"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                value={branch}
                onChange={(event) => setBranch(event.target.value)}
              />
              <div className="paper-git-actions">
                {failure && <Failure text={failure} />}
                <button
                  className="button primary"
                  disabled={busy || !validRepository(repository) || !branch.trim()}
                >
                  Create key
                </button>
              </div>
            </form>
          )}
          {status.state === "pending" && (
            <div className="paper-git-body">
              <strong className="paper-git-repository">
                {status.repository} · {status.branch}
              </strong>
              <a href={deployKeyUrl(status.repository)} target="_blank" rel="noreferrer">
                Deploy key, with write access
              </a>
              <code className="paper-well paper-git-key">{status.public_key}</code>
              <div className="paper-git-actions">
                {failure && <Failure text={failure} />}
                <button
                  className="button"
                  onClick={() =>
                    void copyText(status.public_key).then(
                      () => {
                        setCopied(true);
                        setTimeout(() => setCopied(false), 1500);
                      },
                      (error) => setFailure(String(error)),
                    )
                  }
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                  Copy key
                </button>
                <button
                  className="button primary"
                  disabled={busy}
                  onClick={() => void run(() => paperGit(apiBase, "/link", "POST"))}
                >
                  Link
                </button>
              </div>
            </div>
          )}
          {linked && (
            <div className="paper-git-body">
              {conflicts.length > 0 && (
                <ul className="paper-well paper-git-files">
                  {conflicts.map((conflict) => (
                    <li
                      key={conflict.path}
                      title={`Mine ${conflict.ours}, theirs ${conflict.theirs}`}
                    >
                      <span>{conflict.path}</span>
                      {(["theirs", "mine"] as const).map((take) => (
                        <button
                          key={take}
                          type="button"
                          className="button compact"
                          disabled={busy}
                          onClick={() =>
                            void run(() =>
                              paperGit(apiBase, "/conflicts/resolve", "POST", {
                                path: conflict.path,
                                take,
                              }),
                            )
                          }
                        >
                          {take === "theirs" ? "Take theirs" : "Keep mine"}
                        </button>
                      ))}
                    </li>
                  ))}
                </ul>
              )}
              <PaperChanges
                apiBase={apiBase}
                changes={changes}
                onAction={onAction}
                onUndoAll={async () => undefined}
                onLocate={(change) => {
                  close();
                  onLocate(change);
                }}
              />
              {files && files.length + status.skipped.length > 0 && (
                <ul className="paper-well paper-git-files">
                  {files.map((file) => (
                    <ChangedFile
                      key={file.path}
                      apiBase={apiBase}
                      file={file}
                      refreshKey={refreshKey}
                    />
                  ))}
                  {status.skipped.map((entry) => (
                    <li key={entry.path} data-status="skipped" title={entry.reason}>
                      <span>{entry.path}</span>
                      <em>skipped</em>
                    </li>
                  ))}
                </ul>
              )}
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void run(async () => {
                    await paperGit(apiBase, "/push", "POST", { message });
                    setMessage("");
                  });
                }}
              >
                <input
                  aria-label="Commit message"
                  placeholder="Commit message"
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                />
                <div className="paper-git-actions">
                  {failure && <Failure text={refusals[failure] ?? failure} />}
                  <button
                    className="button primary"
                    disabled={busy || held > 0 || !files?.length || !message.trim()}
                  >
                    Push
                  </button>
                </div>
              </form>
            </div>
          )}
          {unlinking && (
            <PaperConfirm
              label={`Unlink ${status.repository}?`}
              onClose={() => setUnlinking(false)}
            >
              <button
                className="button"
                onClick={() => {
                  setUnlinking(false);
                  setFiles(null);
                  setConflicts([]);
                  void run(() => paperGit(apiBase, "", "DELETE"));
                }}
              >
                Unlink
              </button>
              <button className="button" onClick={() => setUnlinking(false)}>
                Cancel
              </button>
            </PaperConfirm>
          )}
        </PaperDialog>
      )}
    </>
  );
}

function Failure({ text }: { text: string }) {
  return (
    <p className="paper-git-failure" role="alert">
      {text}
    </p>
  );
}

function ChangedFile({
  apiBase,
  file,
  refreshKey,
}: {
  apiBase: string;
  file: PaperGitChangedFile;
  refreshKey: string;
}) {
  const [open, setOpen] = useState(false);
  const [diff, setDiff] = useState<PaperGitFileDiff | null>(null);
  const [failure, setFailure] = useState("");
  useEffect(() => {
    if (!open) return;
    let current = true;
    setFailure("");
    paperGit<PaperGitFileDiff>(apiBase, `/changes/diff?path=${encodeURIComponent(file.path)}`)
      .then((next) => current && setDiff(next))
      .catch(
        (error) => current && setFailure(String(error instanceof Error ? error.message : error)),
      );
    return () => {
      current = false;
    };
  }, [apiBase, open, file.path, refreshKey]);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <li className="paper-git-changed" data-status={file.status}>
      <button
        type="button"
        className="paper-git-file"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Chevron size={13} aria-hidden="true" />
        <span>{file.path}</span>
        {file.added === null && <em>{file.status}</em>}
        {Boolean(file.added) && <em className="paper-git-added">+{file.added}</em>}
        {Boolean(file.removed) && <em className="paper-git-removed">−{file.removed}</em>}
      </button>
      {open && failure && <Failure text={failure} />}
      {open && diff && !diff.binary && (
        <div className="paper-change-diff">
          {diff.lines.map((line, index) => (
            <div key={index} className={diffLineKind(line)}>
              {line.startsWith("@@") ? "⋯" : line.slice(1) || " "}
            </div>
          ))}
          {diff.more > 0 && <div className="paper-change-more">+{diff.more} more lines</div>}
        </div>
      )}
    </li>
  );
}

const DIFF_LINE_KINDS: Record<string, string> = { "+": "added", "-": "removed" };

function diffLineKind(line: string) {
  if (line.startsWith("@@")) return "hunk";
  return DIFF_LINE_KINDS[line[0]];
}
