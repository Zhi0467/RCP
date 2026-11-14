import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, Copy, GitBranch, Plus, RefreshCw, Unlink, X } from "lucide-react";
import { copyText } from "../core/MarkdownCodeBlock";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { popoverPlacement, type PopoverPlacement } from "../ui/popoverPlacement";
import "../graph/GraphPicker.css";
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

const MENU_WIDTH = 300;

export type PaperGitState = ReturnType<typeof usePaperGit>;

/** One Paper Git status, file list, and action runner shared by the pill and the Changes tab. */
export function usePaperGit(apiBase: string, refreshKey: string, onChanged: () => void) {
  const [status, setStatus] = useState<PaperGitStatus | null>(null);
  const [files, setFiles] = useState<PaperGitChangedFile[]>([]);
  const [conflicts, setConflicts] = useState<PaperGitFileConflict[]>([]);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
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
  const unpushed = linked && status.unpushed;
  const held = linked ? status.conflicts : 0;
  const head = status?.head.commit;
  useEffect(() => {
    let current = true;
    void Promise.all([
      unpushed ? paperGit<PaperGitChangedFile[]>(apiBase, "/changes") : [],
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
  }, [apiBase, unpushed, held, head, refreshKey]);
  const run = async (action: () => Promise<unknown>) => {
    if (busy) return false;
    setBusy(true);
    setFailure("");
    try {
      await action();
      return true;
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      await read().catch(() => undefined);
      changed.current();
      setBusy(false);
    }
  };
  return { apiBase, status, files, conflicts, busy, failure, setFailure, run };
}

export function PaperGitPill({ git }: { git: PaperGitState }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<PopoverPlacement>({ right: 0, maxHeight: 0 });
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const { setFailure } = git;
  const close = useCallback(() => {
    setOpen(false);
    setFailure("");
  }, [setFailure]);
  const { status } = git;
  const linked = status?.state === "linked";
  useLayerDismiss(open && linked, close, [menu, button], button);
  if (!status) return null;
  const pill = gitPill(status);
  return (
    <>
      <button
        ref={button}
        className="icon-button paper-git-pill"
        aria-label="Paper Git"
        aria-expanded={open}
        data-failed={pill.failed}
        title={pill.failed ? pill.hover : linked ? status.repository : "Link to GitHub"}
        onClick={() => {
          const placement = popoverPlacement(button.current, MENU_WIDTH);
          if (placement) setPosition(placement);
          if (open) close();
          else setOpen(true);
        }}
      >
        <GitBranch size={15} />
        {pill.branch && <span className="paper-git-branch">{pill.branch}</span>}
      </button>
      {open &&
        (linked ? (
          createPortal(
            <div
              ref={menu}
              className="branch-picker-popover paper-git-menu"
              role="dialog"
              aria-label="Branches"
              style={position}
            >
              <BranchMenu git={git} onDone={close} />
            </div>,
            document.body,
          )
        ) : (
          <LinkDialog git={git} onClose={close} />
        ))}
    </>
  );
}

function BranchMenu({ git, onDone }: { git: PaperGitState; onDone: () => void }) {
  const status = git.status!;
  const [branches, setBranches] = useState<string[] | null>(null);
  const [filter, setFilter] = useState("");
  const [unlinking, setUnlinking] = useState(false);
  const { apiBase, setFailure } = git;
  useEffect(() => {
    let current = true;
    paperGit<string[]>(apiBase, "/branches")
      .then((next) => current && setBranches(next))
      .catch(
        (error) => current && setFailure(String(error instanceof Error ? error.message : error)),
      );
    return () => {
      current = false;
    };
  }, [apiBase, setFailure]);
  const name = filter.trim();
  const shown = (branches ?? []).filter((branch) => branch.includes(name));
  const switchTo = (branch: string, create: boolean) =>
    void git
      .run(() => paperGit(apiBase, "/branch", "POST", { branch, create }))
      .then((done) => done && onDone());
  const creatable = name && !branches?.includes(name) && name !== status.branch;
  return (
    <>
      <input
        className="paper-git-filter"
        aria-label="Switch branch"
        placeholder="Switch branch"
        autoFocus
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          if (creatable) switchTo(name, true);
          else if (shown.length === 1) switchTo(shown[0], false);
        }}
      />
      <ul className="branch-picker-list">
        {shown.map((branch) => (
          <li key={branch} className="branch-picker-row">
            <span className="branch-picker-leading">
              {branch === status.branch && <Check size={14} />}
            </span>
            <button
              className="branch-picker-name"
              aria-current={branch === status.branch}
              disabled={git.busy || branch === status.branch}
              onClick={() => switchTo(branch, false)}
            >
              {branch}
            </button>
          </li>
        ))}
        {creatable && (
          <li className="branch-picker-row">
            <span className="branch-picker-leading">
              <Plus size={14} />
            </span>
            <button
              className="branch-picker-name paper-git-new"
              disabled={git.busy}
              onClick={() => switchTo(name, true)}
            >
              New branch {name}
            </button>
          </li>
        )}
        <li className="branch-picker-row">
          <span className="branch-picker-leading">
            <Unlink size={14} />
          </span>
          <button
            className="branch-picker-name paper-git-new"
            disabled={git.busy}
            onClick={() => setUnlinking(true)}
          >
            Unlink {status.repository}
          </button>
        </li>
      </ul>
      {git.failure && <Failure text={refusals[git.failure] ?? git.failure} />}
      {unlinking && (
        <PaperConfirm label={`Unlink ${status.repository}?`} onClose={() => setUnlinking(false)}>
          <button
            className="button"
            onClick={() => {
              setUnlinking(false);
              void git.run(() => paperGit(apiBase, "", "DELETE")).then(onDone);
            }}
          >
            Unlink
          </button>
          <button className="button" onClick={() => setUnlinking(false)}>
            Cancel
          </button>
        </PaperConfirm>
      )}
    </>
  );
}

