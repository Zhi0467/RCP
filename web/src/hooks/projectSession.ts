import {
  humanDraftChangeCount,
  normalizeHumanDraft,
  reconcileHumanDraft,
  retainBehindDraftAfterSync,
  serializeHumanDraft,
  humanDraftStorageKey,
  type HumanDraft,
} from "../humanDraft";
import {
  emptyProjectTransitionCoordinator,
  reduceProjectTransitionCoordinator,
  transitionHeadsEqual,
  type ProjectTransitionCoordinatorState,
  type ProjectTransitionProjection,
  type TransitionSyncFence,
} from "../projectTransition";
import {
  decodeProjectSnapshot,
  type ExperimentControlState,
  type GraphHeadRef,
  type GraphState,
  type ProjectSnapshot,
  type TransitionTriggerManifest,
} from "../types";

export type BrowserTransitionProjection = ProjectTransitionProjection<
  GraphState,
  Record<string, ExperimentControlState>
>;

export type ProjectSessionManifestState =
  | {
      status: "loading";
      project_id: string | null;
      manifest: TransitionTriggerManifest | null;
    }
  | { status: "valid"; project_id: string; manifest: TransitionTriggerManifest }
  | { status: "invalid"; project_id: string; manifest: null };

export interface ProjectSessionTabState {
  project: ProjectSnapshot | null;
  renderedRevision: number;
  humanDraft: HumanDraft | null;
  transitionHead: GraphHeadRef;
  transitionRulesetTag: string | null;
  transitionManifestState: ProjectSessionManifestState;
  transitionManifestRefresh: number;
  transitionManifestExpectedRulesetTag: string | null;
  draftTransitionProjection: BrowserTransitionProjection | null;
  draftPreviewConflict: string | null;
  draftPreviewPending: boolean;
  draftReconciliationDiscardedProposalIds: string[];
}

export interface ProjectSessionState extends ProjectSessionTabState {
  snapshotRequestSequence: number;
  latestSnapshotRequests: Record<string, number>;
  transitionCoordinator: ProjectTransitionCoordinatorState;
  syncRequestSequence: number;
}

export type ProjectSessionAction =
  | { kind: "activate"; project_id: string | null }
  | { kind: "reset"; project_id: string | null; human_draft?: HumanDraft | null }
  | {
      kind: "restore_tab";
      project_id: string | null;
      state: ProjectSessionTabState;
      consumeDiscardedProposals?: boolean;
      clearPendingPreview?: boolean;
    }
  | { kind: "snapshot_request_started"; project_id: string; request_id: number }
  | {
      kind: "snapshot_applied";
      snapshot: ProjectSnapshot;
      preserve_readiness: boolean;
      request?: { project_id: string; request_id: number };
    }
  | { kind: "project_replaced"; project: ProjectSnapshot | null }
  | { kind: "human_draft_loaded"; draft: HumanDraft | null }
  | { kind: "human_draft_updated"; project_id: string; draft: HumanDraft | null }
  | { kind: "discarded_proposals_consumed" }
  | {
      kind: "manifest_loading";
      project_id: string;
      manifest: TransitionTriggerManifest | null;
      expected_ruleset_tag?: string | null;
      refresh?: boolean;
    }
  | { kind: "manifest_valid"; project_id: string; manifest: TransitionTriggerManifest }
  | { kind: "manifest_invalid"; project_id: string }
  | {
      kind: "draft_preview_changed";
      projection: BrowserTransitionProjection | null;
      conflict: string | null;
      pending: boolean;
    }
  | {
      kind: "preview_ruleset_invalidated";
      project_id: string;
      head: GraphHeadRef;
      ruleset_tag: string;
      manifest: TransitionTriggerManifest | null;
    }
  | {
      kind: "preview_applied";
      project_id: string;
      projection: BrowserTransitionProjection;
      base_head: GraphHeadRef;
    }
  | {
      kind: "sync_started";
      fence: TransitionSyncFence;
      snapshot_request_id: number;
      sync_request_sequence: number;
    }
  | { kind: "sync_finished"; fence: TransitionSyncFence }
  | {
      kind: "committed_transition_applied";
      project_id: string;
      projection: BrowserTransitionProjection;
      submitted_draft: HumanDraft;
    };

