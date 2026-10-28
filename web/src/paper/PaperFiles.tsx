import { PaperDialog } from "./PaperDialog";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, File, Folder, FolderOpen, PanelLeft, RotateCcw, X } from "lucide-react";
import { api } from "../core/api";
import type { PaperFile } from "../core/types";
import { downloadPaper } from "./paperDownload";
import { PaperTreeMenu, type PaperTreeAction, type PaperTreeTarget } from "./PaperTreeMenu";

type TreeFolder = { path: string; name: string; children: (TreeFolder | PaperFile)[] };

/** Folders exist through their files' paths, plus any this browser just created. */
function buildTree(files: PaperFile[], extraFolders: string[]): TreeFolder {
  const root: TreeFolder = { path: "", name: "", children: [] };
  const folders = new Map([["", root]]);
  const folder = (path: string): TreeFolder => {
    const known = folders.get(path);
    if (known) return known;
    const parent = folder(path.split("/").slice(0, -1).join("/"));
    const created: TreeFolder = { path, name: path.split("/").at(-1)!, children: [] };
    parent.children.push(created);
    folders.set(path, created);
    return created;
  };
  for (const path of extraFolders) folder(path);
  for (const file of files) folder(file.path.split("/").slice(0, -1).join("/")).children.push(file);
  const name = (entry: TreeFolder | PaperFile) =>
    "file_id" in entry ? entry.path.split("/").at(-1)! : entry.name;
  for (const entry of folders.values())
    entry.children.sort((a, b) => name(a).localeCompare(name(b), undefined, { numeric: true }));
  return root;
}
const under = (path: string, folder: string) => path.startsWith(`${folder}/`);

