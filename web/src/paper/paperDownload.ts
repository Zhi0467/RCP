import { downloadDesktopPaper, isDesktopRuntime } from "../core/desktopRuntime";

export type PaperDownload = { fileId: string; name: string } | { folder: string; name: string };

/** The desktop asks where to save through its native dialog; a browser downloads normally.
 * A folder arrives as one zip. */
export async function downloadPaper(
  apiBase: string,
  projectId: string,
  item: PaperDownload,
): Promise<void> {
  if (isDesktopRuntime()) {
    const { name, ...target } = item;
    await downloadDesktopPaper({ projectId, ...target, suggestedName: name });
    return;
  }
  const link = document.createElement("a");
  link.href =
    "fileId" in item
      ? `${apiBase}/paper/files/${encodeURIComponent(item.fileId)}/raw`
      : `${apiBase}/paper/folders/archive?path=${encodeURIComponent(item.folder)}`;
  link.download = item.name;
  link.click();
}
