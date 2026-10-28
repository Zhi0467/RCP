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

/** Read literal dependencies, skipping strings, raw text, and nested comments. */
function typstImports(source: string): string[] {
  const imports: string[] = [];
  let dependency = false;
  for (let i = 0; i < source.length;) {
    if (/\s/.test(source[i])) {
      i++;
      continue;
    }
    if (source.startsWith("//", i)) {
      const end = source.indexOf("\n", i);
      i = end < 0 ? source.length : end;
      continue;
    }
    if (source.startsWith("/*", i)) {
      let depth = 1;
      i += 2;
      while (i < source.length && depth) {
        if (source.startsWith("/*", i)) {
          depth++;
          i += 2;
        } else if (source.startsWith("*/", i)) {
          depth--;
          i += 2;
        } else i++;
      }
      continue;
    }
    if (source[i] === '"') {
      const start = i++;
      while (i < source.length) {
        if (source[i++] === "\\") i++;
        else if (source[i - 1] === '"') break;
      }
      if (dependency) {
        try {
          imports.push(JSON.parse(source.slice(start, i)) as string);
        } catch {
          /* An incomplete string will be diagnosed by the compiler. */
        }
      }
      dependency = false;
      continue;
    }
    if (source[i] === "`") {
      const start = i;
      while (source[i] === "`") i++;
      const fence = source.slice(start, i);
      const end = source.indexOf(fence, i);
      i = end < 0 ? source.length : end + fence.length;
      dependency = false;
      continue;
    }
    const word = /^[\p{L}_][\p{L}\p{N}_-]*/u.exec(source.slice(i));
    dependency = word?.[0] === "include" || word?.[0] === "import";
    i += word?.[0].length ?? 1;
  }
  return imports;
}

/** Roots are ancestors of the open file, not unrelated documents in the tree. */
export function typstRoots(openPath: string, sources: ReadonlyMap<string, string>): string[] {
  const parents = new Map<string, Set<string>>();
  for (const [path, source] of sources) {
    for (const reference of typstImports(source)) {
      const child = paperRelativePath(path, reference);
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
  cache: Map<string, Uint8Array>,
): Promise<TypstSource[]> {
  const keys = new Set(files.map((file) => `${projectId}:${file.sha256}`));
  for (const key of cache.keys()) if (!keys.has(key)) cache.delete(key);
  return Promise.all(
    files.map(async (file) => {
      if (file.file_id === openFile.file_id) {
        return { path: file.path, bytes: new TextEncoder().encode(text) };
      }
      const key = `${projectId}:${file.sha256}`;
      const cached = cache.get(key);
      if (cached) return { path: file.path, bytes: cached };
      const response = await fetch(paperRawUrl(projectId, file.file_id, revision), { signal });
      if (!response.ok) throw new Error(`Could not read ${file.path} (${response.status})`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      if (!signal.aborted) cache.set(key, bytes);
      return { path: file.path, bytes };
    }),
  );
}

/** One compiler per mounted preview. Request ids fence superseded results. */
export class TypstCompiler {
  private worker: Worker | null = null;
  private sequence = 0;
  private cancel: (() => void) | null = null;

  dispose() {
    this.cancel?.();
    this.worker?.terminate();
    this.worker = null;
  }

  compile(sources: TypstSource[], root: string, signal: AbortSignal): Promise<Uint8Array> {
    this.cancel?.();
    if (signal.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
    const worker = (this.worker ??= new Worker(new URL("./typst.worker.ts", import.meta.url), {
      type: "module",
    }));
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const finish = () => {
        clearTimeout(timeout);
        signal.removeEventListener("abort", abort);
        this.cancel = null;
      };
      const abort = () => {
        finish();
        reject(new DOMException("Aborted", "AbortError"));
      };
      const fail = (message: string) => {
        finish();
        worker.terminate();
        if (this.worker === worker) this.worker = null;
        reject(new Error(message));
      };
      const timeout = setTimeout(() => fail("Typst preview timed out"), 60_000);
      this.cancel = abort;
      signal.addEventListener("abort", abort, { once: true });
      worker.onmessage = ({
        data,
      }: MessageEvent<{ id: number; pdf?: Uint8Array; error?: string }>) => {
        if (data.id !== id) return;
        finish();
        // A source error leaves the warm worker usable for the next edit.
        if (data.pdf) resolve(data.pdf);
        else reject(new Error(data.error ?? "Typst compilation failed"));
      };
      worker.onerror = (event) => fail(event.message);
      worker.postMessage({ id, sources, root });
    });
  }
}
