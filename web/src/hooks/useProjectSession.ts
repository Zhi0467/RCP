import { useCallback, useReducer, useRef } from "react";
import { emptyHumanDraft, humanDraftChangeCount, type HumanDraft } from "../humanDraft";
import type { TransitionSyncFence } from "../projectTransition";
import type { GraphHeadRef, GraphState } from "../types";
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
  const [state, reactDispatch] = useReducer(
    projectSessionReducer,
    initialProjectId,
    emptyProjectSessionState,
  );
  const stateRef = useRef(state);

  const dispatch = useCallback((action: ProjectSessionAction): ProjectSessionTransition => {
    const previous = stateRef.current;
    const next = projectSessionReducer(previous, action);
    stateRef.current = next;
    if (next !== previous) reactDispatch(action);
    return { previous, next };
  }, []);

  const getState = useCallback(() => stateRef.current, []);

  const beginSnapshotRequest = useCallback(
    (projectId: string): number => {
      const requestId = stateRef.current.snapshotRequestSequence + 1;
      dispatch({
        kind: "snapshot_request_started",
        project_id: projectId,
        request_id: requestId,
      });
      return requestId;
    },
    [dispatch],
  );

  const snapshotRequestIsCurrent = useCallback(
    (projectId: string, requestId: number): boolean =>
      projectSessionSnapshotRequestIsCurrent(stateRef.current, projectId, requestId),
    [],
  );

  const updateHumanDraft = useCallback(
    (
      projectId: string,
      graph: GraphState,
      update: (draft: HumanDraft) => HumanDraft,
    ): ProjectSessionTransition => {
      const updated = update(stateRef.current.humanDraft ?? emptyHumanDraft(graph.revision));
      const draft = humanDraftChangeCount(updated) > 0 ? updated : null;
      return dispatch({ kind: "human_draft_updated", project_id: projectId, draft });
    },
    [dispatch],
  );

  const beginSync = useCallback(
    (projectId: string, expectedHead: GraphHeadRef): TransitionSyncFence | null => {
      const current = stateRef.current;
      if (current.transitionCoordinator.sync_requests[projectId]) return null;
      const syncRequestSequence = current.syncRequestSequence + 1;
      const fence: TransitionSyncFence = {
        project_id: projectId,
        request_id: syncRequestSequence,
        expected_head: expectedHead,
        draft_generation: current.transitionCoordinator.draft_generations[projectId] ?? 0,
      };
      dispatch({
        kind: "sync_started",
        fence,
        snapshot_request_id: current.snapshotRequestSequence + 1,
        sync_request_sequence: syncRequestSequence,
      });
      return fence;
    },
    [dispatch],
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
