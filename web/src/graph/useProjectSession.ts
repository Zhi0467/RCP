import { useRefreshRuntime } from "../core/useResource";
import { resourceKeyId } from "../core/resourceStore";
import { graphResourceKey } from "./graphResources";
import type { ProjectSnapshot } from "../core/types";
import { graphSessionKey, sameGraphTarget } from "../core/graphTarget";
import { useCallback, useReducer, useRef, useSyncExternalStore } from "react";
import { emptyHumanDraft, humanDraftChangeCount, type HumanDraft } from "./humanDraft";
import type { TransitionSyncFence } from "./projectTransition";
import type { GraphHeadRef, GraphState } from "../core/types";
import {
  emptyProjectSessionState,
  projectSessionReducer,
  projectSessionSnapshotRequestIsCurrent,
  type ProjectSessionAction,
  type ProjectSessionState,
  type ProjectSessionTabState,
} from "./projectSession";

interface ProjectSessionTransition {
  previous: ProjectSessionState;
  next: ProjectSessionState;
}

export function useProjectSession(initialProjectId: string | null) {
  const { store } = useRefreshRuntime();
  const [, render] = useReducer((value: number) => value + 1, 0);
  // Only local draft/preview coordination is retained here. Canonical payloads
  // are selected from the shared resource on every read, including activation.
  const local = useRef(emptyProjectSessionState(initialProjectId));
  const derived = useRef<{
    local: ProjectSessionState;
    snapshot: ProjectSnapshot | undefined;
    state: ProjectSessionState;
  } | null>(null);
  const getState = useCallback(() => {
    const current = local.current;
    const snapshot = current.projectId
      ? store.view<ProjectSnapshot>(
          resourceKeyId(graphResourceKey(current.projectId, current.graphTarget)),
        ).value
      : undefined;
    const cached = derived.current;
    if (cached?.local === current && cached.snapshot === snapshot) return cached.state;
    const sameTarget =
      cached &&
      cached.state.projectId === current.projectId &&
      sameGraphTarget(cached.state.graphTarget, current.graphTarget);
    // A local edit does not re-accept the same snapshot: doing so would erase
    // its freshly computed preview after stripping the canonical payload.
    let next: ProjectSessionState;
    if (sameTarget && cached.snapshot === snapshot) {
      next = { ...current, project: cached.state.project };
    } else if (snapshot) {
      next = projectSessionReducer(
        {
          ...current,
          project: sameTarget ? cached.state.project : null,
        },
        { kind: "snapshot_applied", snapshot, preserve_readiness: false },
      );
      // Reconciliation changes human drafts and preview coordination, which
      // must survive the next local command without retaining a payload copy.
      local.current = { ...next, project: null };
    } else {
      next = current;
    }
    derived.current = { local: local.current, snapshot, state: next };
    return next;
  }, [store]);

  const state = useSyncExternalStore(store.subscribe, getState, getState);

  const dispatch = useCallback(
    (action: ProjectSessionAction): ProjectSessionTransition => {
      const previous = getState();
      const next = projectSessionReducer(previous, action);
      if (next === previous) return { previous, next };
      // Commands publish their complete atomic snapshot; no second graph copy is
      // restored from saved tab UI or an older request.
      local.current = { ...next, project: null };
      if (action.kind === "restore_tab" || action.kind === "reset") derived.current = null;
      if (next.project && next.project !== previous.project) {
        store.write(
          resourceKeyId(graphResourceKey(next.project.id, next.graphTarget)),
          next.project,
        );
      }
      const accepted = getState();
      render();
      return { previous, next: accepted };
    },
    [getState, store],
  );

  const beginSnapshotRequest = useCallback(
    (projectId: string): number => {
      const requestId = getState().snapshotRequestSequence + 1;
      dispatch({
        kind: "snapshot_request_started",
        project_id: projectId,
        request_id: requestId,
      });
      return requestId;
    },
    [dispatch, getState],
  );

  const snapshotRequestIsCurrent = useCallback(
    (projectId: string, requestId: number): boolean =>
      projectSessionSnapshotRequestIsCurrent(getState(), projectId, requestId),
    [getState],
  );

  const updateHumanDraft = useCallback(
    (
      projectId: string,
      graph: GraphState,
      update: (draft: HumanDraft) => HumanDraft,
    ): ProjectSessionTransition => {
      const updated = update(getState().humanDraft ?? emptyHumanDraft(graph.revision));
      const draft = humanDraftChangeCount(updated) > 0 ? updated : null;
      return dispatch({ kind: "human_draft_updated", project_id: projectId, draft });
    },
    [dispatch, getState],
  );

  const beginSync = useCallback(
    (projectId: string, expectedHead: GraphHeadRef): TransitionSyncFence | null => {
      const current = getState();
      const key = graphSessionKey(projectId, expectedHead.target);
      if (current.transitionCoordinator.sync_requests[key]) return null;
      const syncRequestSequence = current.syncRequestSequence + 1;
      const fence: TransitionSyncFence = {
        project_id: projectId,
        request_id: syncRequestSequence,
        expected_head: expectedHead,
        draft_generation: current.transitionCoordinator.draft_generations[key] ?? 0,
      };
      dispatch({
        kind: "sync_started",
        fence,
        snapshot_request_id: current.snapshotRequestSequence + 1,
        sync_request_sequence: syncRequestSequence,
      });
      return fence;
    },
    [dispatch, getState],
  );

  const restoreTab = useCallback(
    (
      projectId: string,
      cached: ProjectSessionTabState,
      options: { consumeDiscardedProposals?: boolean } = {},
    ): ProjectSessionTransition =>
      dispatch({
        kind: "restore_tab",
        project_id: projectId,
        state: cached,
        ...options,
      }),
    [dispatch],
  );

  return {
    state,
    dispatchProjectSession: dispatch,
    getProjectSessionState: getState,
    beginProjectSnapshotRequest: beginSnapshotRequest,
    projectSnapshotRequestIsCurrent: snapshotRequestIsCurrent,
    updateProjectHumanDraft: updateHumanDraft,
    beginProjectSync: beginSync,
    restoreProjectSessionTab: restoreTab,
  };
}
