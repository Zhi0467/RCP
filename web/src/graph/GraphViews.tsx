import { useEvidenceFlags } from "./EvidenceFlagContext";
import { branchName } from "../core/branchNames";
import { MAIN_GRAPH } from "../core/graphTarget";
import { ExperimentLoopMetadata } from "../experiments/ExperimentLoopMetadata";
import { ComputeRouteNotice } from "../experiments/ComputeRouteNotice";
import { ProviderLoginNotice } from "../projects/ProviderLoginNotice";
import { branchGraphProjection, expandBranchContext } from "./branchGraph";
import { graphSessionKey } from "../core/graphTarget";
import { ChangedFields, ChangeHistory } from "./BranchChangeDetail";
import { BranchTag } from "../core/BranchTag";
import { EpisodeMergePanel } from "../experiments/EpisodeMergePanel";
import { type BranchDiffWord, branchDiffWord, mergeDiffMarks } from "./mergePanel";
import type {
  GraphBranchChanges,
  GraphRef,
  GraphTargetRef,
  MergeDiffPath,
  MergeEpisodeBody,
} from "../core/types";

const BRANCH_DIFF_WORDS: Record<BranchDiffWord, string> = {
  created: "Added",
  updated: "Changed",
  removed: "Removed",
  changed: "Changed",
  delivered: "Delivered",
  proposal: "Review",
  conflict: "Conflict",
};
const BRANCH_DIFF_LEGEND: BranchDiffWord[] = [
  "created",
  "updated",
  "removed",
  "proposal",
  "conflict",
  "delivered",
];
import {
  ChevronDown,
  CircleDot,
  Eye,
  EyeOff,
  FlaskConical,
  Focus,
  Gauge,
  GitBranch,
  Lightbulb,
  LightbulbOff,
  Link2,
  Maximize2,
  Minimize2,
  Orbit,
  Pencil,
  Pin,
  PinOff,
  RotateCcw,
  Scan,
  Workflow,
  X,
} from "lucide-react";
import {
  lensBrightEdge,
  lensBrightNode,
  lensShows,
  readDagLens,
  toggled,
  writeDagLens,
  type DagLens,
} from "./dagLens";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MutableRefObject,
  type PointerEvent as ReactPointerEvent,
  type Ref,
  type ReactNode,
} from "react";
import {
  buildNodeProjectionEmphasis,
  buildDagProjection,
  edgeProjectionEmphasis,
  relationFocus,
  type DagOntologyProjection,
} from "./graphProjection";
import {
  DAG_ZOOM_MIN,
  fitDagToViewport,
  zoomDagAtPoint,
  type DagViewport,
  type DagZoomResult,
} from "./dagZoom";
import {
  DAG_NODE_HEIGHT,
  DAG_NODE_WIDTH,
  useForceDag,
  type DagLayoutMode,
  type DagPosition,
} from "./useForceDag";
import { buildExperimentRun, type ExperimentRun } from "../experiments/runProjection";
import {
  ExperimentRunDetail,
  experimentHealthLabel,
  experimentHealthTone,
} from "../experiments/ExperimentRunDetail";
import type { GraphEditingProps } from "./canvasEditing";
import { useDagCanvasEditor } from "./DagCanvasEditor";
import { AutoResearchEpisodeCard, EpisodeBudgetMeter } from "../experiments/CampaignRuns";
import {
  EpisodeArchiveButton,
  EpisodeAuthor,
  type ArchiveEpisodeAction,
} from "../experiments/EpisodeRunControls";
import { runsEpisodeCards } from "../experiments/campaigns";
import {
  currentExperimentEntries,
  unavailableExperimentTargets,
  mainExperimentRouteMatchesControl,
  type ExperimentRouteIdentity,
} from "../experiments/experimentBoardModel";
import type {
  ProviderLoginState,
  Machine,
  AgentRunConfig,
  AgentTask,
  Episode,
  EpisodeRunSection,
  ExperimentControlState,
  ExperimentLoopIndexEntry,
  UnavailableExperimentLoop,
  GraphNode,
  GraphState,
  ProjectSnapshot,
  Proposal,
  TrustView,
  WatcherRecord,
} from "../core/types";
import { humanize, nodeTypeLabel } from "./nodePresentation";
import { RESEARCH, TYPE_LENS, isControlNode } from "./researchType";

export function focusRunDetail(detail: Pick<HTMLDivElement, "focus" | "scrollIntoView">): void {
  detail.focus({ preventScroll: true });
  detail.scrollIntoView({ block: "center" });
}

interface Props {
  graph: GraphState;
  trustView: TrustView;
  onSelectNode: (node: GraphNode) => void;
  /** Main nodes changed since the viewer's digest mark; empty on other targets. */
  changedNodeIds?: ReadonlySet<string>;
}

const dagTypes = RESEARCH.nodeTypes;
const NO_CHANGED_NODES: ReadonlySet<string> = new Set();
const dagTypeMeta = TYPE_LENS;

interface DagProps extends Props, GraphEditingProps {
  graphRefs?: GraphRef[] | null;
  graphTarget?: GraphTargetRef;
  branchChanges?: GraphBranchChanges | null;
  /** The branch's changed fields as the merge builder classifies them, when loaded. */
  mergePaths?: MergeDiffPath[] | null;
  onInspectTask?: (taskId: string) => void;
  /** Session-scoped pan and zoom, owned by the shell so it survives leaving the view. */
  viewportRef: MutableRefObject<DagViewport | null>;
  relationFocusNodeId?: string | null;
  onClearRelationFocus?: () => void;
}

interface DragState {
  nodeId: string;
  pointerId: number;
  startX: number;
  startY: number;
  moved: boolean;
  original?: DagPosition;
}