export function canonicalGraphHead(
  revision: number,
  transitionId: string | null = null,
): GraphHeadRef {
  return { target: { kind: "main" }, revision, transition_id: transitionId };
}

export function emptyProjectSessionState(
  initialProjectId: string | null = null,
): ProjectSessionState {
  return {
    project: null,
    renderedRevision: 0,
    humanDraft: null,
    transitionHead: canonicalGraphHead(0),
    transitionRulesetTag: null,
    transitionManifestState: {
      status: "loading",
      project_id: initialProjectId,
      manifest: null,
    },
    transitionManifestRefresh: 0,
    transitionManifestExpectedRulesetTag: null,
    draftTransitionProjection: null,
    draftPreviewConflict: null,
    draftPreviewPending: false,
    draftReconciliationDiscardedProposalIds: [],
    snapshotRequestSequence: 0,
    latestSnapshotRequests: {},
    transitionCoordinator: reduceProjectTransitionCoordinator(emptyProjectTransitionCoordinator(), {
      kind: "activate",
      project_id: initialProjectId,
    }),
    syncRequestSequence: 0,
  };
}

export function projectSessionReducer(
  state: ProjectSessionState,
  action: ProjectSessionAction,
): ProjectSessionState {
  switch (action.kind) {
    case "activate":
      return withTransitionCoordinator(state, {
        kind: "activate",
        project_id: action.project_id,
      });
    case "reset": {
      const empty = emptyProjectSessionState(action.project_id);
      return {
        ...state,
        ...serializeProjectSessionTabState(empty),
        humanDraft: action.human_draft ?? null,
        transitionCoordinator: reduceProjectTransitionCoordinator(state.transitionCoordinator, {
          kind: "activate",
          project_id: action.project_id,
        }),
      };
    }
    case "restore_tab": {
      let transitionCoordinator = reduceProjectTransitionCoordinator(state.transitionCoordinator, {
        kind: "activate",
        project_id: action.project_id,
      });
      if (action.project_id && action.state.project) {
        transitionCoordinator = reduceProjectTransitionCoordinator(transitionCoordinator, {
          kind: "observe_head",
          project_id: action.project_id,
          head: action.state.transitionHead,
        });
      }
      const restored = cloneProjectSessionTabState(action.state);
      return {
        ...state,
        ...restored,
        draftPreviewPending: action.clearPendingPreview ? false : restored.draftPreviewPending,
        draftReconciliationDiscardedProposalIds: action.consumeDiscardedProposals
          ? []
          : restored.draftReconciliationDiscardedProposalIds,
        transitionCoordinator,
      };
    }
    case "snapshot_request_started":
      return {
        ...state,
        snapshotRequestSequence: action.request_id,
        latestSnapshotRequests: {
          ...state.latestSnapshotRequests,
          [action.project_id]: action.request_id,
        },
      };
    case "snapshot_applied":
      return applyProjectSnapshot(state, action);
    case "project_replaced": {
      if (
        action.project &&
        (action.project.id !== state.project?.id ||
          action.project.graph.revision !== state.renderedRevision)
      ) {
        return state;
      }
      return state.project === action.project ? state : { ...state, project: action.project };
    }
    case "human_draft_loaded":
      return state.humanDraft === action.draft ? state : { ...state, humanDraft: action.draft };
    case "human_draft_updated": {
      const nextGeneration =
        (state.transitionCoordinator.draft_generations[action.project_id] ?? 0) + 1;
      const transitionCoordinator = reduceProjectTransitionCoordinator(
        state.transitionCoordinator,
        {
          kind: "observe_draft_generation",
          project_id: action.project_id,
          generation: nextGeneration,
        },
      );
      return { ...state, humanDraft: action.draft, transitionCoordinator };
    }
    case "discarded_proposals_consumed":
      return state.draftReconciliationDiscardedProposalIds.length === 0
        ? state
        : { ...state, draftReconciliationDiscardedProposalIds: [] };
    case "manifest_loading":
      return {
        ...state,
        transitionManifestState: {
          status: "loading",
          project_id: action.project_id,
          manifest: action.manifest,
        },
        transitionManifestExpectedRulesetTag:
          action.expected_ruleset_tag === undefined
            ? state.transitionManifestExpectedRulesetTag
            : action.expected_ruleset_tag,
        transitionManifestRefresh:
          state.transitionManifestRefresh + (action.refresh === true ? 1 : 0),
      };
    case "manifest_valid":
      return {
        ...state,
        transitionManifestState: {
          status: "valid",
          project_id: action.project_id,
          manifest: action.manifest,
        },
        transitionManifestExpectedRulesetTag: null,
        transitionRulesetTag: action.manifest.ruleset_tag,
      };
    case "manifest_invalid":
      return {
        ...state,
        transitionManifestState: {
          status: "invalid",
          project_id: action.project_id,
          manifest: null,
        },
      };
    case "draft_preview_changed":
      return {
        ...state,
        draftTransitionProjection: action.projection,
        draftPreviewConflict: action.conflict,
        draftPreviewPending: action.pending,
      };
    case "preview_ruleset_invalidated": {
      const transitionCoordinator = reduceProjectTransitionCoordinator(
        state.transitionCoordinator,
        { kind: "observe_head", project_id: action.project_id, head: action.head },
      );
      return {
        ...state,
        transitionHead: action.head,
        transitionRulesetTag: action.ruleset_tag,
        transitionManifestState: {
          status: "loading",
          project_id: action.project_id,
          manifest: action.manifest,
        },
        transitionManifestExpectedRulesetTag: action.ruleset_tag,
        transitionManifestRefresh: state.transitionManifestRefresh + 1,
        draftPreviewConflict: null,
        draftPreviewPending: true,
        transitionCoordinator,
      };
    }
    case "preview_applied": {
      const transitionCoordinator = reduceProjectTransitionCoordinator(
        state.transitionCoordinator,
        { kind: "observe_head", project_id: action.project_id, head: action.base_head },
      );
      return {
        ...state,
        draftTransitionProjection: action.projection,
        transitionHead: transitionHeadsEqual(state.transitionHead, action.base_head)
          ? state.transitionHead
          : action.base_head,
        transitionRulesetTag: action.projection.ruleset_tag,
        draftPreviewConflict: null,
        draftPreviewPending: false,
        transitionCoordinator,
      };
    }
    case "sync_started":
      return {
        ...state,
        snapshotRequestSequence: action.snapshot_request_id,
        latestSnapshotRequests: {
          ...state.latestSnapshotRequests,
          [action.fence.project_id]: action.snapshot_request_id,
        },
        syncRequestSequence: action.sync_request_sequence,
        transitionCoordinator: reduceProjectTransitionCoordinator(state.transitionCoordinator, {
          kind: "sync_started",
          fence: action.fence,
        }),
      };
    case "sync_finished":
      return withTransitionCoordinator(state, { kind: "sync_finished", fence: action.fence });
    case "committed_transition_applied":
      return applyCommittedTransition(state, action);
  }
}

