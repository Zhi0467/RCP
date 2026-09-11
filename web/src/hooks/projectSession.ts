import { graphSessionKey, MAIN_GRAPH, sameGraphTarget } from "../graphTarget";
import type { GraphTargetRef } from "../types";
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
  type PaperSnapshot,
  type ProjectSnapshot,
  type TransitionTriggerManifest,
} from "../types";

export type BrowserTransitionProjection = ProjectTransitionProjection<
  GraphState,
  Record<string, ExperimentControlState>
>;

/**
 * Which readiness slice of the rendered project survives a settings response.
 *
 * Provider readiness and compute status are invalidated by different edits, so
 * one save or resolve keeps the slice it did not touch. A provider path resolve
 * carries fresh provider readiness while a compute probe may still be in
 * flight; a compute connection or execution-binding change invalidates the
 * matrix while provider readiness stands.
 */
export interface ProjectReadinessRetention {
  provider: boolean;
  compute: boolean;
}

export const RETAIN_ALL_PROJECT_READINESS: ProjectReadinessRetention = {
  provider: true,
  compute: true,
};

export type ProjectSessionManifestState =
  | {
      status: "loading";
      project_id: string | null;
      manifest: TransitionTriggerManifest | null;
    }
  | { status: "valid"; project_id: string; manifest: TransitionTriggerManifest }
  | { status: "invalid"; project_id: string; manifest: null };

export interface ProjectSessionTabState {
  projectId: string | null;
  graphTarget: GraphTargetRef;
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
  draftReconciliationDiscardedProposalIds: string[];
}

export interface ProjectSessionState extends ProjectSessionTabState {
  draftPreviewPending: boolean;
  snapshotRequestSequence: number;
  latestSnapshotRequests: Record<string, number>;
  transitionCoordinator: ProjectTransitionCoordinatorState;
  syncRequestSequence: number;
  paperEditorGeneration: number;
}

export type ProjectSessionAction =
  | { kind: "activate"; project_id: string | null; graph_target?: GraphTargetRef }
  | {
      kind: "reset";
      project_id: string | null;
      graph_target?: GraphTargetRef;
      human_draft?: HumanDraft | null;
    }
  | {
      kind: "restore_tab";
      project_id: string | null;
      state: ProjectSessionTabState;
      consumeDiscardedProposals?: boolean;
    }
  | {
      kind: "snapshot_request_started";
      project_id: string;
      request_id: number;
      graph_target?: GraphTargetRef;
    }
  | {
      kind: "snapshot_applied";
      snapshot: ProjectSnapshot;
      preserve_readiness: boolean;
      request?: { project_id: string; request_id: number };
    }
  | { kind: "project_replaced"; project: ProjectSnapshot | null }
  | { kind: "paper_editor_opened" }
  | {
      kind: "paper_updated";
      project_id: string;
      editor_generation: number;
      paper: PaperSnapshot;
    }
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
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): ProjectSessionState {
  return {
    projectId: initialProjectId,
    graphTarget,
    project: null,
    renderedRevision: 0,
    humanDraft: null,
    transitionHead: { ...canonicalGraphHead(0), target: graphTarget },
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
      graph_target: graphTarget,
    }),
    syncRequestSequence: 0,
    paperEditorGeneration: 0,
  };
}

