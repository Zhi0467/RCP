import type {
  MachineDirectoryEntry,
  MachineDirectoryListing,
  MachineDirectoryRequest,
} from "./types";

/** What the picker shows: one folder level, narrowed by a server-side filter. */
export interface PathPickerState {
  /** Null until the first listing answers. */
  path: string | null;
  parent: string | null;
  filter: string;
  entries: MachineDirectoryEntry[];
  total: number;
  nextOffset: number | null;
}

export const EMPTY_PATH_PICKER: PathPickerState = {
  path: null,
  parent: null,
  filter: "",
  entries: [],
  total: 0,
  nextOffset: null,
};

/** Open a folder (the filter clears), narrow the current one, or fetch its next page. */
export type PathPickerMove =
  { kind: "open"; path: string | null } | { kind: "filter"; filter: string } | { kind: "more" };

export function directoryRequest(
  state: PathPickerState,
  move: PathPickerMove,
): MachineDirectoryRequest {
  if (move.kind === "open") return { path: move.path };
  const filter = move.kind === "filter" ? move.filter.trim() : state.filter.trim();
  const request: MachineDirectoryRequest = { path: state.path };
  if (filter) request.filter = filter;
  if (move.kind === "more" && state.nextOffset !== null) request.offset = state.nextOffset;
  return request;
}

export function applyDirectoryPage(
  state: PathPickerState,
  move: PathPickerMove,
  page: MachineDirectoryListing,
): PathPickerState {
  return {
    path: page.path,
    parent: page.parent,
    filter: move.kind === "open" ? "" : move.kind === "filter" ? move.filter : state.filter,
    entries: move.kind === "more" ? [...state.entries, ...page.entries] : page.entries,
    total: page.total,
    nextOffset: page.next_offset,
  };
}

/** Each ancestor of an absolute path, root first, for one-click navigation. */
export function pathBreadcrumbs(path: string): Array<{ label: string; path: string }> {
  const parts = path.split("/").filter(Boolean);
  const crumbs = [{ label: "/", path: "/" }];
  parts.forEach((part, index) => {
    crumbs.push({ label: part, path: `/${parts.slice(0, index + 1).join("/")}` });
  });
  return crumbs;
}
