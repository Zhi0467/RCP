import type { PaperFile } from "../core/types";
import { parseBibtex, type BibEntry } from "./bibtex.ts";
import { paperRawUrl } from "./typstPreview.ts";

export interface PaperReference extends BibEntry {
  file: PaperFile;
}

export async function readReferenceFiles(
  projectId: string,
  treeFiles: PaperFile[],
  revision: number,
  signal: AbortSignal,
  cache: Map<string, string>,
): Promise<PaperReference[]> {
  const files = treeFiles.filter((file) => /\.bib$/i.test(file.path));
  const keys = new Set(files.map((file) => `${projectId}:${file.sha256}`));
  for (const key of cache.keys()) if (!keys.has(key)) cache.delete(key);
  const groups = await Promise.all(
    files.map(async (file) => {
      const key = `${projectId}:${file.sha256}`;
      let text = cache.get(key);
      if (text === undefined) {
        const response = await fetch(paperRawUrl(projectId, file.file_id, revision), { signal });
        if (!response.ok) throw new Error(`Could not read ${file.path} (${response.status})`);
        text = await response.text();
        if (!signal.aborted) cache.set(key, text);
      }
      return parseBibtex(text).map((entry) => ({ ...entry, file }));
    }),
  );
  return groups.flat();
}