export function serializeProjectSessionTabState(
  state: ProjectSessionTabState,
): ProjectSessionTabState {
  return cloneProjectSessionTabState(state);
}

export function latestSnapshotRequestCanApply(
  latestStartedRequestId: number | undefined,
  responseRequestId: number,
): boolean {
  return latestStartedRequestId === responseRequestId;
}

export function projectSessionSnapshotRequestIsCurrent(
  state: Pick<ProjectSessionState, "latestSnapshotRequests">,
  projectId: string,
  requestId: number,
): boolean {
  return latestSnapshotRequestCanApply(state.latestSnapshotRequests[projectId], requestId);
}

export function cachedSnapshotCanReplace(
  renderedProjectId: string | null,
  renderedRevision: number,
  snapshot: ProjectSnapshot,
): boolean {
  return snapshot.id !== renderedProjectId || snapshot.graph.revision >= renderedRevision;
}

export function reconcileInactiveProjectSession(
  state: ProjectSessionTabState,
  snapshot: ProjectSnapshot,
): ProjectSessionTabState {
  const decodedSnapshot = decodeProjectSnapshot(snapshot);
  if (decodedSnapshot.id !== state.project?.id || decodedSnapshot.snapshot_freshness !== "fresh")
    return state;
  const session = {
    ...emptyProjectSessionState(state.project?.id ?? null),
    ...cloneProjectSessionTabState(state),
  };
  const next = applyProjectSnapshot(session, {
    kind: "snapshot_applied",
    snapshot: decodedSnapshot,
    preserve_readiness: false,
  });
  return next === session ? state : serializeProjectSessionTabState(next);
}

