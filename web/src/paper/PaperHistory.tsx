import { PaperDialog } from "./PaperDialog";
import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  File,
  LoaderCircle,
  Plus,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import { api, ApiError, apiReadResponse } from "../core/api";
import type { PaperTree } from "../core/types";
import "./paper-coach.css";

interface VersionFile {
  file_id: string;
  path: string;
  kind: "text" | "binary";
  text_sha256?: string;
  blob_sha256?: string;
}
interface Version {
  version_id: string;
  revision: number;
  name?: string | null;
  actor_name: string | null;
  created_at: string;
  kind: "auto" | "named";
  manifest?: VersionFile[];
}
async function readText(path: string, signal: AbortSignal) {
  const response = await apiReadResponse(path, signal);
  if (!response.ok)
    throw new ApiError(`Paper history read failed (${response.status})`, response.status);
  return response.text();
}

export function PaperHistory({
  apiBase,
  tree,
  onClose,
  onRestored,
}: {
  apiBase: string;
  tree: PaperTree;
  onClose: () => void;
  onRestored: () => void;
}) {
  const [versions, setVersions] = useState<Version[]>([]);
  const [version, setVersion] = useState<Version | null>(null);
  const [file, setFile] = useState<VersionFile | null>(null);
  const [comparison, setComparison] = useState<{ saved: string; current: string } | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; retryable: boolean } | null>(null);
  const fail = (cause: unknown) =>
    setError({
      message: String(cause),
      retryable: !(cause instanceof ApiError && cause.status >= 400 && cause.status < 500),
    });
  const [confirmation, setConfirmation] = useState<{
    label: string;
    action: () => Promise<unknown>;
    onDone: () => void;
  } | null>(null);
  const [refresh, setRefresh] = useState(0);
  const retryAction = useRef<() => void>(() => setRefresh((value) => value + 1));
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    const abort = new AbortController();
    setBusy(true);
    retryAction.current = () => setRefresh((value) => value + 1);
    api<Version[]>(`${apiBase}/paper/history`, { signal: abort.signal })
      .then((next) => {
        setVersions(next);
        setError(null);
      })
      .catch((cause) => {
        if (!abort.signal.aborted) fail(cause);
      })
      .finally(() => {
        if (!abort.signal.aborted) setBusy(false);
      });
    return () => abort.abort();
  }, [apiBase, refresh]);
  const selectVersion = (next: Version) => {
    controller.current?.abort();
    setBusy(false);
    setError(null);
    setVersion(next);
    setFile(null);
    setComparison(null);
  };
  const selectFile = async (next: VersionFile) => {
    if (!version) return;
    retryAction.current = () => {
      void selectFile(next);
    };
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setFile(next);
    setComparison(null);
    setBusy(true);
    setError(null);
    try {
      if (next.kind === "text") {
        const [saved, current] = await Promise.all([
          readText(
            `${apiBase}/paper/history/${encodeURIComponent(version.version_id)}/files/${encodeURIComponent(next.file_id)}`,
            abort.signal,
          ),
          tree.files.some((item) => item.file_id === next.file_id)
            ? readText(
                `${apiBase}/paper/files/${encodeURIComponent(next.file_id)}/raw`,
                abort.signal,
              )
            : Promise.resolve(""),
        ]);
        if (!abort.signal.aborted) setComparison({ saved, current });
      }
    } catch (cause) {
      if (!abort.signal.aborted) fail(cause);
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  };
  const run = async (action: () => Promise<unknown>, onDone: () => void) => {
    retryAction.current = () => void run(action, onDone);
    setBusy(true);
    setError(null);
    try {
      await action();
      onDone();
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(false);
    }
  };
  const reload = () => setRefresh((value) => value + 1);
  return (
    <PaperDialog
      className={`paper-inspector${error ? " paper-failure" : ""}`}
      label="Paper history"
      onClose={onClose}
    >
      <header className="paper-bar">
        {(version || file) && (
          <button
            className="icon-button"
            aria-label="Back"
            onClick={() => {
              controller.current?.abort();
              setBusy(false);
              setError(null);
              if (file) {
                setFile(null);
                setComparison(null);
              } else setVersion(null);
            }}
          >
            <ArrowLeft size={16} />
          </button>
        )}
        <strong>
          {file?.path ||
            version?.name ||
            (version ? new Date(version.created_at).toLocaleString() : "History")}
        </strong>
        {!version && (
          <button className="icon-button" aria-label="Name version" onClick={() => setName("")}>
            <Plus size={16} />
          </button>
        )}
        {file && !busy && !error && (
          <button
            className="icon-button"
            aria-label="Restore file"
            title="Restore file"
            onClick={() =>
              setConfirmation({
                label: `Restore ${file.path}?`,
                action: () =>
                  api(
                    `${apiBase}/paper/history/${encodeURIComponent(version!.version_id)}/restore`,
                    {
                      method: "POST",
                      body: JSON.stringify({ file_ids: [file.file_id] }),
                    },
                  ),
                onDone: () => {
                  onRestored();
                  onClose();
                },
              })
            }
          >
            <RotateCcw size={16} />
          </button>
        )}
        {error && (
          <button
            className="icon-button"
            aria-label={error.retryable ? "Retry history" : "Dismiss history error"}
            title={error.message}
            onClick={() => (error.retryable ? retryAction.current() : setError(null))}
          >
            {error.retryable ? <RotateCcw size={16} /> : <X size={16} />}
          </button>
        )}
        <button className="icon-button" aria-label="Close history" onClick={onClose}>
          <X size={16} />
        </button>
      </header>
      {name !== null && (
        <form
          className="paper-history-name"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name.trim()) return;
            void run(
              () =>
                api(`${apiBase}/paper/history`, {
                  method: "POST",
                  body: JSON.stringify({ name: name.trim() }),
                }),
              () => {
                setName(null);
                reload();
              },
            );
          }}
        >
          <input
            autoFocus
            aria-label="Version name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              setName(null);
            }}
            maxLength={200}
          />
          <button className="icon-button" aria-label="Save version" disabled={!name.trim() || busy}>
            <Check size={16} />
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label="Cancel name"
            onClick={() => setName(null)}
          >
            <X size={16} />
          </button>
        </form>
      )}
      {busy && <LoaderCircle className="spin" size={18} aria-label="Loading history" />}
      <div className="paper-history-content">
        {!version &&
          versions.map((item) => (
            <div className="paper-history-version" key={item.version_id}>
              <button
                className="paper-history-row"
                disabled={busy}
                onClick={() => void selectVersion(item)}
              >
                <span>
                  {item.name && <>{item.name} · </>}
                  {new Date(item.created_at).toLocaleString()}
                  {item.actor_name && <> · {item.actor_name}</>}
                </span>
                <ChevronRight size={16} />
              </button>
              {item.kind === "named" && (
                <button
                  className="icon-button"
                  aria-label="Delete version"
                  title="Delete version"
                  disabled={busy}
                  onClick={() =>
                    setConfirmation({
                      label: `Delete ${item.name}?`,
                      action: () =>
                        api(`${apiBase}/paper/history/${encodeURIComponent(item.version_id)}`, {
                          method: "DELETE",
                        }),
                      onDone: reload,
                    })
                  }
                >
                  <Trash2 size={15} />
                </button>
              )}
            </div>
          ))}
        {version &&
          !file &&
          version.manifest?.map((item) => (
            <button
              className="paper-history-row"
              key={item.file_id}
              disabled={busy}
              onClick={() => void selectFile(item)}
            >
              <File size={15} />
              <span>{item.path}</span>
              <ChevronRight size={16} />
            </button>
          ))}
        {file && comparison && (
          <div className="paper-history-comparison">
            <section>
              <h3>Version</h3>
              <pre>{comparison.saved}</pre>
            </section>
            <section>
              <h3>Current</h3>
              <pre>{comparison.current}</pre>
            </section>
          </div>
        )}
        {file?.kind === "binary" && (
          <dl className="paper-history-binary">
            <dt>Version</dt>
            <dd>{file.blob_sha256}</dd>
            <dt>Current</dt>
            <dd>{tree.files.find((item) => item.file_id === file.file_id)?.sha256 ?? "Deleted"}</dd>
          </dl>
        )}
      </div>
      {confirmation && (
        <PaperDialog
          className="paper-box paper-confirm"
          role="alertdialog"
          label={confirmation.label}
          onClose={() => setConfirmation(null)}
        >
          <strong>{confirmation.label}</strong>
          <div className="paper-bar">
            <button className="button" onClick={() => setConfirmation(null)}>
              Cancel
            </button>
            <button
              className="button"
              onClick={() => {
                setConfirmation(null);
                void run(confirmation.action, confirmation.onDone);
              }}
            >
              Confirm
            </button>
          </div>
        </PaperDialog>
      )}
    </PaperDialog>
  );
}
