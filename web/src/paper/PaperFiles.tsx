import { useRef, useState } from "react";
import {
  Check,
  Download,
  File,
  FilePlus,
  FolderOpen,
  PanelLeftClose,
  Pencil,
  RotateCcw,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api } from "../core/api";
import type { PaperFile, PaperTree } from "../core/types";

export function PaperFiles({
  apiBase,
  tree,
  selected,
  onSelect,
  onFold,
  onChanged,
}: {
  apiBase: string;
  tree: PaperTree;
  selected: string | null;
  onSelect: (id: string) => void;
  onFold: () => void;
  onChanged: () => void;
}) {
  const [edit, setEdit] = useState<{ file: PaperFile | null; path: string } | null>(null);
  const [deleting, setDeleting] = useState<PaperFile | null>(null);
  const [failure, setFailure] = useState<{ message: string; retry: () => Promise<void> } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const upload = useRef<HTMLInputElement>(null);
  const selectedFile = tree.files.find((file) => file.file_id === selected);
  const perform = async (action: () => Promise<void>) => {
    setBusy(true);
    setFailure(null);
    try {
      await action();
      onChanged();
    } catch (cause) {
      setFailure({ message: String(cause), retry: action });
    } finally {
      setBusy(false);
    }
  };
  const savePath = () => {
    if (!edit?.path.trim()) return;
    const target = edit;
    void perform(async () => {
      await api(
        `${apiBase}/paper/files${target.file ? `/${encodeURIComponent(target.file.file_id)}/rename` : ""}`,
        {
          method: "POST",
          body: JSON.stringify({
            path: target.path.trim(),
            ...(!target.file ? { kind: "text" } : {}),
          }),
        },
      );
      setEdit(null);
    });
  };
  const folders = new Set<string>();
  return (
    <aside
      className={`paper-box paper-files ${failure ? "paper-failed" : ""}`}
      data-testid="paper-files"
      aria-busy={busy}
    >
      <header className="paper-bar">
        <strong className="paper-grow">Files</strong>
        <button className="icon-button" aria-label="Fold files" onClick={onFold}>
          <PanelLeftClose size={16} />
        </button>
        <button
          className="icon-button"
          aria-label="New file"
          onClick={() => setEdit({ file: null, path: "" })}
        >
          <FilePlus size={16} />
        </button>
        <button
          className="icon-button"
          aria-label="Upload file"
          onClick={() => upload.current?.click()}
        >
          <Upload size={16} />
        </button>
        <input
          hidden
          type="file"
          ref={upload}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            void perform(async () => {
              const body = new FormData();
              body.append("path", file.name);
              body.append("file", file);
              await api(`${apiBase}/paper/uploads`, { method: "POST", body });
            });
            event.target.value = "";
          }}
        />
      </header>
      <div className="paper-tree paper-well">
        {edit && (
          <form
            className="paper-path-form"
            onSubmit={(event) => {
              event.preventDefault();
              savePath();
            }}
          >
            <input
              autoFocus
              aria-label="File path"
              value={edit.path}
              onChange={(event) => setEdit({ ...edit, path: event.target.value })}
            />
            <button
              className="icon-button compact"
              aria-label="Save file path"
              disabled={busy || !edit.path.trim()}
            >
              <Check size={14} />
            </button>
            <button
              type="button"
              className="icon-button compact"
              aria-label="Cancel file path"
              onClick={() => setEdit(null)}
            >
              <X size={14} />
            </button>
          </form>
        )}
        {[...tree.files]
          .sort((a, b) => a.path.localeCompare(b.path))
          .map((file) => {
            const parts = file.path.split("/");
            const rows = [];
            for (let i = 1; i < parts.length; i++) {
              const folder = parts.slice(0, i).join("/");
              if (!folders.has(folder)) {
                folders.add(folder);
                rows.push(
                  <div
                    className="paper-folder"
                    style={{ paddingLeft: 8 + (i - 1) * 14 }}
                    key={`folder:${folder}`}
                  >
                    <FolderOpen size={15} />
                    <span>{parts[i - 1]}</span>
                  </div>,
                );
              }
            }
            rows.push(
              <button
                key={file.file_id}
                className={`paper-file ${selected === file.file_id ? "selected" : ""}`}
                style={{ paddingLeft: 8 + (parts.length - 1) * 14 }}
                title={file.path}
                aria-pressed={selected === file.file_id}
                onClick={() => onSelect(file.file_id)}
              >
                <File size={15} />
                <span>{parts.at(-1)}</span>
              </button>,
            );
            return rows;
          })}
      </div>
      <footer className="paper-bar">
        {failure ? (
          <button
            className="icon-button"
            aria-label="Retry file operation"
            title={failure.message}
            onClick={() => void perform(failure.retry)}
          >
            <RotateCcw size={16} />
          </button>
        ) : (
          selectedFile && (
            <>
              <button
                className="icon-button"
                aria-label="Rename file"
                onClick={() => setEdit({ file: selectedFile, path: selectedFile.path })}
              >
                <Pencil size={15} />
              </button>
              <a
                className="icon-button"
                aria-label="Download file"
                download={selectedFile.path.split("/").at(-1)}
                href={`${apiBase}/paper/files/${encodeURIComponent(selectedFile.file_id)}/raw`}
              >
                <Download size={15} />
              </a>
              <span className="paper-grow" />
              <button
                className="icon-button"
                aria-label="Delete file"
                onClick={() => setDeleting(selectedFile)}
              >
                <Trash2 size={15} />
              </button>
            </>
          )
        )}
      </footer>
      {deleting && (
        <div className="paper-dialog-backdrop">
          <section className="paper-box paper-confirm" role="alertdialog" aria-label="Delete file">
            <strong>Delete {deleting.path}?</strong>
            <div className="paper-bar">
              <button className="button" onClick={() => setDeleting(null)}>
                Cancel
              </button>
              <button
                className="button"
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    await api(`${apiBase}/paper/files/${encodeURIComponent(deleting.file_id)}`, {
                      method: "DELETE",
                    });
                    setDeleting(null);
                  })
                }
              >
                Delete
              </button>
            </div>
          </section>
        </div>
      )}
    </aside>
  );
}
