import { api } from "../core/api.ts";

export const PAPER_GIT_TASK = "paper-git";

export interface PaperGitStatus {
  state: "none" | "pending" | "linked";
  repository: string;
  branch: string;
  public_key: string;
  key_source: string;
  head: { commit: string; author: string };
  unpushed: boolean;
  skipped: { path: string; reason: string }[];
  last_error: string;
  checked_at: string;
}
export interface PaperGitChangedFile {
  path: string;
  status: "added" | "modified" | "deleted";
  added: number | null;
  removed: number | null;
}
export interface PaperGitFileDiff {
  lines: string[];
  more: number;
  binary: boolean;
}
export function repositoryFromInput(input: string): string {
  const text = input.trim();
  const match =
    /^(?:https?:\/\/)?(?:www\.)?github\.com\/([^/\s]+\/[^/\s#?]+?)(?:\.git)?\/?(?:[#?].*)?$/i.exec(
      text,
    ) ?? /^git@github\.com:([^/\s]+\/[^/\s]+?)(?:\.git)?$/i.exec(text);
  return match ? match[1] : text;
}

export function validRepository(repository: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(repository);
}

export function deployKeyUrl(repository: string): string {
  return `https://github.com/${repository}/settings/keys/new`;
}

export function gitPill(status: PaperGitStatus | null) {
  const linked = status?.state === "linked";
  return {
    branch: linked ? status.branch : "",
    failed: Boolean(status?.last_error),
    hover: status?.last_error || status?.checked_at || "",
  };
}

export function paperGit<T>(
  apiBase: string,
  route: string,
  method: "GET" | "POST" | "DELETE" = "GET",
  body?: object,
): Promise<T> {
  return api<T>(`${apiBase}/paper/git${route}`, {
    method,
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
}