export type ProjectHeartbeatSnapshotDisposition<T extends ProjectSessionTabState> =
  { kind: "ignore" } | { kind: "reload_active" } | { kind: "reconcile_inactive"; state: T };

export function projectHeartbeatSnapshotDisposition<T extends ProjectSessionTabState>({
  requestedProjectId,
  activeProjectId,
  tabOpen,
  inactiveState,
  snapshotRevision,
  renderedRevision,
}: {
  requestedProjectId: string;
  activeProjectId: string | null;
  tabOpen: boolean;
  inactiveState: T | null;
  snapshotRevision: number;
  renderedRevision: number;
}): ProjectHeartbeatSnapshotDisposition<T> {
  if (!tabOpen) return { kind: "ignore" };
  if (activeProjectId === requestedProjectId) {
    return snapshotRevision > renderedRevision ? { kind: "reload_active" } : { kind: "ignore" };
  }
  return inactiveState ? { kind: "reconcile_inactive", state: inactiveState } : { kind: "ignore" };
}

export function persistProjectHumanDraft(
  storage: Pick<Storage, "setItem" | "removeItem">,
  projectId: string,
  draft: HumanDraft | null,
): void {
  if (draft && humanDraftChangeCount(draft) > 0) {
    storage.setItem(humanDraftStorageKey(projectId), serializeHumanDraft(draft));
  } else {
    storage.removeItem(humanDraftStorageKey(projectId));
  }
}

function applyProjectSnapshot(
  state: ProjectSessionState,
  action: Extract<ProjectSessionAction, { kind: "snapshot_applied" }>,
): ProjectSessionState {
  if (
    action.request &&
    !projectSessionSnapshotRequestIsCurrent(
      state,
      action.request.project_id,
      action.request.request_id,
    )
  ) {
    return state;
  }
  const decodedProject = decodeProjectSnapshot(action.snapshot);
  if (
    !cachedSnapshotCanReplace(
      state.project?.id ?? state.transitionCoordinator.active_project_id,
      state.renderedRevision,
      decodedProject,
    )
  ) {
    return state;
  }
  const authoritative = decodedProject.snapshot_freshness === "fresh";
  const previousRevision = state.renderedRevision;
  const nextGraph = decodedProject.graph;
  const observedHead = state.transitionCoordinator.canonical_heads[decodedProject.id];
  const nextHead =
    observedHead?.target.kind === "main" && observedHead.revision === nextGraph.revision
      ? observedHead
      : canonicalGraphHead(nextGraph.revision);
  const reconciliation = state.humanDraft
    ? authoritative
      ? reconcileHumanDraft(state.humanDraft, nextGraph)
      : { draft: normalizeHumanDraft(state.humanDraft, nextGraph), discardedProposalIds: [] }
    : null;
  const rebasedDraft = reconciliation?.draft ?? null;
  const humanDraft = rebasedDraft && humanDraftChangeCount(rebasedDraft) > 0 ? rebasedDraft : null;
  const revisionAdvanced = authoritative && nextGraph.revision !== previousRevision;
  const transitionCoordinator = reduceProjectTransitionCoordinator(state.transitionCoordinator, {
    kind: "observe_head",
    project_id: decodedProject.id,
    head: nextHead,
  });
  return {
    ...state,
    project: action.preserve_readiness
      ? preserveProjectReadiness(decodedProject, state.project)
      : decodedProject,
    renderedRevision: nextGraph.revision,
    humanDraft,
    transitionHead: nextHead,
    transitionManifestState: revisionAdvanced
      ? {
          status: "loading",
          project_id: decodedProject.id,
          manifest:
            state.transitionManifestState.project_id === decodedProject.id
              ? state.transitionManifestState.manifest
              : null,
        }
      : state.transitionManifestState,
    transitionManifestRefresh: state.transitionManifestRefresh + (revisionAdvanced ? 1 : 0),
    transitionManifestExpectedRulesetTag: revisionAdvanced
      ? null
      : state.transitionManifestExpectedRulesetTag,
    draftTransitionProjection: null,
    draftPreviewConflict: null,
    draftPreviewPending: false,
    draftReconciliationDiscardedProposalIds: [
      ...new Set([
        ...state.draftReconciliationDiscardedProposalIds,
        ...(reconciliation?.discardedProposalIds ?? []),
      ]),
    ],
    transitionCoordinator,
  };
}