export function DagView({
  graph,
  trustView,
  onSelectNode,
  projectId,
  graphTarget,
  branchChanges,
  graphRefs,
  mergePaths,
  onInspectTask,
  viewportRef,
  relationFocusNodeId,
  onClearRelationFocus,
  changedNodeIds = NO_CHANGED_NODES,
  ...editing
}: DagProps) {
  const { nodeIds: flaggedNodeIds } = useEvidenceFlags();
  const [toolbarMenu, setToolbarMenu] = useState<"layout" | "view" | null>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!toolbarMenu) return;
    const outside = (event: PointerEvent) => {
      if (!toolbarRef.current?.contains(event.target as Node)) setToolbarMenu(null);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setToolbarMenu(null);
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [toolbarMenu]);
  const [expandedContext, setExpandedContext] = useState<Set<string> | null>(() => new Set());
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const displayGraph = useMemo(
    () => (branchChanges ? branchGraphProjection(graph, branchChanges, expandedContext) : graph),
    [graph, branchChanges, expandedContext],
  );
  const nodeChanges = useMemo(
    () => new Map(branchChanges?.nodes.map((change) => [change.node_id, change]) ?? []),
    [branchChanges],
  );
  const edgeChanges = useMemo(
    () => new Map(branchChanges?.edges.map((change) => [change.edge_id, change]) ?? []),
    [branchChanges],
  );
  const mergeMarks = useMemo(() => mergeDiffMarks(mergePaths ?? []), [mergePaths]);
  const changeTasks = useMemo(
    () => [
      ...new Map(
        [...(branchChanges?.nodes ?? []), ...(branchChanges?.edges ?? [])].flatMap((change) =>
          change.history
            .filter((source) => source.task_id)
            .map((source) => [source.task_id!, source] as const),
        ),
      ).values(),
    ],
    [branchChanges],
  );
  const taskHighlights = useMemo(() => {
    const nodes = new Set<string>();
    for (const change of branchChanges?.nodes ?? []) {
      if (change.history.some((source) => source.task_id === selectedTaskId))
        nodes.add(change.node_id);
    }
    for (const change of branchChanges?.edges ?? []) {
      if (!change.history.some((source) => source.task_id === selectedTaskId)) continue;
      for (const edge of [change.before, change.after]) {
        if (edge) {
          nodes.add(edge.source);
          nodes.add(edge.target);
        }
      }
    }
    return nodes;
  }, [branchChanges, selectedTaskId]);
  const projection = useMemo(
    () =>
      buildDagProjection(displayGraph, trustView, relationFocusNodeId, {
        includeResolvedBlockers: Boolean(branchChanges),
      }),
    [displayGraph, relationFocusNodeId, trustView, branchChanges],
  );
  const focusNodeId =
    relationFocusNodeId && graph.nodes[relationFocusNodeId] ? relationFocusNodeId : null;
  const focusedRelations = useMemo(
    () => (relationFocusNodeId ? relationFocus(relationFocusNodeId, projection.edges) : null),
    [projection.edges, relationFocusNodeId],
  );
  const [lensState, setLensState] = useState(() => ({
    projectId,
    lens: readDagLens(projectId),
  }));
  if (lensState.projectId !== projectId) setLensState({ projectId, lens: readDagLens(projectId) });
  const lens = lensState.lens;
  const updateLens = (change: Partial<DagLens>) =>
    setLensState((current) => ({ ...current, lens: { ...current.lens, ...change } }));
  useEffect(() => writeDagLens(lensState.projectId, lensState.lens), [lensState]);
  const [ontologyProjection, setOntologyProjection] = useState<DagOntologyProjection>("all");
  const nodeProjectionEmphasis = useMemo(
    () => buildNodeProjectionEmphasis(projection.edges, ontologyProjection),
    [ontologyProjection, projection.edges],
  );
  const [repulsion, setRepulsion] = useState(readRepulsion);
  const [layoutMode, setLayoutMode] = useState<DagLayoutMode>(readDagLayoutMode);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [zoom, setZoom] = useState(() => viewportRef.current?.zoom ?? 1);
  const [canvasOffset, setCanvasOffset] = useState(() => ({
    offsetX: viewportRef.current?.offsetX ?? 0,
    offsetY: viewportRef.current?.offsetY ?? 0,
  }));
  const canvasOffsetRef = useRef(canvasOffset);
  // An explicit relation focus outranks the remembered viewport: it is a request to look somewhere.
  const restoringViewportRef = useRef(relationFocusNodeId ? null : viewportRef.current);
  const shellRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef(zoom);
  // The current fitted or remembered floor, so gestures can return to that view.
  // Each Fit resets it for the current layout.
  const gestureFloorRef = useRef(viewportRef.current?.floor ?? Math.min(DAG_ZOOM_MIN, zoom));
  const pendingZoomScrollRef = useRef<DagZoomResult | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const dragWatchdogRef = useRef<number | null>(null);
  const suppressClickRef = useRef<string | null>(null);
  const framedLayoutRef = useRef<string | null>(null);
  const commitViewport = useCallback(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    viewportRef.current = {
      ...canvasOffsetRef.current,
      zoom: zoomRef.current,
      floor: gestureFloorRef.current,
      scrollLeft: scroller.scrollLeft,
      scrollTop: scroller.scrollTop,
    };
  }, [viewportRef]);
  const layout = useForceDag({
    nodes: projection.nodes,
    edges: projection.edges,
    projectId: graphSessionKey(projectId, graphTarget),
    repulsion,
    mode: layoutMode,
  });
  const canEdit = !editing.mutationsDisabled && Boolean(editing.editOptions);
  const editor = useDagCanvasEditor({
    graph,
    editing: { ...editing, projectId },
    enabled: canEdit,
    positions: layout.positions,
    canvasRef,
    scrollRef,
    zoom,
    pinAt: layout.pinAt,
    onSelectNode,
  });
  const typeCounts = useMemo(
    () =>
      Object.fromEntries(
        dagTypes.map((type) => [
          type,
          projection.nodes.filter((node) => node.type === type).length,
        ]),
      ) as Record<GraphNode["type"], number>,
    [projection.nodes],
  );
  const layoutReady = Object.keys(layout.positions).length === projection.nodes.length;

  useEffect(() => {
    try {
      window.localStorage.setItem("rcp:dag-repulsion", String(repulsion));
    } catch {
      // Keep the control usable when browser storage is unavailable.
    }
  }, [repulsion]);

  useEffect(() => {
    try {
      window.localStorage.setItem("rcp:dag-layout-mode", layoutMode);
    } catch {
      // Keep the layout switch usable when browser storage is unavailable.
    }
  }, [layoutMode]);

  useEffect(
    () => () => {
      if (dragWatchdogRef.current !== null) window.clearTimeout(dragWatchdogRef.current);
    },
    [],
  );

  useEffect(() => {
    const updateFullscreenState = () =>
      setIsFullscreen(document.fullscreenElement === shellRef.current);
    document.addEventListener("fullscreenchange", updateFullscreenState);
    return () => document.removeEventListener("fullscreenchange", updateFullscreenState);
  }, []);

  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const pinchZoom = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const rect = scroller.getBoundingClientRect();
      const pending = pendingZoomScrollRef.current;
      const deltaScale =
        event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroller.clientHeight : 1;
      const next = zoomDagAtPoint({
        ...canvasOffsetRef.current,
        zoom: zoomRef.current,
        deltaY: event.deltaY * deltaScale,
        focalX: event.clientX - rect.left - scroller.clientLeft,
        focalY: event.clientY - rect.top - scroller.clientTop,
        scrollLeft: pending?.scrollLeft ?? scroller.scrollLeft,
        scrollTop: pending?.scrollTop ?? scroller.scrollTop,
        minZoom: gestureFloorRef.current,
      });
      if (next.zoom === zoomRef.current) return;
      pendingZoomScrollRef.current = next;
      viewportRef.current = {
        ...next,
        ...canvasOffsetRef.current,
        floor: gestureFloorRef.current,
      };
      zoomRef.current = next.zoom;
      setZoom(next.zoom);
    };
    scroller.addEventListener("wheel", pinchZoom, { passive: false });
    return () => scroller.removeEventListener("wheel", pinchZoom);
  }, [viewportRef]);

  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    commitViewport();
    scroller.addEventListener("scroll", commitViewport, { passive: true });
    return () => scroller.removeEventListener("scroll", commitViewport);
  }, [commitViewport]);

  useLayoutEffect(() => {
    const pending = pendingZoomScrollRef.current;
    const scroller = scrollRef.current;
    if (!pending || pending.zoom !== zoom || !scroller) return;
    scroller.scrollLeft = pending.scrollLeft;
    scroller.scrollTop = pending.scrollTop;
    pendingZoomScrollRef.current = null;
  }, [zoom, canvasOffset]);

  useLayoutEffect(() => {
    const restored = restoringViewportRef.current;
    const scroller = scrollRef.current;
    if (!restored || !scroller) return;
    scroller.scrollLeft = restored.scrollLeft;
    scroller.scrollTop = restored.scrollTop;
  }, []);

  // Read the viewport while the canvas is still mounted; a scroll event may never arrive in time.
  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    return () => {
      viewportRef.current = {
        ...canvasOffsetRef.current,
        zoom: zoomRef.current,
        floor: gestureFloorRef.current,
        scrollLeft: scroller.scrollLeft,
        scrollTop: scroller.scrollTop,
      };
    };
  }, [viewportRef]);

  const fitToView = useCallback(() => {
    const scroller = scrollRef.current;
    const canvas = canvasRef.current;
    if (!scroller || !canvas) return;
    const next = fitDagToViewport({
      nodes: [...canvas.querySelectorAll<HTMLElement>(".dag-node")].map((node) => ({
        left: node.offsetLeft,
        top: node.offsetTop,
        width: node.offsetWidth,
        height: node.offsetHeight,
      })),
      viewportWidth: scroller.clientWidth,
      viewportHeight: scroller.clientHeight,
    });
    if (!next) return;
    gestureFloorRef.current = Math.min(DAG_ZOOM_MIN, next.zoom);
    viewportRef.current = next;
    const offset = { offsetX: next.offsetX ?? 0, offsetY: next.offsetY ?? 0 };
    if (
      next.zoom === zoomRef.current &&
      offset.offsetX === canvasOffsetRef.current.offsetX &&
      offset.offsetY === canvasOffsetRef.current.offsetY
    ) {
      scroller.scrollLeft = next.scrollLeft;
      scroller.scrollTop = next.scrollTop;
      return;
    }
    // Scroll lands in the layout effect once the canvas has re-scaled.
    pendingZoomScrollRef.current = next;
    canvasOffsetRef.current = offset;
    setCanvasOffset(offset);
    zoomRef.current = next.zoom;
    setZoom(next.zoom);
  }, [viewportRef]);

  // Fit the columns on open. A relation focus and a remembered viewport
  // both outrank this: each already states where the human wants to look.
  useEffect(() => {
    if (!layoutReady || focusNodeId) return;
    const frameKey = `fit:${layoutMode}`;
    if (framedLayoutRef.current === frameKey) return;
    if (restoringViewportRef.current) {
      framedLayoutRef.current = frameKey;
      restoringViewportRef.current = null;
      return;
    }
    const timer = window.setTimeout(
      () => {
        framedLayoutRef.current = frameKey;
        fitToView();
      },
      layoutMode === "force" ? 550 : 80,
    );
    return () => window.clearTimeout(timer);
  }, [fitToView, focusNodeId, layoutMode, layoutReady]);

  useEffect(() => {
    if (!layoutReady || !focusNodeId) return;
    const frameKey = `${layoutMode}:${relationFocusNodeId ?? "initial"}`;
    if (framedLayoutRef.current === frameKey) return;
    if (restoringViewportRef.current) {
      framedLayoutRef.current = frameKey;
      restoringViewportRef.current = null;
      return;
    }
    const timer = window.setTimeout(
      () => {
        framedLayoutRef.current = frameKey;
        const scroller = scrollRef.current;
        const focusNode = [
          ...(canvasRef.current?.querySelectorAll<HTMLElement>(".dag-node") ?? []),
        ].find((node) => node.dataset.nodeId === focusNodeId);
        if (!scroller || !focusNode) return;
        const currentZoom = zoomRef.current;
        scroller.scrollLeft = Math.max(
          0,
          canvasOffsetRef.current.offsetX + focusNode.offsetLeft * currentZoom - 32,
        );
        scroller.scrollTop = Math.max(
          0,
          canvasOffsetRef.current.offsetY +
            focusNode.offsetTop * currentZoom -
            Math.max(32, (scroller.clientHeight - focusNode.offsetHeight * currentZoom) / 2),
        );
      },
      layoutMode === "force" ? 550 : 80,
    );
    return () => window.clearTimeout(timer);
  }, [focusNodeId, relationFocusNodeId, layoutMode, layoutReady]);

  const pointerDown = (event: ReactPointerEvent<HTMLDivElement>, nodeId: string) => {
    if (event.button !== 0) return;
    if ((event.target as Element).closest(".dag-connect-handle, input, select")) return;
    if (dragWatchdogRef.current !== null) window.clearTimeout(dragWatchdogRef.current);
    dragRef.current = {
      nodeId,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
      original: layout.positions[nodeId],
    };
  };

  const finishDrag = (pointerId: number, commit = true) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== pointerId) return;
    if (dragWatchdogRef.current !== null) window.clearTimeout(dragWatchdogRef.current);
    dragWatchdogRef.current = null;
    if (drag.moved) {
      layout.endDrag();
      if (editor.onNodeDragEnd(drag.nodeId, commit) && drag.original) {
        layout.pinAt(drag.nodeId, drag.original.x, drag.original.y);
        if (!drag.original.pinned) layout.releasePin(drag.nodeId);
      }
      suppressClickRef.current = drag.nodeId;
      window.requestAnimationFrame(() => {
        if (suppressClickRef.current === drag.nodeId) suppressClickRef.current = null;
      });
    }
    dragRef.current = null;
    setDraggingId(null);
  };

  const pointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 5)
      return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (!drag.moved) {
      drag.moved = true;
      event.currentTarget.setPointerCapture(event.pointerId);
      layout.beginDrag(drag.nodeId);
      setDraggingId(drag.nodeId);
    }
    const rect = canvas.getBoundingClientRect();
    layout.moveDrag(
      drag.nodeId,
      (event.clientX - rect.left) / zoomRef.current,
      (event.clientY - rect.top) / zoomRef.current,
    );
    editor.onNodeDragMove(drag.nodeId, event.clientX, event.clientY);
    if (dragWatchdogRef.current !== null) window.clearTimeout(dragWatchdogRef.current);
    dragWatchdogRef.current = window.setTimeout(() => finishDrag(drag.pointerId, false), 3000);
    event.preventDefault();
  };

  const pointerEnd = (event: ReactPointerEvent<HTMLDivElement>) => {
    finishDrag(event.pointerId, event.type === "pointerup");
  };

  const toggleFullscreen = async () => {
    const shell = shellRef.current;
    if (!shell || !document.fullscreenEnabled) return;
    try {
      if (document.fullscreenElement === shell) await document.exitFullscreen();
      else await shell.requestFullscreen();
    } catch {
      setIsFullscreen(document.fullscreenElement === shell);
    }
  };

  const allBright = lens.brightTypes.size === dagTypes.length;
  const allDim = lens.brightTypes.size === 0 && lens.litNodeIds.size === 0;
  const fullscreenSupported =
    typeof document !== "undefined" &&
    document.fullscreenEnabled &&
    "requestFullscreen" in HTMLElement.prototype;
  const repulsionLabel =
    repulsion < 650
      ? "Gentle"
      : repulsion < 1050
        ? "Balanced"
        : repulsion < 1500
          ? "Strong"
          : "Wide";
  return (
    <section className="view-panel dag-panel">
      {branchChanges && (
        <div className="branch-graph-controls">
          <span className="branch-graph-origin">
            {branchName(graphRefs, branchChanges.base_head.target)}@
            {branchChanges.base_head.revision}
            {" → "}
            {branchName(graphRefs, branchChanges.head.target)}@{branchChanges.head.revision}
          </span>
          <div className="segmented" role="group" aria-label="Branch graph lens">
            <button
              type="button"
              aria-pressed={expandedContext !== null}
              onClick={() => setExpandedContext(new Set())}
            >
              Changes + context
            </button>
            <button
              type="button"
              aria-pressed={expandedContext === null}
              onClick={() => setExpandedContext(null)}
            >
              Full graph
            </button>
          </div>
          {expandedContext !== null && (
            <button
              type="button"
              className="button compact secondary"
              onClick={() =>
                setExpandedContext(
                  expandBranchContext(graph, new Set(Object.keys(displayGraph.nodes))),
                )
              }
            >
              Expand context
            </button>
          )}
          <label>
            Task{" "}
            <select
              aria-label="Highlight task changes"
              value={selectedTaskId}
              onChange={(event) => setSelectedTaskId(event.target.value)}
            >
              <option value="">All changes</option>
              {changeTasks.map((source) => (
                <option key={source.task_id} value={source.task_id!}>
                  {`${humanize(source.producer)} · revision ${source.revision}`}
                </option>
              ))}
            </select>
          </label>
          {selectedTaskId && onInspectTask && (
            <button
              type="button"
              className="button compact secondary"
              onClick={() => onInspectTask(selectedTaskId)}
            >
              View task
            </button>
          )}
          <div className="branch-change-legend" aria-label="Change legend">
            {BRANCH_DIFF_LEGEND.map((word) => (
              <span key={word} className={`branch-diff-word diff-${word}`}>
                {BRANCH_DIFF_WORDS[word]}
              </span>
            ))}
          </div>
        </div>
      )}
      {relationFocusNodeId && graph.nodes[relationFocusNodeId] && (
        <div className="dag-relation-focus" role="status">
          <Focus size={16} />
          <span>
            <strong>{graph.nodes[relationFocusNodeId].title}</strong>
          </span>
          <button className="button compact secondary" onClick={onClearRelationFocus}>
            <X size={14} /> Clear focus
          </button>
        </div>
      )}
      {branchChanges && branchChanges.edges.length > 0 && (
        <details className="branch-edge-changes">
          <summary>{branchChanges.edges.length} relation changes</summary>
          {branchChanges.edges.map((change) => {
            const edge = change.after ?? change.before!;
            return (
              <article key={change.edge_id}>
                <strong>
                  <span className={`branch-change-badge ${change.change}`}>{change.change}</span>{" "}
                  {displayGraph.nodes[edge.source]?.title ?? edge.source} →{" "}
                  {displayGraph.nodes[edge.target]?.title ?? edge.target}
                </strong>
                <p>{edge.relation.replaceAll("_", " ")}</p>
                <ChangedFields before={change.before} after={change.after} />
                <ChangeHistory history={change.history} onInspectTask={onInspectTask} />
              </article>
            );
          })}
        </details>
      )}
      <div className="dag-shell" ref={shellRef} tabIndex={-1} onKeyDown={editor.onKeyDown}>
        <div className="dag-toolbar" ref={toolbarRef} data-canvas-ui>
          {editor.controls}
          <div className="dag-toolbar-group">
            <button
              type="button"
              aria-label="Layout"
              aria-expanded={toolbarMenu === "layout"}
              onClick={() => setToolbarMenu((open) => (open === "layout" ? null : "layout"))}
            >
              <GitBranch size={15} />
              <span>Layout</span>
              <ChevronDown size={13} />
            </button>
            <button
              type="button"
              aria-label="Lens"
              aria-expanded={toolbarMenu === "view"}
              onClick={() => setToolbarMenu((open) => (open === "view" ? null : "view"))}
            >
              <Eye size={15} />
              <span>Lens</span>
              <ChevronDown size={13} />
            </button>
          </div>
          <div className="dag-toolbar-group">
            <button type="button" aria-label="Fit" title="Fit" onClick={fitToView}>
              <Scan size={15} />
            </button>
            {fullscreenSupported && (
              <button
                aria-label={isFullscreen ? "Exit DAG full screen" : "Enter DAG full screen"}
                aria-pressed={isFullscreen}
                title={isFullscreen ? "Exit full screen" : "Full screen"}
                type="button"
                onClick={toggleFullscreen}
              >
                {isFullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
              </button>
            )}
          </div>
          {toolbarMenu === "layout" && (
            <div className="dag-toolbar-menu" role="group" aria-label="Layout options">
              <div className="segmented" role="group" aria-label="DAG layout">
                <button
                  type="button"
                  aria-pressed={layoutMode === "force"}
                  onClick={() => setLayoutMode("force")}
                >
                  <Orbit size={14} /> Force-directed
                </button>
                <button
                  type="button"
                  aria-pressed={layoutMode === "flow"}
                  onClick={() => setLayoutMode("flow")}
                >
                  <Workflow size={14} /> Research flow
                </button>
              </div>
              {layoutMode === "force" && (
                <label className="dag-force-control">
                  <span>
                    <Gauge size={14} /> Repulsion
                  </span>
                  <input
                    aria-label="Node repulsion"
                    type="range"
                    min="350"
                    max="1900"
                    step="50"
                    value={repulsion}
                    onChange={(event) => setRepulsion(Number(event.target.value))}
                  />
                  <output>{repulsionLabel}</output>
                </label>
              )}
              <div className="dag-toolbar-menu-row">
                <button
                  className="dag-tool-button"
                  type="button"
                  disabled={layout.pinCount === 0}
                  onClick={layout.releasePins}
                >
                  <PinOff size={14} /> Release all pins
                </button>
                <button
                  className="dag-tool-button"
                  type="button"
                  onClick={() => {
                    framedLayoutRef.current = null;
                    layout.resetLayout();
                  }}
                >
                  <RotateCcw size={14} /> Reset layout
                </button>
              </div>
            </div>
          )}
          {toolbarMenu === "view" && (
            <div className="dag-toolbar-menu" role="group" aria-label="Lens options">
              <div
                className="segmented dag-projection-switch"
                role="group"
                aria-label="DAG ontology projection"
              >
                {(["all", "belief", "action"] as const).map((item) => (
                  <button
                    type="button"
                    aria-pressed={ontologyProjection === item}
                    onClick={() => setOntologyProjection(item)}
                    key={item}
                  >
                    {item[0].toUpperCase() + item.slice(1)}
                  </button>
                ))}
              </div>
              <div className="dag-lenses" role="group" aria-label="Node brightness by type">
                {dagTypes.map((type) => {
                  const active = lens.brightTypes.has(type);
                  const hidden = lens.hiddenTypes.has(type);
                  const meta = dagTypeMeta[type];
                  return (
                    <div className="dag-lens-row" key={type}>
                      <button
                        className={`dag-lens ${active ? "is-bright" : "is-dim"}`}
                        style={{ "--lens-color": meta.color } as CSSProperties}
                        type="button"
                        aria-pressed={active}
                        aria-label={`${active ? "Dim" : "Brighten"} ${meta.label.toLowerCase()} (${typeCounts[type]})`}
                        disabled={hidden}
                        onClick={() => updateLens({ brightTypes: toggled(lens.brightTypes, type) })}
                      >
                        {active ? <Lightbulb size={12} /> : <LightbulbOff size={12} />}
                        <span>{meta.label}</span>
                        <small>{typeCounts[type]}</small>
                      </button>
                      <button
                        className="dag-lens-hide"
                        type="button"
                        aria-pressed={hidden}
                        aria-label={`${hidden ? "Show" : "Hide"} ${meta.label.toLowerCase()}`}
                        onClick={() => updateLens({ hiddenTypes: toggled(lens.hiddenTypes, type) })}
                      >
                        {hidden ? <EyeOff size={12} /> : <Eye size={12} />}
                      </button>
                    </div>
                  );
                })}
              </div>
              <div className="dag-toolbar-menu-row">
                <button
                  className="dag-tool-button"
                  type="button"
                  disabled={allBright}
                  onClick={() => updateLens({ brightTypes: new Set(dagTypes) })}
                >
                  <Lightbulb size={14} /> Brighten all
                </button>
                <button
                  className="dag-tool-button"
                  type="button"
                  disabled={allDim}
                  onClick={() => updateLens({ brightTypes: new Set(), litNodeIds: new Set() })}
                >
                  <LightbulbOff size={14} /> Dim all
                </button>
              </div>
            </div>
          )}
        </div>
        <div
          className="dag-scroll"
          ref={scrollRef}
          onPointerDown={editor.onCanvasPointerDown}
          onDoubleClick={editor.onCanvasDoubleClick}
          onMouseDown={(event) => {
            if (!(event.target as Element).closest("input, select, textarea")) {
              event.preventDefault();
              ((event.target as Element).closest<HTMLElement>("button") ?? shellRef.current)?.focus(
                { preventScroll: true },
              );
            }
          }}
        >
          <div
            className="dag-zoom-plane"
            style={{
              // Trailing room lets the browser center nodes near either canvas edge.
              width: `calc(${layout.width * zoom + canvasOffset.offsetX}px + 50%)`,
              height: `calc(${layout.height * zoom + canvasOffset.offsetY}px + 50%)`,
            }}
          >
            <div
              className="dag-canvas"
              ref={canvasRef}
              style={
                {
                  "--dag-canvas-zoom": zoom,
                  width: layout.width,
                  height: layout.height,
                  left: canvasOffset.offsetX,
                  top: canvasOffset.offsetY,
                  transform: `scale(${zoom})`,
                } as CSSProperties
              }
            >
              <svg width={layout.width} height={layout.height} aria-hidden="true">
                <defs>
                  <marker
                    id="dag-arrow"
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 10 5 L 0 10 z" />
                  </marker>
                </defs>
                {projection.edges.map((edge, edgeIndex) => {
                  const source = layout.positions[edge.source];
                  const target = layout.positions[edge.target];
                  const sourceNode = displayGraph.nodes[edge.source];
                  const targetNode = displayGraph.nodes[edge.target];
                  if (!source || !target || !lensShows(lens, sourceNode)) return null;
                  if (!lensShows(lens, targetNode)) return null;
                  const geometry = edgeGeometry(source, target, edgeIndex);
                  const projectionEmphasis = edgeProjectionEmphasis(edge, ontologyProjection);
                  const dimmed =
                    projectionEmphasis === "dimmed" ||
                    !lensBrightEdge(lens, sourceNode, targetNode) ||
                    Boolean(focusedRelations && !focusedRelations.edgeIds.has(edge.id)) ||
                    Boolean(
                      selectedTaskId &&
                      !edgeChanges
                        .get(edge.id)
                        ?.history.some((source) => source.task_id === selectedTaskId),
                    );
                  return (
                    <g
                      className={`dag-edge-group ${edgeChanges.has(edge.id) ? `branch-${edgeChanges.get(edge.id)!.change}` : ""} ${dimmed ? "is-dim" : ""} ${projectionEmphasis === "neutral" ? "is-neutral" : ""}`}
                      key={edge.id}
                    >
                      <path className="dag-edge" d={geometry.path} markerEnd="url(#dag-arrow)" />
                      {canEdit &&
                        (editing.canonicalGraph.edges[edge.id] ||
                          editing.humanDraft?.added_edges.some(
                            (added) => added.id === edge.id,
                          )) && (
                          <path
                            className="dag-edge-hit"
                            data-edge-id={edge.id}
                            d={geometry.path}
                            onClick={(event) => editor.onEdgeClick(event, edge)}
                          />
                        )}
                      <text className="dag-edge-label" x={geometry.labelX} y={geometry.labelY}>
                        {edgeChanges.has(edge.id) ? `${edgeChanges.get(edge.id)!.change} · ` : ""}
                        {edge.relation.replaceAll("_", " ")}
                      </text>
                    </g>
                  );
                })}
              </svg>
              {projection.nodes.map((node) => {
                const position = layout.positions[node.id];
                if (!position || !lensShows(lens, node)) return null;
                const lit = lens.litNodeIds.has(node.id);
                const projectionEmphasis =
                  nodeProjectionEmphasis.get(node.id) ??
                  (ontologyProjection === "all" ? "emphasized" : "neutral");
                const dimmed =
                  projectionEmphasis === "dimmed" ||
                  !lensBrightNode(lens, node) ||
                  Boolean(focusedRelations && !focusedRelations.nodeIds.has(node.id)) ||
                  Boolean(selectedTaskId && !taskHighlights.has(node.id));
                const nodeChange = nodeChanges.get(node.id);
                const diffWord = nodeChange
                  ? branchDiffWord(nodeChange.change, mergeMarks.get(`node:${node.id}`))
                  : null;
                return (
                  <div
                    className={`dag-node ${canEdit && editing.humanDraft?.custom_nodes[node.id] && node.origin === "unknown" ? "needs-origin" : ""} ${flaggedNodeIds.has(node.id) ? "has-evidence-flag" : ""} ${editor.nodeClass(node.id)} ${branchChanges ? "is-branch-diff" : ""} ${diffWord ? `diff-${diffWord}` : ""} ${node.type} ${node.standing} ${node.draft_touched ? "draft-touched" : ""} ${dimmed ? "is-dim" : ""} ${projectionEmphasis === "neutral" ? "is-layer-neutral" : ""} ${position.pinned ? "is-pinned" : ""} ${draggingId === node.id ? "is-dragging" : ""} ${changedNodeIds.has(node.id) ? "digest-changed" : ""}`}
                    data-node-id={node.id}
                    style={
                      {
                        left: position.x - DAG_NODE_WIDTH / 2,
                        top: position.y - DAG_NODE_HEIGHT / 2,
                        "--node-accent": dagTypeMeta[node.type].color,
                      } as CSSProperties
                    }
                    key={node.id}
                    onDragStart={(event) => event.preventDefault()}
                    onPointerDown={(event) => pointerDown(event, node.id)}
                    onPointerMove={pointerMove}
                    onPointerUp={pointerEnd}
                    onPointerCancel={pointerEnd}
                    onLostPointerCapture={pointerEnd}
                  >
                    <button
                      aria-label={`${node.title}. Inspect node. Drag this card to pin it.`}
                      className="dag-node-inspect"
                      type="button"
                      onDoubleClick={(event) => {
                        event.stopPropagation();
                        editor.onNodeDoubleClick(node);
                      }}
                      onClick={(event) => {
                        if (suppressClickRef.current === node.id) {
                          suppressClickRef.current = null;
                          return;
                        }
                        if (!editor.onNodeClick(event, node)) onSelectNode(node);
                      }}
                    >
                      {!branchChanges && <span className="eyebrow">{nodeTypeLabel(node)}</span>}
                      <strong>{node.title}</strong>
                      {branchChanges && (
                        <span className={`branch-diff-word diff-${diffWord ?? "context"}`}>
                          {diffWord ? BRANCH_DIFF_WORDS[diffWord] : "Context"}
                        </span>
                      )}
                      <small className="dag-node-standing">
                        <span className={`standing ${node.standing}`}>{node.standing}</span>
                        <span>{node.status || node.validity || ""}</span>
                      </small>
                    </button>
                    <button
                      className={`dag-node-light ${lit ? "is-lit" : ""}`}
                      type="button"
                      aria-pressed={lit}
                      aria-label={`${lit ? "Dim" : "Light up"} ${node.title}`}
                      onPointerDown={(event) => event.stopPropagation()}
                      onClick={(event) => {
                        event.stopPropagation();
                        updateLens({ litNodeIds: toggled(lens.litNodeIds, node.id) });
                      }}
                    >
                      {lit ? <Lightbulb size={13} /> : <LightbulbOff size={13} />}
                    </button>
                    {editor.renameField(node)}
                    {canEdit &&
                      editing.humanDraft?.custom_nodes[node.id] &&
                      node.origin === "unknown" && (
                        <button
                          className="dag-origin-action"
                          type="button"
                          aria-label="Edit node origin"
                          onPointerDown={(event) => event.stopPropagation()}
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelectNode(node);
                          }}
                        >
                          <Pencil size={14} />
                        </button>
                      )}
                    {canEdit && graph.nodes[node.id] && (
                      <button
                        onPointerDown={(event) => editor.onHandlePointerDown(event, node.id)}
                        onClick={(event) => editor.onHandleClick(event, node.id)}
                        className="dag-connect-handle"
                        type="button"
                        aria-label={`Connect from ${node.title}`}
                        title="Drag to another node, or click to choose a connection"
                      >
                        <Link2 size={14} />
                      </button>
                    )}
                    {position.pinned && (
                      <button
                        aria-label={`Release pin from ${node.title}`}
                        className="dag-pin-state"
                        title="Release this pin"
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          layout.releasePin(node.id);
                        }}
                        onPointerDown={(event) => event.stopPropagation()}
                      >
                        <Pin size={12} /> pinned
                      </button>
                    )}
                  </div>
                );
              })}
              {editor.overlays}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

