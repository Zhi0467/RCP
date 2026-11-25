import { downloadDesktopPaper, isDesktopRuntime } from "../core/desktopRuntime";

export type StoredPaperItem = { fileId: string; name: string } | { folder: string; name: string };

export function storedFile(file: { file_id: string; path: string }): StoredPaperItem {
  return { fileId: file.file_id, name: file.path.split("/").at(-1) ?? file.path };
}

export function storedPaperHref(apiBase: string, item: StoredPaperItem): string {
  return "fileId" in item
    ? `${apiBase}/paper/files/${encodeURIComponent(item.fileId)}/raw`
    : `${apiBase}/paper/folders/archive?path=${encodeURIComponent(item.folder)}`;
}

/**
 * Saves a stored Paper file, or a folder as one zip, through the desktop dialog;
 * null in a browser. The desktop command fetches by file or folder identity, so
 * a compiled PDF and text made in the page have no desktop save.
 */
export function storedPaperSave(
  projectId: string,
  item: StoredPaperItem | null,
): (() => Promise<unknown>) | null {
  if (!item || !isDesktopRuntime()) return null;
  const { name, ...target } = item;
  return () => downloadDesktopPaper({ projectId, ...target, suggestedName: name });
}

function followDownloadLink(href: string, name: string): void {
  const link = document.createElement("a");
  link.href = href;
  link.download = name;
  link.click();
}

/** A Download that is a menu action instead of a link. */
export async function downloadPaper(
  apiBase: string,
  projectId: string,
  item: StoredPaperItem,
): Promise<void> {
  const save = storedPaperSave(projectId, item);
  if (save) await save();
  else followDownloadLink(storedPaperHref(apiBase, item), item.name);
}

/** Bytes made in the page, which only the page itself can hand over. */
export function downloadPaperBlob(blob: Blob, name: string): void {
  const href = URL.createObjectURL(blob);
  followDownloadLink(href, name);
  setTimeout(() => URL.revokeObjectURL(href), 0);
}
