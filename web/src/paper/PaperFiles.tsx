import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Check,
  Download,
  File,
  FilePlus,
  Folder,
  FolderOpen,
  FolderPlus,
  PanelLeft,
  Pencil,
  RotateCcw,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api } from "../core/api";
import type { PaperFile } from "../core/types";
import { OverflowMenu, type OverflowMenuItem } from "../ui/OverflowMenu";
import { PaperConfirm } from "./PaperConfirm";
import { downloadPaper, type PaperDownload } from "./paperDownload";

type TreeFolder = { path: string; name: string; children: (TreeFolder | PaperFile)[] };
/** A new or renamed file (`file` null for new), or a new or renamed folder (`from` null for new). */
type PathEdit =
  | { mode: "file"; file: PaperFile | null; path: string }
  | { mode: "folder"; from: string | null; path: string };

const parentOf = (path: string) => path.split("/").slice(0, -1).join("/");

/** Folders exist through their files' paths, plus any this browser just created. */
function buildTree(files: PaperFile[], extraFolders: string[]): TreeFolder {
  const root: TreeFolder = { path: "", name: "", children: [] };
  const folders = new Map([["", root]]);
  const folder = (path: string): TreeFolder => {
    const known = folders.get(path);
    if (known) return known;
    const parent = folder(parentOf(path));
    const created: TreeFolder = { path, name: path.split("/").at(-1)!, children: [] };
    parent.children.push(created);
    folders.set(path, created);
    return created;
  };
  for (const path of extraFolders) folder(path);
  for (const file of files) folder(parentOf(file.path)).children.push(file);
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
  const [edit, setEdit] = useState<PathEdit | null>(null);
  const editing = edit && (edit.mode === "file" ? edit.file : edit.from);
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
  }, [edit?.mode, editing]);
  const [deleting, setDeleting] = useState<{ name: string; remove: () => Promise<void> } | null>(
    null,
  );
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
      const from = target.from;
      const parent = parentOf(path);
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
    const file = target.file;
    void perform("path", async () => {
      const receipt = await api<{ file_ids: string[]; revision: number }>(
        `${apiBase}/paper/files${file ? `/${encodeURIComponent(file.file_id)}/rename` : ""}`,
        {
          method: "POST",
          body: JSON.stringify({ path, ...(!file ? { kind: "text" } : {}) }),
        },
      );
      expand(parentOf(path));
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
      expand(parentOf(path));
      onSelect(receipt.file_ids[0], receipt.revision);
    });
  const startEdit = (next: PathEdit, folder = "") => {
    clearFailure("path");
    if (folder) expand(folder);
    setEdit(next);
  };
  // The root's and a folder's menus start with these; `folder` is "" for the root.
  const createItems = (folder: string): OverflowMenuItem[] => {
    const prefix = folder ? `${folder}/` : "";
    return [
      {
        label: "New file",
        icon: FilePlus,
        onSelect: () => startEdit({ mode: "file", file: null, path: prefix }, folder),
      },
      {
        label: "New folder",
        icon: FolderPlus,
        onSelect: () => startEdit({ mode: "folder", from: null, path: prefix }, folder),
      },
      {
        label: "Upload",
        icon: Upload,
        onSelect: () => {
          uploadFolder.current = folder;
          upload.current?.click();
        },
      },
    ];
  };
  // An empty folder exists only in this browser, so it has nothing to download.
  const entryItems = (
    rename: PathEdit,
    download: PaperDownload | null,
    name: string,
    remove: () => Promise<void>,
  ): OverflowMenuItem[] => [
    { label: "Rename", icon: Pencil, onSelect: () => startEdit(rename) },
    {
      label: "Download",
      icon: Download,
      disabled: !download,
      onSelect: () => {
        if (download) void perform("download", () => downloadPaper(apiBase, projectId, download));
      },
    },
    {
      label: "Delete",
      icon: Trash2,
      onSelect: () => {
        clearFailure("delete");
        setDeleting({ name, remove });
      },
    },
  ];
  const removeFolder = (path: string, empty: boolean) => async () => {
    if (!empty)
      await api(`${apiBase}/paper/folders/delete`, {
        method: "POST",
        body: JSON.stringify({ path }),
      });
    setNewFolders((current) => current.filter((folder) => folder !== path && !under(folder, path)));
  };
  // Cancel, then the action, which turns into its retry after a failure.
  const confirmButtons = (
    action: string,
    failure: Failure | undefined,
    cancel: () => void,
    confirm: () => void,
    retryLabel: string,
  ) => (
    <>
      <button className="button" disabled={busy} onClick={cancel}>
        Cancel
      </button>
      <button
        className={failure ? "icon-button" : "button"}
        aria-label={failure ? retryLabel : undefined}
        title={failure?.message}
        disabled={busy}
        onClick={confirm}
      >
        {failure ? <RotateCcw size={16} /> : action}
      </button>
    </>
  );
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
            <OverflowMenu
              label={`Actions for ${entry.path}`}
              triggerClassName="paper-tree-menu-button"
              portal
              items={entryItems(
                { mode: "file", file: entry, path: entry.path },
                { fileId: entry.file_id, name: name! },
                entry.path,
                () =>
                  api(`${apiBase}/paper/files/${encodeURIComponent(entry.file_id)}`, {
                    method: "DELETE",
                  }),
              )}
            />
          </div>,
        );
        continue;
      }
      const open = !collapsed.has(entry.path);
      const empty = !files.some((file) => under(file.path, entry.path));
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
          <OverflowMenu
            label={`Actions for ${entry.path}/`}
            triggerClassName="paper-tree-menu-button"
            portal
            items={[
              ...createItems(entry.path),
              ...entryItems(
                { mode: "folder", from: entry.path, path: entry.path },
                empty ? null : { folder: entry.path, name: `${entry.name}.zip` },
                `${entry.path}/`,
                removeFolder(entry.path, empty),
              ),
            ]}
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
        <OverflowMenu label="File tree actions" items={createItems("")} portal />
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
        <PaperConfirm
          label={`Replace ${replacing.path}?`}
          failed={Boolean(uploadFailure)}
          onClose={() => {
            setReplacing(null);
            clearFailure("upload");
          }}
        >
          {confirmButtons(
            "Replace",
            uploadFailure,
            () => {
              setReplacing(null);
              clearFailure("upload");
            },
            () => void uploadFile(replacing.file, replacing.path, replacing.existing),
            "Retry upload",
          )}
        </PaperConfirm>
      )}
      {deleting && (
        <PaperConfirm
          label={`Delete ${deleting.name}?`}
          failed={Boolean(deleteFailure)}
          onClose={() => {
            setDeleting(null);
            clearFailure("delete");
          }}
        >
          {confirmButtons(
            "Delete",
            deleteFailure,
            () => {
              setDeleting(null);
              clearFailure("delete");
            },
            () =>
              void perform("delete", async () => {
                await deleting.remove();
                setDeleting(null);
              }),
            "Retry delete",
          )}
        </PaperConfirm>
      )}
    </aside>
  );
}