interface ExecutionProps {
  graphTarget?: GraphTargetRef;
  /** The project's refs, so every Run row names its branch. */
  graphRefs?: GraphRef[] | null;
  graph: GraphState;
  episodes: Episode[];
  episodeAction: string | null;
  tasks: AgentTask[];
  watchers: WatcherRecord[];
  experimentControl: Record<string, ExperimentControlState>;
  experimentEntries?: ExperimentLoopIndexEntry[];
  experimentUnavailableLoops?: UnavailableExperimentLoop[];
  experimentEntriesLoaded?: boolean;
  exactExperimentRoute?: ExperimentRouteIdentity | null;
  exactExperimentEntry?: ExperimentLoopIndexEntry | null;
  selectedExperimentId: string | null;
  focusExperimentId: string | null;
  selectedAutoResearchEpisodeId?: string | null;
  runBusy: boolean;
  stopBusyIds: ReadonlySet<string>;
  watcherCheckBusyId: string | null;
  taskActionId: string | null;
  selectedExperimentConversation?: ReactNode;
  providerLabels?: Record<string, string>;
  /** Offers the Experiment agent picker on each Run row. */
  project?: ProjectSnapshot;
  providerLogins?: ProviderLoginState[];
  viewerUserId?: string | null;
  onOpenSignIns?: (state: ProviderLoginState) => void;
  machines?: Machine[];
  computeApiBase?: string;
  onOpenSettings?: () => void;
  mutationsDisabled?: boolean;
  experimentStartsDisabled?: boolean;
  onInspectTask: (operationId: string) => void;
  onStopEpisode: (episodeId: string) => Promise<void>;
  onArchiveEpisode: ArchiveEpisodeAction;
  onMergeEpisode: (episodeId: string, body: MergeEpisodeBody) => Promise<void>;
  onContinueEpisode: (episodeId: string, invocationCeiling: number) => Promise<void>;
  onSendEpisodeMessage: (
    episodeId: string,
    body: string,
    invocationCeiling?: number,
    requestId?: string,
  ) => Promise<void>;
  onOperateEpisodeTask: (task: AgentTask, action: "pause" | "resume" | "retry") => Promise<void>;
  onSwitchEpisodeProvider: (task: AgentTask) => void;
  onSelectExperiment: (nodeId: string | null, route?: ExperimentRouteIdentity) => void;
  onOpenExperimentEntry: (entry: ExperimentLoopIndexEntry) => void;
  onDetailFocused: () => void;
  onOpenHistory: () => void;
  onRunExperiment: (
    node: GraphNode,
    invocationCeiling?: number,
    browserRequested?: boolean,
    launchConfig?: AgentRunConfig,
    message?: string,
  ) => void;
  onStopExperiment: (nodeId: string, episodeId: string) => void;
  onCheckExperimentWatcher: (watcherId: string) => void;
  onStopExperimentWatcher: (watcherId: string) => void;
  onRecoverExperiment: (task: AgentTask, action: "resume" | "retry") => void;
  onSwitchExperimentProvider: (task: AgentTask) => void;
  episodeReportHref: (episodeId: string) => string;
}

