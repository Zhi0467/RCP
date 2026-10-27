import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  File,
  LoaderCircle,
  Plus,
  RotateCcw,
  X,
} from "lucide-react";
import { api, apiReadResponse } from "../core/api";
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
  actor: string;
  created_at: string;
  kind: "auto" | "named";
  manifest?: VersionFile[];
}
async function readText(path: string, signal: AbortSignal) {
  const response = await apiReadResponse(path, signal);
  if (!response.ok) throw new Error(`Paper history read failed (${response.status})`);
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
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const retryAction = useRef<() => void>(() => setRefresh((value) => value + 1));
  const controller = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => {
      controller.current?.abort();
      previous?.focus();
    };
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    setBusy(true);
    api<Version[]>(`${apiBase}/paper/history`, { signal: abort.signal })
      .then((next) => {
        setVersions(next);
        setError(null);
      })
      .catch((cause) => {
        if (!abort.signal.aborted) setError(String(cause));
      })
      .finally(() => {
        if (!abort.signal.aborted) setBusy(false);
      });
    return () => abort.abort();
  }, [apiBase, refresh]);
  const selectVersion = async (next: Version) => {
    retryAction.current = () => {
      void selectVersion(next);
    };
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setBusy(true);
    setError(null);
    try {
      const detail = await api<Version>(
        `${apiBase}/paper/history/${encodeURIComponent(next.version_id)}`,
        { signal: abort.signal },
      );
      if (!abort.signal.aborted) {
        setVersion(detail);
        setFile(null);
        setComparison(null);
      }
    } catch (cause) {
      if (!abort.signal.aborted) setError(String(cause));
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
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
      if (!abort.signal.aborted) setError(String(cause));
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  };
  const restore = async () => {
    if (!version || !file) return;
    retryAction.current = () => {
      void restore();
    };
    setBusy(true);
    setError(null);
    try {
      await api(`${apiBase}/paper/history/${encodeURIComponent(version.version_id)}/restore`, {
        method: "POST",
        body: JSON.stringify({ file_ids: [file.file_id] }),
      });
      onRestored();
      onClose();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };
  const saveNamed = async () => {
    if (!name?.trim()) return;
    retryAction.current = () => {
      void saveNamed();
    };
    setBusy(true);
    setError(null);
    try {
      await api(`${apiBase}/paper/history`, {
        method: "POST",
        body: JSON.stringify({ name: name.trim() }),
      });
      setName(null);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="paper-inspector-backdrop" onClick={onClose}>
      <section
        className={`paper-inspector${error ? " paper-failure" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="Paper history"
        tabIndex={-1}
        ref={dialog}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
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
              onClick={() => void restore()}
            >
              <RotateCcw size={16} />
            </button>
          )}
          {error && (
            <button
              className="icon-button"
              aria-label="Retry history"
              title={error}
              onClick={() => retryAction.current()}
            >
              <RotateCcw size={16} />
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
              void saveNamed();
            }}
          >
            <input
              autoFocus
              aria-label="Version name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={200}
            />
            <button
              className="icon-button"
              aria-label="Save version"
              disabled={!name.trim() || busy}
            >
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
              <button
                className="paper-history-row"
                key={item.version_id}
                disabled={busy}
                onClick={() => void selectVersion(item)}
              >
                <span>{item.name || new Date(item.created_at).toLocaleString()}</span>
                <span>{item.actor}</span>
                <ChevronRight size={16} />
              </button>
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
              <dd>
                {tree.files.find((item) => item.file_id === file.file_id)?.sha256 ?? "Deleted"}
              </dd>
            </dl>
          )}
        </div>
      </section>
    </div>
  );
}