export function projectSessionReducer(
  state: ProjectSessionState,
  action: ProjectSessionAction,
): ProjectSessionState {
  switch (action.kind) {
    case "activate": {
      const next = withTransitionCoordinator(state, {
        kind: "activate",
        project_id: action.project_id,
        graph_target: action.graph_target ?? state.graphTarget,
      });
      return action.project_id === state.transitionCoordinator.active_project_id
        ? next
        : { ...next, paperEditorGeneration: state.paperEditorGeneration + 1 };
    }
    case "reset": {
      const empty = emptyProjectSessionState(action.project_id, action.graph_target);
      return {
        ...state,
        ...serializeProjectSessionTabState(empty),
        humanDraft: action.human_draft ?? null,
        draftPreviewPending: false,
        paperEditorGeneration: state.paperEditorGeneration + 1,
        transitionCoordinator: reduceProjectTransitionCoordinator(state.transitionCoordinator, {
          kind: "activate",
          project_id: action.project_id,
          graph_target: action.graph_target,
        }),
      };
    }
    case "restore_tab": {
      let transitionCoordinator = reduceProjectTransitionCoordinator(state.transitionCoordinator, {
        kind: "activate",
        project_id: action.project_id,
        graph_target: action.state.graphTarget ?? MAIN_GRAPH,
      });
      if (action.project_id && action.state.project) {
        transitionCoordinator = reduceProjectTransitionCoordinator(transitionCoordinator, {
          kind: "observe_head",
          project_id: action.project_id,
          head: action.state.transitionHead,
        });
      }
      const restored = cloneProjectSessionTabState(
        action.state.projectId === action.project_id &&
          (!action.state.project || action.state.project.id === action.project_id)
          ? action.state
          : emptyProjectSessionState(action.project_id),
      );
      return {
        ...state,
        ...restored,
        projectId: action.project_id,
        draftPreviewPending: false,
        paperEditorGeneration: state.paperEditorGeneration + 1,
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
          [graphSessionKey(action.project_id, action.graph_target ?? state.graphTarget)]:
            action.request_id,
        },
      };
    case "snapshot_applied":
      return applyProjectSnapshot(state, action);
    case "project_replaced": {
      if (
        action.project &&
        (action.project.id !== state.project?.id ||
          action.project.graph.revision !== state.renderedRevision ||
          !sameGraphTarget(action.project.graph_target, state.graphTarget))
      ) {
        return state;
      }
      return state.project === action.project ? state : { ...state, project: action.project };
    }
    case "paper_editor_opened":
      return { ...state, paperEditorGeneration: state.paperEditorGeneration + 1 };
    case "paper_updated": {
      if (
        action.project_id !== state.transitionCoordinator.active_project_id ||
        action.project_id !== state.project?.id ||
        action.editor_generation !== state.paperEditorGeneration
      ) {
        return state;
      }
      return { ...state, project: { ...state.project, paper: action.paper } };
    }
    case "human_draft_loaded":
      return state.humanDraft === action.draft ? state : { ...state, humanDraft: action.draft };
    case "human_draft_updated": {
      if (state.projectId !== action.project_id) return state;
      const nextGeneration =
        (state.transitionCoordinator.draft_generations[
          graphSessionKey(action.project_id, state.graphTarget)
        ] ?? 0) + 1;
      const transitionCoordinator = reduceProjectTransitionCoordinator(
        state.transitionCoordinator,
        {
          kind: "observe_draft_generation",
          project_id: action.project_id,
          generation: nextGeneration,
          graph_target: state.graphTarget,
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
          [graphSessionKey(action.fence.project_id, action.fence.expected_head.target)]:
            action.snapshot_request_id,
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

export function projectDraftPreviewEffectInputs(
  state: Pick<ProjectSessionState, "projectId" | "project" | "humanDraft">,
  projectId: string | null,
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): Pick<ProjectSessionState, "project" | "humanDraft"> {
  if (
    !projectId ||
    state.projectId !== projectId ||
    state.project?.id !== projectId ||
    !sameGraphTarget(state.project.graph_target, graphTarget)
  ) {
    return { project: null, humanDraft: null };
  }
  return { project: state.project, humanDraft: state.humanDraft };
}

export function trustedProjectTransitionManifest(
  state: Pick<
    ProjectSessionState,
    "projectId" | "transitionManifestState" | "transitionRulesetTag"
  >,
  projectId: string | null,
): TransitionTriggerManifest | null {
  const manifestState = state.transitionManifestState;
  if (
    !projectId ||
    state.projectId !== projectId ||
    manifestState.status !== "valid" ||
    manifestState.project_id !== projectId ||
    (state.transitionRulesetTag &&
      manifestState.manifest.ruleset_tag !== state.transitionRulesetTag)
  ) {
    return null;
  }
  return manifestState.manifest;
}

export function projectSettingsSavedProject(
  saved: ProjectSnapshot,
  current: ProjectSnapshot | null,
  retention: ProjectReadinessRetention,
): ProjectSnapshot {
  return preserveProjectReadiness(decodeProjectSnapshot(saved), current, retention);
}

export function latestSnapshotRequestCanApply(
  latestStartedRequestId: number | undefined,
  responseRequestId: number,
): boolean {
  return latestStartedRequestId === responseRequestId;
}

export function projectSessionSnapshotRequestIsCurrent(
  state: Pick<ProjectSessionState, "latestSnapshotRequests"> & { graphTarget?: GraphTargetRef },
  projectId: string,
  requestId: number,
): boolean {
  return latestSnapshotRequestCanApply(
    state.latestSnapshotRequests[graphSessionKey(projectId, state.graphTarget)],
    requestId,
  );
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
  if (
    decodedSnapshot.id !== state.project?.id ||
    !sameGraphTarget(decodedSnapshot.graph_target, state.graphTarget) ||
    decodedSnapshot.snapshot_freshness !== "fresh"
  )
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

export function projectHeartbeatMetadataChanged(
  observed: Partial<Pick<ProjectSnapshot, "snapshot_freshness" | "last_remote_sync_at">>,
  rendered: Partial<Pick<ProjectSnapshot, "snapshot_freshness" | "last_remote_sync_at">> | null,
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): boolean {
  return Boolean(
    graphTarget.kind === "main" &&
    rendered &&
    ((observed.snapshot_freshness !== undefined &&
      observed.snapshot_freshness !== rendered.snapshot_freshness) ||
      (observed.last_remote_sync_at !== undefined &&
        observed.last_remote_sync_at !== rendered.last_remote_sync_at)),
  );
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
  metadataChanged = false,
}: {
  requestedProjectId: string;
  activeProjectId: string | null;
  tabOpen: boolean;
  inactiveState: T | null;
  snapshotRevision: number;
  renderedRevision: number;
  metadataChanged?: boolean;
}): ProjectHeartbeatSnapshotDisposition<T> {
  if (!tabOpen) return { kind: "ignore" };
  if (activeProjectId === requestedProjectId) {
    return snapshotRevision > renderedRevision ||
      (snapshotRevision === renderedRevision && metadataChanged)
      ? { kind: "reload_active" }
      : { kind: "ignore" };
  }
  return inactiveState ? { kind: "reconcile_inactive", state: inactiveState } : { kind: "ignore" };
}

export function persistProjectHumanDraft(
  storage: Pick<Storage, "setItem" | "removeItem">,
  projectId: string,
  draft: HumanDraft | null,
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): void {
  if (draft && humanDraftChangeCount(draft) > 0) {
    storage.setItem(humanDraftStorageKey(projectId, graphTarget), serializeHumanDraft(draft));
  } else {
    storage.removeItem(humanDraftStorageKey(projectId, graphTarget));
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
    decodedProject.id !== state.projectId ||
    !sameGraphTarget(decodedProject.graph_target, state.graphTarget)
  )
    return state;
  if (
    !cachedSnapshotCanReplace(
      state.project?.id ?? state.transitionCoordinator.active_project_id,
      state.renderedRevision,
      decodedProject,
    )
  ) {
    return state;
  }
  if (
    decodedProject.graph_head &&
    (!sameGraphTarget(decodedProject.graph_head.target, state.graphTarget) ||
      decodedProject.graph_head.revision !== decodedProject.graph.revision)
  )
    return state;
  if (
    decodedProject.graph_changes &&
    (!sameGraphTarget(decodedProject.graph_changes.head.target, state.graphTarget) ||
      decodedProject.graph_changes.head.revision !== decodedProject.graph.revision ||
      !transitionHeadsEqual(decodedProject.graph_changes.head, decodedProject.graph_head))
  )
    return state;
  if (state.graphTarget.kind === "branch" && !decodedProject.graph_head) return state;
  const authoritative = decodedProject.snapshot_freshness === "fresh";
  const previousRevision = state.renderedRevision;
  const nextGraph = decodedProject.graph;
  const observedHead =
    state.transitionCoordinator.canonical_heads[
      graphSessionKey(decodedProject.id, state.graphTarget)
    ];
  const nextHead =
    decodedProject.graph_head ??
    (observedHead &&
    sameGraphTarget(observedHead.target, state.graphTarget) &&
    observedHead.revision === nextGraph.revision
      ? observedHead
      : { ...canonicalGraphHead(nextGraph.revision), target: state.graphTarget });
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
    !sameGraphTarget(action.projection.head.target, state.graphTarget) ||
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
  const trustedManifest = trustedProjectTransitionManifest(state, action.project_id);
  const manifestInvalid =
    (state.transitionRulesetTag && state.transitionRulesetTag !== action.projection.ruleset_tag) ||
    (trustedManifest && trustedManifest.ruleset_tag !== action.projection.ruleset_tag);
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
      graph_head: action.projection.head,
      graph_changes: null,
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
          manifest: trustedManifest,
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
    projectId: state.projectId,
    graphTarget: state.graphTarget ?? MAIN_GRAPH,
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
    draftReconciliationDiscardedProposalIds: [...state.draftReconciliationDiscardedProposalIds],
  };
}

function preserveProjectReadiness(
  next: ProjectSnapshot,
  current: ProjectSnapshot | null,
  retention: ProjectReadinessRetention = RETAIN_ALL_PROJECT_READINESS,
): ProjectSnapshot {
  if (!current || current.id !== next.id) return next;
  return {
    ...next,
    ...(retention.compute ? { compute_status: current.compute_status } : {}),
    ...(retention.provider
      ? {
          provider_readiness: current.provider_readiness,
          providers: current.providers,
          provider_skill_inventories: current.provider_skill_inventories,
        }
      : {}),
  };
}