export function ExecutionView({
  graphTarget = MAIN_GRAPH,
  graphRefs = null,
  graph,
  episodes,
  episodeAction,
  tasks,
  watchers,
  experimentControl,
  experimentEntries = [],
  experimentUnavailableLoops = [],
  experimentEntriesLoaded = true,
  exactExperimentRoute = null,
  exactExperimentEntry = null,
  selectedExperimentId,
  focusExperimentId,
  selectedAutoResearchEpisodeId: requestedAutoResearchEpisodeId = null,
  runBusy,
  stopBusyIds,
  watcherCheckBusyId,
  taskActionId,
  selectedExperimentConversation,
  providerLabels = {},
  project,
  providerLogins = [],
  viewerUserId = null,
  onOpenSignIns,
  machines = [],
  computeApiBase = "",
  onOpenSettings,
  mutationsDisabled = false,
  experimentStartsDisabled = false,
  onInspectTask,
  onStopEpisode,
  onArchiveEpisode,
  onMergeEpisode,
  onContinueEpisode,
  onSendEpisodeMessage,
  onOperateEpisodeTask,
  onSwitchEpisodeProvider,
  onSelectExperiment,
  onOpenExperimentEntry,
  onDetailFocused,
  onOpenHistory,
  onRunExperiment,
  onStopExperiment,
  onCheckExperimentWatcher,
  onStopExperimentWatcher,
  onRecoverExperiment,
  onSwitchExperimentProvider,
  episodeReportHref,
}: ExecutionProps) {
  const [showArchived, setShowArchived] = useState(false);
  const selectedDetailRef = useRef<HTMLDivElement>(null);
  const selectedAutoResearchDetailRef = useRef<HTMLDivElement>(null);
  const focusedAutoResearchEpisodeId = useRef<string | null>(null);
  const staleMainRoute =
    exactExperimentRoute?.graph_target.kind === "main" &&
    (graphTarget.kind === "main" || experimentEntriesLoaded) &&
    !mainExperimentRouteMatchesControl(
      exactExperimentRoute,
      graphTarget.kind === "main"
        ? experimentControl[exactExperimentRoute.experiment_id]
        : experimentEntries.find(
            (entry) => entry.episode.episode_id === exactExperimentRoute.episode_id,
          )?.control,
    )
      ? exactExperimentRoute
      : null;
  const experimentNodes = new Map(
    Object.values(graph.nodes)
      .filter((node) => isControlNode(node.type))
      .map((node) => [node.id, node]),
  );
  const experimentRuns = new Map<string, ExperimentRun>();
  const experimentEntriesByEpisode = new Map<string, ExperimentLoopIndexEntry>();
  experimentNodes.forEach((node, nodeId) => {
    const control = experimentControl[nodeId];
    if (!control) {
      throw new Error(`Experiment ${nodeId} is missing its backend control projection.`);
    }
    if (!control.health || !control.recommendation || !control.run_section) {
      throw new Error(`Experiment ${nodeId} has an incomplete backend control projection.`);
    }
    if (!control?.episode_id) return;
    experimentRuns.set(control.episode_id, buildExperimentRun(node, control, tasks, watchers));
  });
  const indexedEntries = currentExperimentEntries(
    experimentEntries,
    experimentControl,
    exactExperimentEntry,
    graphTarget,
  );
  indexedEntries.forEach((entry) => {
    if (
      entry.control.episode_id !== entry.episode.episode_id ||
      entry.control.episode?.episode_id !== entry.episode.episode_id
    ) {
      throw new Error(
        `Experiment ${entry.node.id} index entry does not identify one exact episode.`,
      );
    }
    experimentEntriesByEpisode.set(entry.episode.episode_id, entry);
    if (entry.graph_target.kind === "main" && experimentRuns.has(entry.episode.episode_id)) {
      return;
    }
    experimentRuns.set(
      entry.episode.episode_id,
      buildExperimentRun(entry.node, entry.control, entry.episode.tasks, watchers),
    );
  });
  // The cached graph control owns lifecycle state. The episode reads own the
  // current archive preference, including for main cards built from that cache.
  const episodesById = new Map(episodes.map((episode) => [episode.episode_id, episode]));
  indexedEntries.forEach((entry) => episodesById.set(entry.episode.episode_id, entry.episode));
  const selectedExperimentEpisodeId = selectedExperimentId
    ? (exactExperimentRoute?.episode_id ??
      experimentControl[selectedExperimentId]?.episode_id ??
      null)
    : null;
  const orderedEpisodes = runsEpisodeCards(
    [...episodesById.values()],
    new Set(experimentRuns.keys()),
    showArchived,
    selectedExperimentEpisodeId,
  );
  const selectedAutoResearchEpisodeId =
    orderedEpisodes.find(
      (episode) =>
        episode.mode === "auto_research" &&
        (episode.episode_id === requestedAutoResearchEpisodeId ||
          episode.chain.some((member) => member.episode_id === requestedAutoResearchEpisodeId)),
    )?.episode_id ?? requestedAutoResearchEpisodeId;
  const visibleEpisodes = orderedEpisodes.filter((episode) => !episode.archived);
  const archivedEpisodes = orderedEpisodes.filter((episode) => episode.archived);
  const requestedEpisodeId =
    selectedAutoResearchEpisodeId ?? exactExperimentRoute?.episode_id ?? null;
  const requestedEpisode = requestedEpisodeId ? episodesById.get(requestedEpisodeId) : undefined;
  const requestedEpisodeArchived = requestedEpisode?.archived ?? false;
  const childExperimentsByParent = new Map<string, ExperimentLoopIndexEntry[]>();
  experimentEntriesByEpisode.forEach((entry) => {
    if (!entry.parent_episode_id) return;
    if (!showArchived && episodesById.get(entry.episode.episode_id)?.archived) return;
    const children = childExperimentsByParent.get(entry.parent_episode_id) ?? [];
    children.push(entry);
    childExperimentsByParent.set(entry.parent_episode_id, children);
  });
  childExperimentsByParent.forEach((children) => {
    children.sort(
      (left, right) =>
        Date.parse(right.episode.created_at) - Date.parse(left.episode.created_at) ||
        left.episode.episode_id.localeCompare(right.episode.episode_id),
    );
  });
  const selectedAutoResearchLoaded = orderedEpisodes.some(
    (episode) => episode.episode_id === selectedAutoResearchEpisodeId,
  );
  const needsAction = visibleEpisodes.filter(
    (episode) => episodeRunSection(episode) === "actionable",
  );
  const inProgress = visibleEpisodes.filter((episode) => episodeRunSection(episode) === "running");
  const completed = visibleEpisodes.filter((episode) => episodeRunSection(episode) === "completed");
  // Cards start folded; only the episode a route names opens.
  const expandedEpisodeId = selectedAutoResearchEpisodeId;
  const completedGroups = [
    {
      mode: "experiment_loop" as const,
      title: "Experiment loop",
      episodes: completed.filter((episode) => episode.mode === "experiment_loop"),
    },
    {
      mode: "auto_research" as const,
      title: "Auto-research",
      episodes: completed.filter((episode) => episode.mode === "auto_research"),
    },
  ];

  useEffect(() => {
    if (requestedEpisodeArchived) setShowArchived(true);
  }, [requestedEpisodeId, requestedEpisodeArchived]);

  useEffect(() => {
    if (
      staleMainRoute ||
      !experimentEntriesLoaded ||
      !focusExperimentId ||
      focusExperimentId !== selectedExperimentId
    ) {
      return;
    }
    selectedDetailRef.current?.focus();
    onDetailFocused();
  }, [
    staleMainRoute,
    experimentEntriesLoaded,
    focusExperimentId,
    onDetailFocused,
    selectedExperimentId,
    selectedExperimentEpisodeId,
  ]);

  useEffect(() => {
    if (!selectedAutoResearchEpisodeId) {
      focusedAutoResearchEpisodeId.current = null;
      return;
    }
    if (
      !selectedAutoResearchLoaded ||
      focusedAutoResearchEpisodeId.current === selectedAutoResearchEpisodeId
    ) {
      return;
    }
    const detail = selectedAutoResearchDetailRef.current;
    if (!detail) return;
    focusRunDetail(detail);
    focusedAutoResearchEpisodeId.current = selectedAutoResearchEpisodeId;
  }, [selectedAutoResearchEpisodeId, selectedAutoResearchLoaded]);

  return (
    <section className="view-panel runs-view" aria-label="Runs">
      <ProviderLoginNotice
        states={providerLogins}
        viewerUserId={viewerUserId}
        onOpenSignIns={onOpenSignIns}
      />
      {onOpenSettings && (
        <ComputeRouteNotice
          // One instance per project, so a check's result or error never
          // outlives the project it was made for.
          key={computeApiBase}
          apiBase={computeApiBase}
          machines={machines}
          onOpenSettings={onOpenSettings}
        />
      )}
      <div className="runs-view-controls">
        <label className="show-archived-runs">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(event) => setShowArchived(event.target.checked)}
          />
          Show archived
        </label>
      </div>
      {experimentUnavailableLoops.length > 0 && (
        <div className="experiment-board-unavailable" role="status">
          <span>Some live loops couldn't be read:</span>
          {unavailableExperimentTargets(experimentUnavailableLoops).map((target) => (
            <BranchTag
              key={target.kind === "main" ? "main" : target.branch_id}
              refs={graphRefs}
              target={target}
            />
          ))}
        </div>
      )}
      {staleMainRoute ? (
        <div className="run-route-history" role="status">
          <strong>The requested Experiment episode is now in History.</strong>
          <button className="button secondary compact" type="button" onClick={onOpenHistory}>
            Open History
          </button>
          {showArchived && requestedEpisode?.archived && renderEpisodeCard(requestedEpisode, false)}
        </div>
      ) : (
        <div
          className="operating-sections episode-ledger-sections"
          aria-busy={experimentEntriesLoaded ? undefined : true}
        >
          <section className="operating-section episode-ledger-section needs-action">
            <header>
              <h2>Needs action</h2>
              <span>{needsAction.length}</span>
            </header>
            {needsAction.length > 0 && (
              <div className="campaign-run-list">
                {needsAction.map((episode) =>
                  renderEpisodeCard(episode, episode.episode_id === expandedEpisodeId),
                )}
              </div>
            )}
          </section>
          <section className="operating-section episode-ledger-section in-progress">
            <header>
              <h2>In progress</h2>
              <span>{inProgress.length}</span>
            </header>
            {inProgress.length > 0 && (
              <div className="campaign-run-list">
                {inProgress.map((episode) =>
                  renderEpisodeCard(episode, episode.episode_id === expandedEpisodeId),
                )}
              </div>
            )}
          </section>
          <section className="operating-section episode-ledger-section completed">
            <header>
              <h2>Completed</h2>
              <span>{completed.length}</span>
            </header>
            <div className="episode-type-groups">
              {completedGroups.map((group) => (
                <details
                  className="episode-type-group"
                  open={
                    group.episodes.some((episode) => episode.episode_id === requestedEpisodeId) ||
                    undefined
                  }
                  key={`${group.mode}:${requestedEpisodeId ?? ""}`}
                >
                  <summary>
                    <strong>{group.title}</strong>
                    <span>{group.episodes.length}</span>
                  </summary>
                  <div className="campaign-run-list">
                    {group.episodes.map((episode) =>
                      renderEpisodeCard(
                        episode,
                        episode.episode_id === selectedAutoResearchEpisodeId,
                      ),
                    )}
                  </div>
                </details>
              ))}
            </div>
          </section>
          {showArchived && (
            <section
              className="operating-section episode-ledger-section archived"
              aria-label="Archived runs"
            >
              <header>
                <h2>Archived</h2>
                <span>{archivedEpisodes.length}</span>
              </header>
              {archivedEpisodes.length > 0 && (
                <div className="campaign-run-list">
                  {archivedEpisodes.map((episode) => renderEpisodeCard(episode, false))}
                </div>
              )}
            </section>
          )}
        </div>
      )}
    </section>
  );

  function renderEpisodeCard(episode: Episode, initiallyExpanded: boolean) {
    if (episode.mode === "auto_research") {
      return (
        <AutoResearchEpisodeCard
          episode={episode}
          graphRefs={graphRefs}
          initiallyExpanded={initiallyExpanded}
          selected={episode.episode_id === selectedAutoResearchEpisodeId}
          detailRef={
            episode.episode_id === selectedAutoResearchEpisodeId
              ? selectedAutoResearchDetailRef
              : undefined
          }
          busyAction={episodeAction}
          taskActionId={taskActionId}
          // A chain is one card, so children of every member are listed on it.
          childExperiments={episode.chain.flatMap(
            (member) => childExperimentsByParent.get(member.episode_id) ?? [],
          )}
          onOpenExperimentEntry={(entry) => {
            // Restore selection even when reopening this exact URL emits no hashchange.
            onSelectExperiment(entry.node.id, {
              experiment_id: entry.node.id,
              episode_id: entry.episode.episode_id,
              graph_target: entry.graph_target,
              parent_episode_id: entry.parent_episode_id,
            });
            onOpenExperimentEntry(entry);
          }}
          onInspectTask={onInspectTask}
          onStop={onStopEpisode}
          onMerge={onMergeEpisode}
          onContinue={onContinueEpisode}
          onSendMessage={onSendEpisodeMessage}
          onOperateTask={onOperateEpisodeTask}
          onSwitchProvider={onSwitchEpisodeProvider}
          onArchive={onArchiveEpisode}
          key={episode.episode_id}
        />
      );
    }
    const run = experimentRuns.get(episode.episode_id);
    if (!run && episode.archived) {
      const historicalEntry = experimentEntries.find(
        (entry) => entry.episode.episode_id === episode.episode_id,
      );
      const title =
        historicalEntry?.node.title ??
        (episode.graph_target.kind === "main"
          ? graph.nodes[episode.control_node_id ?? ""]?.title
          : undefined) ??
        episode.control_node_id ??
        "Experiment loop";
      return (
        <article
          className="campaign-run archived-episode-card"
          data-episode-id={episode.episode_id}
          key={episode.episode_id}
        >
          <div className="campaign-run-heading">
            <span className="campaign-run-identity">
              <strong className="campaign-run-title">
                <FlaskConical size={14} aria-hidden="true" />
                <span>{title}</span>
              </strong>
              <span className="campaign-run-meta">
                <time dateTime={episode.created_at}>
                  {formatEpisodeTimestamp(episode.created_at)}
                </time>
                <BranchTag refs={graphRefs} target={episode.graph_target} />
                <ExperimentLoopMetadata
                  projectId={episode.project_id}
                  graphTarget={episode.graph_target}
                  metadata={episode}
                  author={episode.authorized_by}
                />
                <EpisodeAuthor author={episode.authorized_by} />
              </span>
            </span>
            <div className="archived-episode-actions">
              <button className="button secondary compact" type="button" onClick={onOpenHistory}>
                Open History
              </button>
              <EpisodeArchiveButton
                episode={episode}
                disabled={episodeAction !== null}
                onArchive={onArchiveEpisode}
              />
            </div>
          </div>
        </article>
      );
    }
    if (!run) {
      throw new Error(
        `Experiment episode ${episode.episode_id} is missing its backend control projection.`,
      );
    }
    const indexedEntry = experimentEntriesByEpisode.get(episode.episode_id) ?? null;
    const watchedByParentAutoResearch = indexedEntry?.parent_watching ?? false;
    return (
      <ExperimentEpisodeCard
        episode={episode}
        graphRefs={graphRefs}
        run={run}
        initiallyExpanded={
          !staleMainRoute &&
          (initiallyExpanded || selectedExperimentEpisodeId === episode.episode_id)
        }
        selected={!staleMainRoute && selectedExperimentEpisodeId === episode.episode_id}
        detailRef={
          !staleMainRoute && selectedExperimentEpisodeId === episode.episode_id
            ? selectedDetailRef
            : undefined
        }
        runBusy={runBusy}
        stopBusy={stopBusyIds.has(episode.episode_id)}
        watcherCheckBusyId={watcherCheckBusyId}
        taskActionId={taskActionId}
        archiveDisabled={episodeAction !== null}
        onArchive={onArchiveEpisode}
        mergeBusy={episodeAction === `merge:${episode.episode_id}`}
        onMerge={onMergeEpisode}
        experimentConversation={
          selectedExperimentEpisodeId === episode.episode_id
            ? selectedExperimentConversation
            : undefined
        }
        indexedEntry={indexedEntry}
        watchedByParentAutoResearch={watchedByParentAutoResearch}
        exactBranchEntry={indexedEntry?.graph_target.kind === "branch" ? indexedEntry : null}
        providerLabels={providerLabels}
        experimentStartsDisabled={experimentStartsDisabled}
        mutationsDisabled={
          mutationsDisabled ||
          runBusy ||
          stopBusyIds.has(episode.episode_id) ||
          Boolean(taskActionId) ||
          Boolean(watcherCheckBusyId)
        }
        onInspectTask={onInspectTask}
        onContinueEpisode={onContinueEpisode}
        onSelectExperiment={onSelectExperiment}
        project={project}
        onRunExperiment={onRunExperiment}
        onStopExperiment={onStopExperiment}
        onCheckExperimentWatcher={onCheckExperimentWatcher}
        onStopExperimentWatcher={onStopExperimentWatcher}
        onRecoverExperiment={onRecoverExperiment}
        onSwitchExperimentProvider={onSwitchExperimentProvider}
        episodeReportHref={episodeReportHref}
        key={episode.episode_id}
      />
    );
  }

  function episodeRunSection(episode: Episode): EpisodeRunSection {
    if (episode.mode === "auto_research") return episode.run_section;
    const run = experimentRuns.get(episode.episode_id);
    if (!run) {
      throw new Error(
        `Experiment episode ${episode.episode_id} is missing its backend control projection.`,
      );
    }
    return run.control.run_section;
  }
}