function LinkDialog({ git, onClose }: { git: PaperGitState; onClose: () => void }) {
  const status = git.status!;
  const { apiBase, busy, failure } = git;
  const [repository, setRepository] = useState("");
  const [branch, setBranch] = useState("main");
  const [copied, setCopied] = useState(false);
  return (
    <PaperDialog label="Paper Git" className="paper-box paper-git-dialog" onClose={onClose}>
      <header className="paper-bar">
        <strong>Link to GitHub</strong>
        <span className="paper-grow" />
        {status.state === "pending" && (
          <button
            className="icon-button"
            aria-label="Unlink"
            title="Unlink"
            disabled={busy}
            onClick={() => void git.run(() => paperGit(apiBase, "", "DELETE"))}
          >
            <Unlink size={16} />
          </button>
        )}
        <button className="icon-button" aria-label="Close Paper Git" onClick={onClose}>
          <X size={16} />
        </button>
      </header>
      {status.state === "none" && (
        <form
          className="paper-git-body"
          onSubmit={(event) => {
            event.preventDefault();
            void git.run(() =>
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
              Continue
            </button>
          </div>
        </form>
      )}
      {status.state === "pending" && (
        <div className="paper-git-body">
          <strong className="paper-git-repository">
            {status.repository} · {status.branch}
          </strong>
          {!status.key_source && (
            <>
              <a href={deployKeyUrl(status.repository)} target="_blank" rel="noreferrer">
                Deploy key, with write access
              </a>
              <code className="paper-well paper-git-key">{status.public_key}</code>
            </>
          )}
          <div className="paper-git-actions">
            {failure && <Failure text={failure} />}
            {!status.key_source && (
              <button
                className="button"
                onClick={() =>
                  void copyText(status.public_key).then(
                    () => {
                      setCopied(true);
                      setTimeout(() => setCopied(false), 1500);
                    },
                    (error) => git.setFailure(String(error)),
                  )
                }
              >
                {copied ? <Check size={14} /> : <Copy size={14} />}
                Copy key
              </button>
            )}
            <button
              className="button primary"
              disabled={busy}
              onClick={() => void git.run(() => paperGit(apiBase, "/link", "POST"))}
            >
              Link
            </button>
          </div>
        </div>
      )}
    </PaperDialog>
  );
}

/** The Changes tab: held conflicts, Git change cards, changed files, and Commit & push. */
export function PaperGitChanges({
  git,
  changes,
  openPath,
  onAction,
  onLocate,
  onOpenDiff,
}: {
  git: PaperGitState;
  changes: PaperChange[];
  openPath: string | null;
  onAction: (groupId: string, action: PaperChangeAction) => Promise<void>;
  onLocate: (change: PaperChange) => void;
  onOpenDiff: (path: string) => void;
}) {
  const [message, setMessage] = useState("");
  const { apiBase, status, files, conflicts, busy, failure } = git;
  if (status?.state !== "linked") return null;
  return (
    <div className="paper-git-changes">
      <div className="paper-git-scroll">
        {conflicts.length > 0 && (
          <ul className="paper-well paper-git-files">
            {conflicts.map((conflict) => (
              <li key={conflict.path} title={`Mine ${conflict.ours}, theirs ${conflict.theirs}`}>
                <span>{conflict.path}</span>
                {(["theirs", "mine"] as const).map((take) => (
                  <button
                    key={take}
                    type="button"
                    className="button compact"
                    disabled={busy}
                    onClick={() =>
                      void git.run(() =>
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
        <button
          className="button compact paper-git-refresh"
          disabled={busy}
          onClick={() => void git.run(() => paperGit(apiBase, "/conflicts/refresh", "POST"))}
        >
          <RefreshCw size={14} />
          Refresh from GitHub
        </button>
        <PaperChanges
          apiBase={apiBase}
          changes={changes}
          onAction={onAction}
          onUndoAll={async () => undefined}
          onLocate={onLocate}
        />
        <ul className="paper-git-list">
          {files.map((file) => (
            <li key={file.path}>
              <button
                className="paper-git-file"
                aria-pressed={openPath === file.path}
                onClick={() => onOpenDiff(file.path)}
              >
                <span>{file.path}</span>
                {file.added === null && <em>{file.status}</em>}
                {Boolean(file.added) && <em className="paper-git-added">+{file.added}</em>}
                {Boolean(file.removed) && <em className="paper-git-removed">−{file.removed}</em>}
              </button>
            </li>
          ))}
          {status.skipped.map((entry) => (
            <li key={entry.path} data-status="skipped" title={entry.reason}>
              <span className="paper-git-file">
                <span>{entry.path}</span>
                <em>skipped</em>
              </span>
            </li>
          ))}
        </ul>
      </div>
      <form
        className="paper-git-commit"
        onSubmit={(event) => {
          event.preventDefault();
          void git
            .run(() => paperGit(apiBase, "/push", "POST", { message }))
            .then((done) => done && setMessage(""));
        }}
      >
        <textarea
          aria-label="Commit message"
          placeholder="Commit message"
          rows={2}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
        />
        {failure && <Failure text={refusals[failure] ?? failure} />}
        <button
          className="button primary"
          disabled={busy || status.conflicts > 0 || !files.length || !message.trim()}
        >
          Commit &amp; push
        </button>
      </form>
    </div>
  );
}

/** One changed file's diff against the synced commit, shown in the editor pane. */
export function PaperGitDiff({
  git,
  path,
  refreshKey,
}: {
  git: PaperGitState;
  path: string;
  refreshKey: string;
}) {
  const [loaded, setLoaded] = useState<{ path: string; diff: PaperGitFileDiff } | null>(null);
  const [failure, setFailure] = useState("");
  const { apiBase } = git;
  const diff = loaded?.path === path ? loaded.diff : null;
  useEffect(() => {
    let current = true;
    setFailure("");
    paperGit<PaperGitFileDiff>(apiBase, `/changes/diff?path=${encodeURIComponent(path)}`)
      .then((next) => current && setLoaded({ path, diff: next }))
      .catch(
        (error) => current && setFailure(String(error instanceof Error ? error.message : error)),
      );
    return () => {
      current = false;
    };
  }, [apiBase, path, refreshKey]);
  return (
    <div className="paper-git-diff-view">
      {failure && <Failure text={failure} />}
      {diff?.binary && <p className="paper-git-binary">{path}</p>}
      {diff && !diff.binary && (
        <div className="paper-change-diff">
          {diff.lines.map((line, index) => (
            <div key={index} className={diffLineKind(line)}>
              {line.startsWith("@@") ? "⋯" : line.slice(1) || " "}
            </div>
          ))}
          {diff.more > 0 && <div className="paper-change-more">+{diff.more} more lines</div>}
        </div>
      )}
    </div>
  );
}

function Failure({ text }: { text: string }) {
  return (
    <p className="paper-git-failure" role="alert">
      {text}
    </p>
  );
}

const DIFF_LINE_KINDS: Record<string, string> = { "+": "added", "-": "removed" };

function diffLineKind(line: string) {
  if (line.startsWith("@@")) return "hunk";
  return DIFF_LINE_KINDS[line[0]];
}