export function PaperFiles({
  apiBase,
  projectId,
  files,
  selected,
  onSelect,
  onFold,
  onChanged,
}: {
  apiBase: string;
  projectId: string;
  files: PaperFile[];
  selected: string | null;
  /** `existsAt`: the tree revision that lists a file just created or replaced. */
  onSelect: (id: string, existsAt?: number) => void;
  onFold: () => void;
  onChanged: () => void;
}) {
  /** `from` is the file or folder path being renamed, or null for a new one. */
  const [edit, setEdit] = useState<{
    mode: "file" | "folder";
    from: PaperFile | string | null;
    path: string;
  } | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [newFolders, setNewFolders] = useState<string[]>([]);
  const [replacing, setReplacing] = useState<{
    file: globalThis.File;
    path: string;
    existing: PaperFile;
  } | null>(null);
  const pathInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const input = pathInput.current;
    input?.focus();
    input?.setSelectionRange(input.value.length, input.value.length);
  }, [edit?.mode, edit?.from]);
  const [deleting, setDeleting] = useState<PaperTreeTarget | null>(null);
  type Action = "path" | "upload" | "delete" | "download";
  type Failure = { message: string; retry: () => Promise<void> };
  const [failures, setFailures] = useState<Partial<Record<Action, Failure>>>({});
  const clearFailure = (action: Action) =>
    setFailures((current) => ({ ...current, [action]: undefined }));
  const pathFailure = failures.path;
  const deleteFailure = failures.delete;
  const uploadFailure = failures.upload;
  // Upload and download failures outside a dialog retry from the header.
  const looseKind: Action = failures.download ? "download" : "upload";
  const looseFailure = failures.download ?? (replacing ? undefined : uploadFailure);
  const [busy, setBusy] = useState(false);
  const upload = useRef<HTMLInputElement>(null);
  const uploadFolder = useRef("");
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
  const expand = (path: string) =>
    setCollapsed(
      (current) =>
        new Set([...current].filter((folder) => folder !== path && !under(path, folder))),
    );
  const closeEdit = () => {
    setEdit(null);
    clearFailure("path");
  };
  const savePath = () => {
    const target = edit;
    const path = target?.path.trim().replace(/\/+$/, "");
    if (!target || !path) return;
    if (target.mode === "folder") {
      const from = target.from as string | null;
      const parent = path.split("/").slice(0, -1).join("/");
      void perform("path", async () => {
        if (from && files.some((file) => under(file.path, from)))
          await api(`${apiBase}/paper/folders/rename`, {
            method: "POST",
            body: JSON.stringify({ path: from, to: path }),
          });
        setNewFolders((current) =>
          from
            ? current.map((folder) =>
                folder === from || under(folder, from) ? path + folder.slice(from.length) : folder,
              )
            : [...current, path],
        );
        if (parent) expand(parent);
        setEdit(null);
      });
      return;
    }
    const file = target.from as PaperFile | null;
    void perform("path", async () => {
      const receipt = await api<{ file_ids: string[]; revision: number }>(
        `${apiBase}/paper/files${file ? `/${encodeURIComponent(file.file_id)}/rename` : ""}`,
        {
          method: "POST",
          body: JSON.stringify({ path, ...(!file ? { kind: "text" } : {}) }),
        },
      );
      expand(path.split("/").slice(0, -1).join("/"));
      setEdit(null);
      if (!file) onSelect(receipt.file_ids[0], receipt.revision);
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
      expand(path.split("/").slice(0, -1).join("/"));
      onSelect(receipt.file_ids[0], receipt.revision);
    });
  const removeTarget = (target: PaperTreeTarget) =>
    perform("delete", async () => {
      if (target.kind === "file")
        await api(`${apiBase}/paper/files/${encodeURIComponent(target.file.file_id)}`, {
          method: "DELETE",
        });
      else if (target.kind === "folder") {
        if (!target.empty)
          await api(`${apiBase}/paper/folders/delete`, {
            method: "POST",
            body: JSON.stringify({ path: target.path }),
          });
        setNewFolders((current) =>
          current.filter((folder) => folder !== target.path && !under(folder, target.path)),
        );
      }
      setDeleting(null);
    });
  const act = (action: PaperTreeAction, target: PaperTreeTarget) => {
    const folder = target.kind === "folder" ? target.path : "";
    const prefix = folder ? `${folder}/` : "";
    if (action === "new-file" || action === "new-folder") {
      clearFailure("path");
      if (folder) expand(folder);
      setEdit({ mode: action === "new-file" ? "file" : "folder", from: null, path: prefix });
    } else if (action === "upload") {
      uploadFolder.current = folder;
      upload.current?.click();
    } else if (action === "rename") {
      clearFailure("path");
      setEdit(
        target.kind === "file"
          ? { mode: "file", from: target.file, path: target.file.path }
          : { mode: "folder", from: folder, path: folder },
      );
    } else if (action === "delete") {
      clearFailure("delete");
      setDeleting(target);
    } else if (target.kind !== "root") {
      const download =
        target.kind === "file"
          ? { fileId: target.file.file_id, name: target.file.path.split("/").at(-1)! }
          : { folder, name: `${folder.split("/").at(-1)}.zip` };
      void perform("download", () => downloadPaper(apiBase, projectId, download));
    }
  };
  const tree = buildTree(files, newFolders);
  const rows: ReactNode[] = [];
  const addRows = (folder: TreeFolder, depth: number) => {
    for (const entry of folder.children) {
      const indent = { paddingLeft: 8 + depth * 14 };
      if ("file_id" in entry) {
        const name = entry.path.split("/").at(-1);
        rows.push(
          <div
            key={entry.file_id}
            className={`paper-row ${selected === entry.file_id ? "selected" : ""}`}
          >
            <button
              className="paper-file"
              style={indent}
              title={entry.path}
              aria-pressed={selected === entry.file_id}
              onClick={() => onSelect(entry.file_id)}
            >
              <File size={15} />
              <span>{name}</span>
            </button>
            <PaperTreeMenu
              label={`Actions for ${entry.path}`}
              target={{ kind: "file", file: entry }}
              onAction={act}
            />
          </div>,
        );
        continue;
      }
      const open = !collapsed.has(entry.path);
      rows.push(
        <div key={`folder:${entry.path}`} className="paper-row">
          <button
            className="paper-folder"
            style={indent}
            title={entry.path}
            aria-expanded={open}
            onClick={() =>
              setCollapsed((current) => {
                const next = new Set(current);
                if (open) next.add(entry.path);
                else next.delete(entry.path);
                return next;
              })
            }
          >
            {open ? <FolderOpen size={15} /> : <Folder size={15} />}
            <span>{entry.name}</span>
          </button>
          <PaperTreeMenu
            label={`Actions for ${entry.path}/`}
            target={{
              kind: "folder",
              path: entry.path,
              empty: !files.some((file) => under(file.path, entry.path)),
            }}
            onAction={act}
          />
        </div>,
      );
      if (open) addRows(entry, depth + 1);
    }
  };
  addRows(tree, 0);
  return (
    <aside
      className={`paper-box paper-files ${Object.values(failures).some(Boolean) ? "paper-failed" : ""}`}
      data-testid="paper-files"
      aria-busy={busy}
    >
      <header className="paper-bar">
        <strong className="paper-grow">Files</strong>
        {looseFailure && (
          <button
            className="icon-button"
            aria-label="Retry file operation"
            title={looseFailure.message}
            onClick={() => void perform(looseKind, looseFailure.retry)}
          >
            <RotateCcw size={16} />
          </button>
        )}
        <button className="icon-button" aria-label="Fold files" onClick={onFold}>
          <PanelLeft size={16} />
        </button>
        <PaperTreeMenu label="File tree actions" target={{ kind: "root" }} onAction={act} />
        <input
          hidden
          type="file"
          ref={upload}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            const path = uploadFolder.current ? `${uploadFolder.current}/${file.name}` : file.name;
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
              placeholder={edit.mode === "file" ? "folder/name.md" : "folder"}
              onKeyDown={(event) => {
                if (event.key === "Escape") closeEdit();
              }}
              aria-label={edit.mode === "file" ? "File path" : "Folder path"}
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
              onClick={closeEdit}
            >
              <X size={14} />
            </button>
          </form>
        )}
        {rows}
      </div>
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
      {deleting && deleting.kind !== "root" && (
        <PaperDialog
          className={`paper-box paper-confirm ${deleteFailure ? "paper-failed" : ""}`}
          role="alertdialog"
          label="Delete file"
          onClose={() => {
            setDeleting(null);
            clearFailure("delete");
          }}
        >
          <strong>
            Delete {deleting.kind === "file" ? deleting.file.path : `${deleting.path}/`}?
          </strong>
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
              onClick={() => void removeTarget(deleting)}
            >
              {deleteFailure ? <RotateCcw size={16} /> : "Delete"}
            </button>
          </div>
        </PaperDialog>
      )}
    </aside>
  );
}