function ExperimentEpisodeCard({
  episode,
  graphRefs,
  run,
  initiallyExpanded,
  selected,
  detailRef,
  runBusy,
  stopBusy,
  watcherCheckBusyId,
  taskActionId,
  archiveDisabled,
  onArchive,
  mergeBusy,
  onMerge,
  experimentConversation,
  indexedEntry,
  watchedByParentAutoResearch,
  exactBranchEntry,
  providerLabels,
  experimentStartsDisabled,
  mutationsDisabled,
  project,
  onSelectExperiment,
  onInspectTask,
  onRunExperiment,
  onStopExperiment,
  onCheckExperimentWatcher,
  onStopExperimentWatcher,
  onRecoverExperiment,
  onSwitchExperimentProvider,
  onContinueEpisode,
  episodeReportHref,
}: {
  episode: Episode;
  graphRefs: GraphRef[] | null;
  run: ExperimentRun;
  initiallyExpanded: boolean;
  selected: boolean;
  detailRef?: Ref<HTMLDivElement>;
  runBusy: boolean;
  stopBusy: boolean;
  watcherCheckBusyId: string | null;
  taskActionId: string | null;
  archiveDisabled: boolean;
  onArchive: ArchiveEpisodeAction;
  mergeBusy: boolean;
  onMerge: (episodeId: string, body: MergeEpisodeBody) => Promise<void>;
  experimentConversation?: ReactNode;
  indexedEntry: ExperimentLoopIndexEntry | null;
  watchedByParentAutoResearch: boolean;
  exactBranchEntry: ExperimentLoopIndexEntry | null;
  providerLabels: Record<string, string>;
  experimentStartsDisabled: boolean;
  mutationsDisabled: boolean;
  project?: ProjectSnapshot;
  onSelectExperiment: (nodeId: string | null, route?: ExperimentRouteIdentity) => void;
  onInspectTask: (operationId: string) => void;
  onRunExperiment: (
    node: GraphNode,
    invocationCeiling?: number,
    browserRequested?: boolean,
    launchConfig?: AgentRunConfig,
    message?: string,
  ) => void;
  onStopExperiment: (nodeId: string, episodeId: string) => void;
  onCheckExperimentWatcher: (watcherId: string) => void;
  onStopExperimentWatcher: (watcherId: string) => void;
  onRecoverExperiment: (task: AgentTask, action: "resume" | "retry") => void;
  onSwitchExperimentProvider: (task: AgentTask) => void;
  onContinueEpisode: (episodeId: string, invocationCeiling: number) => Promise<void>;
  episodeReportHref: (episodeId: string) => string;
}) {
  const detailId = useId();
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const tone = experimentHealthTone(run.health);
  const title = run.node.title;
  const episodeTimestamp = formatEpisodeTimestamp(episode.created_at);
  const isExactBranchEpisode = exactBranchEntry?.episode?.episode_id === episode.episode_id;

  useEffect(() => {
    if (selected) setExpanded(true);
  }, [selected]);

  return (
    <article
      className={`campaign-run experiment-episode-card ${tone}`}
      data-episode-id={episode.episode_id}
    >
      <span className="campaign-state-rail" aria-hidden="true" />
      <div className="campaign-run-heading">
        <button
          className="campaign-run-toggle"
          type="button"
          aria-label={`${expanded ? "Collapse" : "Expand"} Experiment loop episode ${title}`}
          aria-expanded={expanded}
          aria-controls={detailId}
          onClick={() => {
            const next = !expanded;
            setExpanded(next);
            if (next) {
              onSelectExperiment(run.node.id, {
                experiment_id: run.node.id,
                episode_id: episode.episode_id,
                graph_target: episode.graph_target,
                parent_episode_id: indexedEntry?.parent_episode_id ?? null,
              });
            } else if (selected) {
              onSelectExperiment(null);
            }
          }}
        />
        <span className="campaign-run-identity">
          <strong className="campaign-run-title">
            <FlaskConical size={14} aria-hidden="true" />
            <span>{title}</span>
          </strong>
          <span className="campaign-run-meta">
            <span className={`status-pill ${tone}`}>{experimentHealthLabel(run.health)}</span>
            <time dateTime={episode.created_at}>{episodeTimestamp}</time>
            <BranchTag refs={graphRefs} target={episode.graph_target} />
            <ExperimentLoopMetadata
              projectId={episode.project_id}
              graphTarget={episode.graph_target}
              metadata={episode}
              author={episode.authorized_by}
            />
            <EpisodeAuthor author={episode.authorized_by} />
          </span>
        </span>
        <EpisodeBudgetMeter episode={episode} />
        <span className="campaign-run-time">
          <EpisodeArchiveButton
            episode={episode}
            disabled={archiveDisabled}
            onArchive={onArchive}
          />
          <ChevronDown size={16} aria-hidden="true" />
        </span>
      </div>
      {expanded && (
        <div className="campaign-run-detail" id={detailId} tabIndex={-1} ref={detailRef}>
          <ExperimentRunDetail
            apiBase={`/api/projects/${encodeURIComponent(episode.project_id)}`}
            run={run}
            runBusy={runBusy}
            runDisabled={mutationsDisabled}
            startDisabled={experimentStartsDisabled}
            stopBusy={stopBusy}
            recoveryBusy={Boolean(run.currentTask && taskActionId === run.currentTask.operation_id)}
            watcherCheckBusyId={watcherCheckBusyId}
            providerLabel={experimentProviderLabel(run, providerLabels)}
            conversation={experimentConversation}
            ownedByAutoResearch={Boolean(indexedEntry?.parent_episode_id)}
            watchedByParentAutoResearch={watchedByParentAutoResearch}
            allowStart={!isExactBranchEpisode}
            project={project}
            onRun={(invocationCeiling, browserRequested, launchConfig, message) =>
              onRunExperiment(run.node, invocationCeiling, browserRequested, launchConfig, message)
            }
            onContinue={(episodeId, invocationCeiling) =>
              void onContinueEpisode(episodeId, invocationCeiling)
            }
            onStopLoop={() => onStopExperiment(run.node.id, episode.episode_id)}
            onCheckWatcher={onCheckExperimentWatcher}
            onStopWatcher={onStopExperimentWatcher}
            onRecover={(action) => {
              if (run.currentTask) onRecoverExperiment(run.currentTask, action);
            }}
            onInspectTask={onInspectTask}
            onSwitchProvider={() => {
              if (run.currentTask) onSwitchExperimentProvider(run.currentTask);
            }}
            episodeReportHref={episodeReportHref}
          />
          {(episode.graph_branch || episode.code_worktree) && (
            <EpisodeMergePanel
              apiBase={`/api/projects/${encodeURIComponent(episode.project_id)}`}
              episode={episode}
              disabled={archiveDisabled || mutationsDisabled}
              busy={mergeBusy}
              onMerge={onMerge}
            />
          )}
        </div>
      )}
    </article>
  );
}

