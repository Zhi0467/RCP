import { PaperDialog } from "./PaperDialog";
import { useEffect, useRef, useState } from "react";
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
import type { PaperFile } from "../core/types";

export function PaperFiles({
  apiBase,
  files,
  selected,
  onSelect,
  onFold,
  onChanged,
}: {
  apiBase: string;
  files: PaperFile[];
  selected: string | null;
  /** `existsAt`: the tree revision that lists a file just created or replaced. */
  onSelect: (id: string, existsAt?: number) => void;
  onFold: () => void;
  onChanged: () => void;
}) {
  const [edit, setEdit] = useState<{ file: PaperFile | null; path: string } | null>(null);
  const [folder, setFolder] = useState<string | null>(null);
  const [replacing, setReplacing] = useState<{
    file: globalThis.File;
    path: string;
    existing: PaperFile;
  } | null>(null);
  const pathInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    pathInput.current?.focus();
  }, [edit?.file]);
  useEffect(() => setFolder(null), [selected]);
  const [deleting, setDeleting] = useState<PaperFile | null>(null);
  type Action = "path" | "upload" | "delete";
  type Failure = { message: string; retry: () => Promise<void> };
  const [failures, setFailures] = useState<Partial<Record<Action, Failure>>>({});
  const clearFailure = (action: Action) =>
    setFailures((current) => ({ ...current, [action]: undefined }));
  const pathFailure = failures.path;
  const deleteFailure = failures.delete;
  const uploadFailure = failures.upload;
  const [busy, setBusy] = useState(false);
  const upload = useRef<HTMLInputElement>(null);
  const selectedFile = files.find((file) => file.file_id === selected);
  const currentFolder = folder ?? selectedFile?.path.split("/").slice(0, -1).join("/") ?? "";
  const perform = async (kind: Action, action: () => Promise<void>) => {
    setBusy(true);
    clearFailure(kind);
    try {
      await action();
      onChanged();
    } catch (cause) {
      setFailures((current) => ({ ...current, [kind]: { message: String(cause), retry: action } }));
    } finally {
      setBusy(false);
    }
  };
  const savePath = () => {
    if (!edit?.path.trim()) return;
    const target = edit;
    void perform("path", async () => {
      const receipt = await api<{ file_ids: string[]; revision: number }>(
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
      if (!target.file) onSelect(receipt.file_ids[0], receipt.revision);
    });
  };
  const uploadFile = (file: globalThis.File, path: string, existing?: PaperFile) =>
    perform("upload", async () => {
      const body = new FormData();
      if (!existing) body.append("path", path);
      body.append("file", file);
      const receipt = await api<{ file_ids: string[]; revision: number }>(
        existing
          ? `${apiBase}/paper/files/${encodeURIComponent(existing.file_id)}/content`
          : `${apiBase}/paper/uploads`,
        { method: existing ? "PUT" : "POST", body },
      );
      setReplacing(null);
      onSelect(receipt.file_ids[0], receipt.revision);
    });
  const folders = new Set<string>();
  return (
    <aside
      className={`paper-box paper-files ${Object.values(failures).some(Boolean) ? "paper-failed" : ""}`}
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
          onClick={() => {
            clearFailure("path");
            setEdit({ file: null, path: "" });
          }}
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
            const path = currentFolder ? `${currentFolder}/${file.name}` : file.name;
            const existing = files.find((entry) => entry.path === path);
            clearFailure("upload");
            if (existing) setReplacing({ file, path, existing });
            else void uploadFile(file, path);
            event.target.value = "";
          }}
        />
      </header>
      <div className="paper-tree paper-well">
        {edit && (
          <form
            className={`paper-path-form ${pathFailure ? "paper-failed" : ""}`}
            onSubmit={(event) => {
              event.preventDefault();
              savePath();
            }}
          >
            <input
              autoFocus
              ref={pathInput}
              placeholder="folder/name.md"
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  setEdit(null);
                  clearFailure("path");
                }
              }}
              aria-label="File path"
              value={edit.path}
              onChange={(event) => setEdit({ ...edit, path: event.target.value })}
            />
            <button
              className="icon-button compact"
              aria-label="Save file path"
              disabled={busy || !edit.path.trim()}
            >
              {pathFailure ? <RotateCcw size={14} /> : <Check size={14} />}
            </button>
            <button
              type="button"
              className="icon-button compact"
              aria-label="Cancel file path"
              disabled={busy}
              onClick={() => {
                setEdit(null);
                clearFailure("path");
              }}
            >
              <X size={14} />
            </button>
          </form>
        )}
        {[...files]
          .sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }))
          .map((file) => {
            const parts = file.path.split("/");
            const rows = [];
            for (let i = 1; i < parts.length; i++) {
              const folder = parts.slice(0, i).join("/");
              if (!folders.has(folder)) {
                folders.add(folder);
                rows.push(
                  <button
                    className={`paper-folder ${currentFolder === folder ? "selected" : ""}`}
                    aria-pressed={currentFolder === folder}
                    onClick={() => setFolder(folder)}
                    style={{ paddingLeft: 8 + (i - 1) * 14 }}
                    key={`folder:${folder}`}
                  >
                    <FolderOpen size={15} />
                    <span>{parts[i - 1]}</span>
                  </button>,
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
                onClick={() => {
                  setFolder(null);
                  onSelect(file.file_id);
                }}
              >
                <File size={15} />
                <span>{parts.at(-1)}</span>
              </button>,
            );
            return rows;
          })}
      </div>
      <footer className="paper-bar">
        {uploadFailure && !replacing && (
          <button
            className="icon-button"
            aria-label="Retry file operation"
            title={uploadFailure.message}
            onClick={() => void perform("upload", uploadFailure.retry)}
          >
            <RotateCcw size={16} />
          </button>
        )}
        {selectedFile && (
          <>
            <button
              className="icon-button"
              aria-label="Rename file"
              onClick={() => {
                clearFailure("path");
                setEdit({ file: selectedFile, path: selectedFile.path });
              }}
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
              onClick={() => {
                clearFailure("delete");
                setDeleting(selectedFile);
              }}
            >
              <Trash2 size={15} />
            </button>
          </>
        )}
      </footer>
      {replacing && (
        <PaperDialog
          className={`paper-box paper-confirm ${uploadFailure ? "paper-failed" : ""}`}
          role="alertdialog"
          label={`Replace ${replacing.path}?`}
          onClose={() => {
            setReplacing(null);
            clearFailure("upload");
          }}
        >
          <strong>Replace {replacing.path}?</strong>
          <div className="paper-bar">
            <button
              className="button"
              disabled={busy}
              onClick={() => {
                setReplacing(null);
                clearFailure("upload");
              }}
            >
              Cancel
            </button>
            <button
              className={uploadFailure ? "icon-button" : "button"}
              aria-label={uploadFailure ? "Retry upload" : undefined}
              title={uploadFailure?.message}
              disabled={busy}
              onClick={() => void uploadFile(replacing.file, replacing.path, replacing.existing)}
            >
              {uploadFailure ? <RotateCcw size={16} /> : "Replace"}
            </button>
          </div>
        </PaperDialog>
      )}
      {deleting && (
        <PaperDialog
          className={`paper-box paper-confirm ${deleteFailure ? "paper-failed" : ""}`}
          role="alertdialog"
          label="Delete file"
          onClose={() => {
            setDeleting(null);
            clearFailure("delete");
          }}
        >
          <strong>Delete {deleting.path}?</strong>
          <div className="paper-bar">
            <button
              className="button"
              disabled={busy}
              onClick={() => {
                setDeleting(null);
                clearFailure("delete");
              }}
            >
              Cancel
            </button>
            <button
              className={deleteFailure ? "icon-button" : "button"}
              aria-label={deleteFailure ? "Retry delete" : undefined}
              title={deleteFailure?.message}
              disabled={busy}
              onClick={() =>
                void perform("delete", async () => {
                  await api(`${apiBase}/paper/files/${encodeURIComponent(deleting.file_id)}`, {
                    method: "DELETE",
                  });
                  setDeleting(null);
                })
              }
            >
              {deleteFailure ? <RotateCcw size={16} /> : "Delete"}
            </button>
          </div>
        </PaperDialog>
      )}
    </aside>
  );
}