function applyCommittedTransition(
  state: ProjectSessionState,
  action: Extract<ProjectSessionAction, { kind: "committed_transition_applied" }>,
): ProjectSessionState {
  const project = state.project;
  if (
    !project ||
    project.id !== action.project_id ||
    action.projection.head.target.kind !== "main" ||
    action.projection.head.revision < state.renderedRevision
  ) {
    return state;
  }
  const nextGraph = action.projection.graph;
  const retainedDraft = retainBehindDraftAfterSync(
    action.submitted_draft,
    project.graph,
    nextGraph,
  );
  const manifestInvalid =
    (state.transitionRulesetTag && state.transitionRulesetTag !== action.projection.ruleset_tag) ||
    (state.transitionManifestState.manifest &&
      state.transitionManifestState.manifest.ruleset_tag !== action.projection.ruleset_tag);
  const transitionCoordinator = reduceProjectTransitionCoordinator(state.transitionCoordinator, {
    kind: "observe_head",
    project_id: action.project_id,
    head: action.projection.head,
  });
  return {
    ...state,
    project: {
      ...project,
      graph: nextGraph,
      revision: nextGraph.revision,
      experiment_control: action.projection.experiment_control,
      attention: action.projection.attention,
      primary_question: action.projection.primary_question,
      counts: action.projection.counts,
    },
    renderedRevision: nextGraph.revision,
    humanDraft: retainedDraft,
    transitionHead: action.projection.head,
    transitionRulesetTag: action.projection.ruleset_tag,
    transitionManifestState: manifestInvalid
      ? {
          status: "loading",
          project_id: action.project_id,
          manifest: state.transitionManifestState.manifest,
        }
      : state.transitionManifestState,
    transitionManifestExpectedRulesetTag: manifestInvalid
      ? action.projection.ruleset_tag
      : state.transitionManifestExpectedRulesetTag,
    transitionManifestRefresh: state.transitionManifestRefresh + (manifestInvalid ? 1 : 0),
    draftTransitionProjection: null,
    draftPreviewConflict: null,
    draftPreviewPending: false,
    transitionCoordinator,
  };
}

function withTransitionCoordinator(
  state: ProjectSessionState,
  action: Parameters<typeof reduceProjectTransitionCoordinator>[1],
): ProjectSessionState {
  const transitionCoordinator = reduceProjectTransitionCoordinator(
    state.transitionCoordinator,
    action,
  );
  return transitionCoordinator === state.transitionCoordinator
    ? state
    : { ...state, transitionCoordinator };
}

function cloneProjectSessionTabState(state: ProjectSessionTabState): ProjectSessionTabState {
  return {
    project: state.project,
    renderedRevision: state.renderedRevision,
    humanDraft: state.humanDraft,
    transitionHead: state.transitionHead,
    transitionRulesetTag: state.transitionRulesetTag,
    transitionManifestState: state.transitionManifestState,
    transitionManifestRefresh: state.transitionManifestRefresh,
    transitionManifestExpectedRulesetTag: state.transitionManifestExpectedRulesetTag,
    draftTransitionProjection: state.draftTransitionProjection,
    draftPreviewConflict: state.draftPreviewConflict,
    draftPreviewPending: state.draftPreviewPending,
    draftReconciliationDiscardedProposalIds: [...state.draftReconciliationDiscardedProposalIds],
  };
}

function preserveProjectReadiness(
  next: ProjectSnapshot,
  current: ProjectSnapshot | null,
): ProjectSnapshot {
  if (!current || current.id !== next.id) return next;
  return {
    ...next,
    provider_readiness: current.provider_readiness,
    providers: current.providers,
    provider_skill_inventories: current.provider_skill_inventories,
  };
}