function formatEpisodeTimestamp(value: string): string {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}

function experimentProviderLabel(
  experiment: ExperimentRun,
  providerLabels: Record<string, string>,
): string | undefined {
  const provider =
    experiment.currentTask?.request.provider || experiment.control?.operational?.session.provider;
  return provider ? providerLabels[provider] || provider : undefined;
}

interface AttentionOverviewProps {
  consolidationCount: number;
  proposals: Proposal[];
  decisions: GraphNode[];
  blockers: GraphNode[];
  onSelectNode: (node: GraphNode) => void;
}

export function AttentionOverview({
  consolidationCount,
  proposals,
  decisions,
  blockers,
  onSelectNode,
}: AttentionOverviewProps) {
  return (
    <section className="view-panel">
      <ViewHeading
        title="Inbox"
        aside={`${proposals.length + decisions.length + blockers.length + consolidationCount} open`}
      />
      <div className="attention-overview-grid">
        <OverviewCard label="Pending proposals" value={proposals.length} />
        <OverviewCard label="Decisions awaiting choice" value={decisions.length} />
        <OverviewCard label="Blockers awaiting judgment" value={blockers.length} />
      </div>
      {(proposals[0] || decisions[0] || blockers[0]) && (
        <h3 className="section-label">Recommended next action</h3>
      )}
      {proposals[0] ? (
        <div className="recommended-action">
          <CircleDot size={16} />
          <strong>Understand and decide “{proposals[0].title}”</strong>
        </div>
      ) : decisions[0] ? (
        <button className="recommended-action" onClick={() => onSelectNode(decisions[0])}>
          <CircleDot size={16} />
          <strong>Choose “{decisions[0].title}”</strong>
        </button>
      ) : blockers[0] ? (
        <button className="recommended-action" onClick={() => onSelectNode(blockers[0])}>
          <CircleDot size={16} />
          <strong>Inspect “{blockers[0].title}”</strong>
        </button>
      ) : null}
    </section>
  );
}

