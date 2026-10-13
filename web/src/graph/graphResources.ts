import { decodeTransitionTriggerManifest, transitionHeadsEqual } from "./projectTransition";
import { api } from "../core/api";
import { graphTargetUrl, MAIN_GRAPH, sameGraphTarget } from "../core/graphTarget";
import { type ResourceKey, type ResourceProfile } from "../core/resourceStore";
import {
  decodeProjectSnapshot,
  type GraphTargetRef,
  type ProjectSnapshot,
  type GraphRef,
  type TransitionTriggerManifest,
  type RevisionSummary,
} from "../core/types";
import { preserveUnchangedProjectSlices } from "./projectSession";

export function graphResourceKey(
  project: string,
  target: GraphTargetRef = MAIN_GRAPH,
): ResourceKey {
  return {
    backend: "",
    member: "",
    project,
    name: "graph",
    ref: target.kind === "main" ? "main" : `branch:${target.branch_id}`,
  };
}

export function graphProfile(
  projectId: string,
  target: GraphTargetRef = MAIN_GRAPH,
): ResourceProfile<ProjectSnapshot> {
  // Capture the target, never consult the currently selected tab after an await.
  const key = graphResourceKey(projectId, target);
  const path = graphTargetUrl(`/api/projects/${encodeURIComponent(projectId)}`, target);
  return {
    key,
    async fetch(signal) {
      const snapshot = decodeProjectSnapshot(await api<ProjectSnapshot>(path, { signal }));
      if (
        snapshot.id !== projectId ||
        !sameGraphTarget(snapshot.graph_target, target) ||
        (snapshot.graph_head &&
          (!sameGraphTarget(snapshot.graph_head.target, target) ||
            snapshot.graph_head.revision !== snapshot.graph.revision)) ||
        (target.kind === "branch" && !snapshot.graph_head) ||
        (snapshot.graph_changes &&
          !transitionHeadsEqual(snapshot.graph_changes.head, snapshot.graph_head))
      ) {
        throw new Error("Project snapshot does not match its graph head.");
      }
      return snapshot;
    },
    reconcile(previous, next) {
      if (previous && next.graph.revision < previous.graph.revision) return previous;
      return previous ? preserveUnchangedProjectSlices(previous, next) : next;
    },
  };
}

export function manifestProfile(
  projectId: string,
  target: GraphTargetRef,
  ruleset: string | null,
): ResourceProfile<TransitionTriggerManifest | null> {
  return {
    key: { ...graphResourceKey(projectId, target), query: JSON.stringify(["manifest", ruleset]) },
    async fetch(signal) {
      return decodeTransitionTriggerManifest(
        await api<unknown>(
          graphTargetUrl(
            `/api/projects/${encodeURIComponent(projectId)}/transition-manifest`,
            target,
          ),
          { signal },
        ),
        ruleset,
      );
    },
  };
}

export function historyProfile(
  projectId: string,
  target: GraphTargetRef,
  revision?: number,
): ResourceProfile<RevisionSummary[]> {
  const path = `/api/projects/${encodeURIComponent(projectId)}/history/summaries`;
  return {
    key: {
      ...graphResourceKey(projectId, target),
      query: revision === undefined ? "history" : `history:${revision}`,
    },
    fetch: (signal) =>
      api(
        graphTargetUrl(
          revision === undefined
            ? path
            : `${path}?from_revision=${revision}&to_revision=${revision}`,
          target,
        ),
        { signal },
      ),
  };
}

export function refsProfile(projectId: string): ResourceProfile<GraphRef[]> {
  return {
    key: { backend: "", member: "", project: projectId, name: "refs" },
    fetch: (signal) => api(`/api/projects/${encodeURIComponent(projectId)}/graph-refs`, { signal }),
  };
}
