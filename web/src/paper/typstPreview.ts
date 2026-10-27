import type { PaperFile } from "../core/types";

export interface TypstSource {
  path: string;
  bytes: Uint8Array;
}

/** Resolve paper-relative paths without letting a reference leave the tree. */
export function paperRelativePath(from: string, reference: string): string | null {
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(reference)) return null;
  const parts = reference.startsWith("/") ? [] : from.split("/").slice(0, -1);
  for (const part of reference.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(part);
  }
  return parts.join("/");
}

export function paperRawUrl(projectId: string, fileId: string, revision?: number): string {
  const path = `/api/projects/${encodeURIComponent(projectId)}/paper/files/${encodeURIComponent(fileId)}/raw`;
  return revision === undefined ? path : `${path}?revision=${revision}`;
}

/** Roots are ancestors of the open file, not unrelated documents in the tree. */
export function typstRoots(openPath: string, sources: ReadonlyMap<string, string>): string[] {
  const parents = new Map<string, Set<string>>();
  for (const [path, source] of sources) {
    const withoutComments = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
    for (const match of withoutComments.matchAll(/\b(?:include|import)\s*"([^"\n]+)"/g)) {
      const child = paperRelativePath(path, match[1]);
      if (!child || !sources.has(child)) continue;
      const entries = parents.get(child) ?? new Set<string>();
      entries.add(path);
      parents.set(child, entries);
    }
  }
  const visited = new Set<string>();
  const roots = new Set<string>();
  const visit = (path: string) => {
    if (visited.has(path)) return;
    visited.add(path);
    const entries = parents.get(path);
    if (!entries?.size) roots.add(path);
    else entries.forEach(visit);
  };
  visit(openPath);
  // A cycle has no root. Keep the open document selectable so the compiler can
  // report its actual diagnostic instead of silently choosing another paper.
  return [...(roots.size ? roots : new Set([openPath]))].sort();
}

export async function readTypstTree(
  projectId: string,
  files: PaperFile[],
  revision: number,
  openFile: PaperFile,
  text: string,
  signal: AbortSignal,
): Promise<TypstSource[]> {
  return Promise.all(
    files.map(async (file) => {
      if (file.file_id === openFile.file_id) {
        return { path: file.path, bytes: new TextEncoder().encode(text) };
      }
      const response = await fetch(paperRawUrl(projectId, file.file_id, revision), { signal });
      if (!response.ok) throw new Error(`Could not read ${file.path} (${response.status})`);
      return { path: file.path, bytes: new Uint8Array(await response.arrayBuffer()) };
    }),
  );
}

export function compileTypst(
  sources: TypstSource[],
  root: string,
  signal: AbortSignal,
): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL("./typst.worker.ts", import.meta.url), { type: "module" });
    const finish = () => {
      clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
      worker.terminate();
    };
    const abort = () => {
      finish();
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timeout = window.setTimeout(() => {
      finish();
      reject(new Error("Typst preview timed out"));
    }, 60_000);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) {
      abort();
      return;
    }
    worker.onmessage = ({ data }: MessageEvent<{ pdf?: Uint8Array; error?: string }>) => {
      finish();
      if (data.pdf) resolve(data.pdf);
      else reject(new Error(data.error ?? "Typst compilation failed"));
    };
    worker.onerror = (event) => {
      finish();
      reject(new Error(event.message));
    };
    worker.postMessage({ sources, root });
  });
}