function edgeGeometry(source: DagPosition, target: DagPosition, edgeIndex: number) {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.max(1, Math.hypot(dx, dy));
  const ux = dx / distance;
  const uy = dy / distance;
  const horizontalReach = DAG_NODE_WIDTH / 2 / Math.max(0.001, Math.abs(ux));
  const verticalReach = DAG_NODE_HEIGHT / 2 / Math.max(0.001, Math.abs(uy));
  const reach = Math.min(horizontalReach, verticalReach);
  const startX = source.x + ux * reach;
  const startY = source.y + uy * reach;
  const endX = target.x - ux * reach;
  const endY = target.y - uy * reach;
  const curve = ((edgeIndex % 5) - 2) * 9;
  const controlX = (startX + endX) / 2 - uy * curve;
  const controlY = (startY + endY) / 2 + ux * curve;
  return {
    path: `M ${startX} ${startY} Q ${controlX} ${controlY}, ${endX} ${endY}`,
    labelX: (startX + 2 * controlX + endX) / 4,
    labelY: (startY + 2 * controlY + endY) / 4 - 6,
  };
}

function readRepulsion(): number {
  try {
    const stored = Number(window.localStorage.getItem("rcp:dag-repulsion"));
    if (Number.isFinite(stored) && stored >= 350 && stored <= 1900) return stored;
  } catch {
    // Use the balanced default when browser storage is unavailable.
  }
  return 950;
}

function readDagLayoutMode(): DagLayoutMode {
  try {
    return window.localStorage.getItem("rcp:dag-layout-mode") === "flow" ? "flow" : "force";
  } catch {
    return "force";
  }
}

export function ViewHeading({
  title,
  aside,
  action,
}: {
  title: string;
  aside: string;
  action?: React.ReactNode;
}) {
  return (
    <header className="view-heading">
      <h2>{title}</h2>
      <span className="view-aside">{aside}</span>
      {action}
    </header>
  );
}

export function EmptyState({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="empty-state">
      {icon}
      <strong>{title}</strong>
    </div>
  );
}

function OverviewCard({ label, value }: { label: string; value: number }) {
  return (
    <article className="overview-card">
      <span className="eyebrow">{label}</span>
      <strong>{value}</strong>
    </article>
  );
}
