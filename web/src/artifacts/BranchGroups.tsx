import { useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { BranchTag } from "../core/BranchTag";
import type { GraphRef, ProjectArtifact } from "../core/types";

const MAIN = "main";

export interface BranchGroup<T> {
  key: string;
  name: string;
  items: T[];
}

/** Items under their artifact's branch, in the order each branch first appears. */
export function branchGroups<T>(items: T[], artifactOf: (item: T) => ProjectArtifact) {
  const groups = new Map<string, BranchGroup<T>>();
  for (const item of items) {
    const artifact = artifactOf(item);
    const key = artifact.graph_branch_id ?? MAIN;
    const group = groups.get(key) ?? { key, name: artifact.graph_branch_name, items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  return [...groups.values()];
}

function foldedStorageKey(projectId: string): string {
  return `rcp:artifacts-folded-branches:${projectId}`;
}

function readFolded(projectId: string): Set<string> {
  try {
    const keys: unknown = JSON.parse(localStorage.getItem(foldedStorageKey(projectId)) ?? "[]");
    return new Set(Array.isArray(keys) ? keys.filter((key) => typeof key === "string") : []);
  } catch {
    return new Set();
  }
}

/** The branches this device keeps folded, shared by the Artifacts page and the picker. */
export function useFoldedBranches(projectId: string) {
  const [folded, setFolded] = useState(() => readFolded(projectId));
  const fold = (key: string, closed: boolean) => {
    if (folded.has(key) === closed) return;
    const next = new Set(folded);
    if (closed) next.add(key);
    else next.delete(key);
    setFolded(next);
    try {
      localStorage.setItem(foldedStorageKey(projectId), JSON.stringify([...next]));
    } catch {
      // Folding still works for this visit when the device keeps no storage.
    }
  };
  return { folded, fold };
}

/** One branch's foldable section: chevron, branch tag, and item count. */
export function BranchGroupSection({
  group,
  refs,
  open,
  onFold,
  children,
}: {
  group: BranchGroup<unknown>;
  refs: readonly GraphRef[] | null | undefined;
  open: boolean;
  onFold: (closed: boolean) => void;
  children: ReactNode;
}) {
  return (
    <details
      className="artifacts-branch"
      open={open}
      onToggle={(event) => onFold(!event.currentTarget.open)}
    >
      <summary>
        <ChevronRight className="artifacts-branch-chevron" size={14} aria-hidden="true" />
        <BranchTag
          refs={refs}
          target={group.key === MAIN ? { kind: "main" } : { kind: "branch", branch_id: group.key }}
          name={group.name}
        />
        <span className="artifacts-branch-count">{group.items.length}</span>
      </summary>
      {children}
    </details>
  );
}
