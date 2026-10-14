import {
  ArrowLeftRight,
  Check,
  GitMerge,
  Link2,
  Pencil,
  Plus,
  Redo2,
  Scissors,
  Trash2,
  Undo2,
  X,
} from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  type RefObject,
} from "react";
import type {
  Edge,
  EvidenceAssessment,
  GraphNode,
  GraphState,
  NewEdge,
  NewNode,
} from "../core/types";
import {
  buildCanvasNode,
  newNodeTypeChoices,
  nodeRemovalRefusal,
  relationChoices,
  requiredCanvasFields,
  type GraphEditingProps,
  type RelationChoice,
} from "./canvasEditing";
import { humanize } from "./nodePresentation";
import { TYPE_LENS } from "./researchType";
import { DAG_NODE_HEIGHT as H, DAG_NODE_WIDTH as W, type DagPosition } from "./useForceDag";

type Point = { x: number; y: number };
type Fresh = {
  at: Point;
  from?: string;
  type: string;
  title: string;
  fields: Record<string, string>;
  pending?: NewNode;
};
type Menu = {
  at: Point;
  choices: RelationChoice[];
  replace?: Edge;
  choice?: RelationChoice;
  relevance?: EvidenceAssessment["relevance"];
  weight?: EvidenceAssessment["weight"];
  node?: NewNode;
};
function automaticRelation(choices: RelationChoice[]): RelationChoice | undefined {
  const ontology = choices.filter((choice) => !choice.meta);
  return ontology.length === 1 ? ontology[0] : choices.length === 1 ? choices[0] : undefined;
}

interface Props {
  graph: GraphState;
  editing?: GraphEditingProps;
  enabled: boolean;
  positions: Record<string, DagPosition>;
  canvasRef: RefObject<HTMLDivElement | null>;
  scrollRef: RefObject<HTMLDivElement | null>;
  zoom: number;
  pinAt: (id: string, x: number, y: number) => void;
  onSelectNode: (node: GraphNode) => void;
}

export function useDagCanvasEditor({
  graph,
  editing,
  enabled,
  positions,
  canvasRef,
  scrollRef,
  zoom,
  pinAt,
  onSelectNode,
}: Props) {
  const active = enabled && !!editing && !editing.mutationsDisabled && !!editing.editOptions;
  const [selected, setSelected] = useState<string[]>([]);
  const [pick, setPick] = useState<{
    kind: "link" | "merge";
    from: string;
    target?: string;
  } | null>(null);
  const [fresh, setFresh] = useState<Fresh | null>(null);
  const [pendingCards, setPendingCards] = useState<Array<{ node: NewNode; at: Point }>>([]);
  const [rename, setRename] = useState<string | null>(null);
  const [menu, setMenu] = useState<Menu | null>(null);
  const [edge, setEdge] = useState<Edge | null>(null);
  const [merge, setMerge] = useState<{ duplicate: string; keeper: string } | null>(null);
  const [mergeHover, setMergeHover] = useState<{ id: string; armed: boolean } | null>(null);
  const [wire, setWire] = useState<{ from: string; to: Point; target?: string } | null>(null);
  const [pendingEdges, setPendingEdges] = useState<NewEdge[]>([]);
  const [box, setBox] = useState<{ a: Point; b: Point } | null>(null);
  const [shake, setShake] = useState<string[]>([]);
  const lastType = useRef("");
  const mergeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mergeCandidate = useRef<{ id: string; armed: boolean } | null>(null);
  const cleanGesture = useRef<(() => void) | null>(null);
  const longPress = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    setFresh((value) =>
      value?.pending &&
      (graph.nodes[value.pending.id] || !editing?.humanDraft?.custom_nodes[value.pending.id])
        ? null
        : value,
    );
    setPendingCards((cards) =>
      cards.filter(
        ({ node }) => !graph.nodes[node.id] && !!editing?.humanDraft?.custom_nodes[node.id],
      ),
    );
    setPendingEdges((old) =>
      old.filter(
        (pending) =>
          !!editing?.humanDraft?.added_edges.some((edge) => edge.id === pending.id) &&
          !Object.values(graph.edges).some(
            (edge) =>
              edge.source === pending.source &&
              edge.target === pending.target &&
              edge.relation === pending.relation,
          ),
      ),
    );
  }, [graph.nodes, graph.edges, editing?.humanDraft]);
  useEffect(
    () => () => {
      cleanGesture.current?.();
      if (longPress.current) clearTimeout(longPress.current);
      if (mergeTimer.current) clearTimeout(mergeTimer.current);
    },
    [],
  );
  const inspectPending = (node: NewNode) =>
    onSelectNode({
      ...node,
      standing: "asserted",
      created_rev: graph.revision,
      updated_rev: graph.revision,
      source_refs: [],
      draft_touched: true,
    });
  const edges = Object.values(graph.edges);
  const choices = (a: string, b: string, skip?: string) =>
    editing?.editOptions
      ? relationChoices(
          editing.editOptions,
          graph.ontology,
          graph.nodes,
          edges.filter((e) => e.id !== skip),
          a,
          b,
        )
      : [];
  const sameType = (a: string, b: string) =>
    a !== b &&
    graph.nodes[a]?.type === graph.nodes[b]?.type &&
    (graph.nodes[a]?.extension_type ?? null) === (graph.nodes[b]?.extension_type ?? null);
  const refused = (id: string) =>
    editing
      ? nodeRemovalRefusal(editing.canonicalGraph, editing.humanDraft, id, editing.experimentActive)
      : null;
  const world = (x: number, y: number): Point => {
    const rect = canvasRef.current?.getBoundingClientRect();
    return { x: (x - (rect?.left ?? 0)) / zoom, y: (y - (rect?.top ?? 0)) / zoom };
  };
  const center = () => {
    const rect = scrollRef.current?.getBoundingClientRect();
    return world(
      (rect?.left ?? 0) + (rect?.width ?? 500) / 2,
      (rect?.top ?? 0) + (rect?.height ?? 400) / 2,
    );
  };
  const port = (id: string) => ({ x: (positions[id]?.x ?? 0) + W / 2, y: positions[id]?.y ?? 0 });
  const controlScale =
    typeof window !== "undefined" && window.matchMedia("(max-width: 560px)").matches ? 1 / zoom : 1;
  const clamped = (at: Point, width = 260): CSSProperties => {
    const rect = scrollRef.current?.getBoundingClientRect();
    if (!rect) return { left: at.x, top: at.y };
    const min = world(rect.left + 8, rect.top + 8),
      max = world(rect.right - 8, rect.bottom - 120);
    const top = Math.max(min.y, Math.min(at.y, max.y));
    return {
      left: Math.max(min.x, Math.min(at.x, max.x - width * controlScale)),
      top,
      maxHeight: Math.max(40, (world(rect.right, rect.bottom - 8).y - top) / controlScale),
      overflowY: "auto",
    };
  };
  const clear = () => {
    setSelected([]);
    setPick(null);
    setMenu(null);
    setEdge(null);
    setRename(null);
    setMerge(null);
    if (!fresh?.pending) setFresh(null);
  };
  const flash = (ids: string[]) => {
    setShake(ids);
    window.setTimeout(() => setShake([]), 450);
  };
  const create = (at: Point, from?: string) => {
    if (!active || !editing?.editOptions) return;
    const types = newNodeTypeChoices(
      editing.editOptions,
      graph.ontology,
      from ? graph.nodes[from] : undefined,
    );
    if (!types.length) return;
    if (fresh?.pending)
      setPendingCards((cards) => [...cards, { node: fresh.pending!, at: fresh.at }]);
    const spot = { ...at };
    while (
      [
        ...Object.values(positions),
        ...pendingCards.map((card) => card.at),
        ...(fresh?.pending ? [fresh.at] : []),
      ].some((p) => Math.abs(p.x - spot.x) < W + 12 && Math.abs(p.y - spot.y) < H + 12)
    )
      spot.y += H + 16;
    setFresh({
      at: spot,
      from,
      type: types.find((t) => t.name === lastType.current)?.name ?? types[0].name,
      title: "",
      fields: {},
    });
    setMenu(null);
    setEdge(null);
    setPick(null);
    setSelected([]);
  };
  const stageChoice = (
    choice: RelationChoice,
    context: Menu,
    relevance = context.relevance,
    weight = context.weight,
  ) => {
    if (!editing) return;
    if (
      context.replace &&
      context.replace.source === choice.source &&
      context.replace.target === choice.target &&
      context.replace.relation === choice.relation
    ) {
      setMenu(null);
      return;
    }
    if (choice.needsAssessment && (!relevance || !weight)) {
      setMenu({ ...context, choice });
      return;
    }
    const next: NewEdge = {
      id: `edge/${crypto.randomUUID()}`,
      source: choice.source,
      target: choice.target,
      relation: choice.relation,
      explanation: context.replace?.explanation ?? "",
      ...(choice.needsAssessment && relevance && weight
        ? {
            assessment: {
              ...context.replace?.assessment,
              relevance,
              weight,
              qualifications: context.replace?.assessment?.qualifications ?? [],
            },
          }
        : {}),
      ...(choice.acceptsExpectation && context.replace?.expectation
        ? { expectation: context.replace.expectation }
        : {}),
    };
    if (context.node) {
      editing.onStageNode(context.node, [next]);
      pinAt(context.node.id, fresh!.at.x, fresh!.at.y);
      setFresh((f) => (f ? { ...f, pending: context.node } : f));
    } else if (context.replace) editing.onReplaceEdge(context.replace.id, next);
    else editing.onStageEdge(next);
    setPendingEdges((old) => [...old, next]);
    setMenu(null);
    setEdge(null);
    setPick(null);
    setWire(null);
  };
  const link = (a: string, b: string, at: Point) => {
    const valid = choices(a, b);
    if (!valid.length) return;
    const context = { at, choices: valid };
    const automatic = automaticRelation(valid);
    if (automatic) stageChoice(automatic, context);
    else setMenu(context);
    setPick(null);
    setWire(null);
  };
  const beginMerge = () => {
    if (selected.length === 2 && sameType(selected[0], selected[1]))
      setMerge({ duplicate: selected[0], keeper: selected[1] });
    else if (selected.length === 1) setPick({ kind: "merge", from: selected[0] });
  };
  const remove = () => {
    if (!active || !editing) return;
    if (edge) {
      editing.onRemoveEdge(edge.id);
      setEdge(null);
      setMenu(null);
      return;
    }
    const blocked = selected.filter((id) => refused(id));
    if (blocked.length) flash(blocked);
    else {
      editing.onRemoveNodes(selected);
      setSelected([]);
    }
  };
  const saveFresh = () => {
    if (!fresh || fresh.pending || !editing?.editOptions) return;
    if (!fresh.title.trim()) {
      setFresh(null);
      return;
    }
    const type = newNodeTypeChoices(
      editing.editOptions,
      graph.ontology,
      fresh.from ? graph.nodes[fresh.from] : undefined,
    ).find((t) => t.name === fresh.type);
    if (!type) return;
    const required = requiredCanvasFields(graph.ontology, type);
    if (
      required.some(
        (f) =>
          !fresh.fields[f.name]?.trim() ||
          (f.kind === "text_list" && !fresh.fields[f.name].split(",").some((part) => part.trim())),
      )
    )
      return;
    const fields: NewNode["extension_fields"] = {};
    for (const field of required) {
      const value = fresh.fields[field.name];
      fields[field.name] =
        field.kind === "number"
          ? Number(value)
          : field.kind === "boolean"
            ? value === "true"
            : field.kind === "text_list"
              ? value
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean)
              : value;
    }
    const node = buildCanvasNode(
      type,
      editing.editOptions,
      fresh.title.trim(),
      new Set([
        ...Object.keys(editing.canonicalGraph.nodes),
        ...Object.keys(graph.nodes),
        ...Object.keys(editing.humanDraft?.custom_nodes ?? {}),
      ]),
      fields,
    );
    lastType.current = type.name;
    if (fresh.from) {
      const nodes = { ...graph.nodes, [node.id]: node as GraphNode };
      const valid = relationChoices(
        editing.editOptions,
        graph.ontology,
        nodes,
        edges,
        fresh.from,
        node.id,
      );
      const context = { at: fresh.at, choices: valid, node };
      const automatic = automaticRelation(valid);
      if (automatic) stageChoice(automatic, context);
      else setMenu(context);
    } else {
      editing.onStageNode(node, []);
      pinAt(node.id, fresh.at.x, fresh.at.y);
      setFresh({ ...fresh, pending: node });
    }
  };
  const eligible = (id: string) =>
    pick?.kind === "merge"
      ? sameType(pick.from, id)
      : !!(wire?.from || pick?.from) && choices(wire?.from ?? pick!.from, id).length > 0;
  const onNodeClick = (event: MouseEvent, node: GraphNode) => {
    if (!active) return false;
    if (pick) {
      if (eligible(node.id)) {
        if (pick.kind === "merge") {
          setMerge({ duplicate: pick.from, keeper: node.id });
          setPick(null);
        } else link(pick.from, node.id, positions[node.id]);
      }
      return true;
    }
    setSelected((old) =>
      event.shiftKey || event.metaKey || event.ctrlKey
        ? old.includes(node.id)
          ? old.filter((id) => id !== node.id)
          : [...old, node.id]
        : [node.id],
    );
    setEdge(null);
    setMenu(null);
    return false;
  };
  const listen = (
    move: (event: globalThis.PointerEvent) => void,
    end: (event: globalThis.PointerEvent) => void,
  ) => {
    cleanGesture.current?.();
    const cleanup = () => {
      if (longPress.current) clearTimeout(longPress.current);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", cancel);
      cleanGesture.current = null;
    };
    const finish = (e: globalThis.PointerEvent) => {
      cleanup();
      end(e);
    };
    const cancel = () => {
      cleanup();
      setWire(null);
      setBox(null);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", cancel);
    cleanGesture.current = cleanup;
  };
  const onHandlePointerDown = (event: PointerEvent, id: string) => {
    if (!active) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.currentTarget instanceof HTMLElement)
      event.currentTarget.focus({ preventScroll: true });
    const start = { x: event.clientX, y: event.clientY };
    let moved = false;
    let target: string | undefined;
    setWire({ from: id, to: port(id) });
    setSelected([id]);
    setPick(null);
    const move = (e: globalThis.PointerEvent) => {
      moved ||= Math.hypot(e.clientX - start.x, e.clientY - start.y) > 5;
      const point = world(e.clientX, e.clientY);
      let best = 48 / zoom;
      target = undefined;
      for (const candidate of Object.keys(positions)) {
        if (!choices(id, candidate).length) continue;
        const p = port(candidate),
          distance = Math.hypot(point.x - p.x, point.y - p.y);
        if (distance < best) {
          best = distance;
          target = candidate;
        }
      }
      if (!target)
        target = Object.keys(positions).find(
          (candidate) =>
            candidate !== id &&
            choices(id, candidate).length > 0 &&
            Math.abs(positions[candidate].x - point.x) < W / 2 &&
            Math.abs(positions[candidate].y - point.y) < H / 2,
        );
      setWire({ from: id, to: target ? port(target) : point, target });
    };
    listen(move, (e) => {
      if (!moved) {
        setWire(null);
        setPick({ kind: "link", from: id });
      } else if (target) link(id, target, world(e.clientX, e.clientY));
      else {
        const point = world(e.clientX, e.clientY);
        const occupied = Object.values(positions).some(
          (p) => Math.abs(p.x - point.x) < W / 2 && Math.abs(p.y - point.y) < H / 2,
        );
        setWire(null);
        if (!occupied) create(point, id);
      }
    });
  };
  const onNodeDragMove = (id: string, x: number, y: number) => {
    if (!active) return;
    const point = world(x, y),
      target = Object.keys(positions).find(
        (other) =>
          sameType(id, other) &&
          Math.abs(positions[other].x - point.x) < W / 2 &&
          Math.abs(positions[other].y - point.y) < H / 2,
      );
    if (mergeCandidate.current?.id === target) return;
    if (mergeTimer.current) clearTimeout(mergeTimer.current);
    mergeCandidate.current = target ? { id: target, armed: false } : null;
    setMergeHover(mergeCandidate.current);
    if (target)
      mergeTimer.current = setTimeout(() => {
        mergeCandidate.current = { id: target, armed: true };
        setMergeHover(mergeCandidate.current);
      }, 350);
  };
  const onNodeDragEnd = (id: string, commit = true) => {
    const candidate = mergeCandidate.current;
    if (mergeTimer.current) clearTimeout(mergeTimer.current);
    mergeCandidate.current = null;
    setMergeHover(null);
    if (commit && candidate?.armed) {
      setMerge({ duplicate: id, keeper: candidate.id });
      return true;
    }
    return false;
  };
  const emptyTarget = (target: EventTarget) =>
    target instanceof Element &&
    !target.closest("[data-node-id],button,input,select,[data-canvas-ui],.dag-node,.dag-edge-hit");
  const onCanvasPointerDown = (event: PointerEvent) => {
    if (!active || !emptyTarget(event.target)) return false;
    const start = world(event.clientX, event.clientY);
    if (pick?.kind === "link") {
      create(start, pick.from);
      return true;
    }
    if (event.shiftKey) {
      event.preventDefault();
      const base = selected;
      setBox({ a: start, b: start });
      listen(
        (e) => {
          const b = world(e.clientX, e.clientY);
          setBox({ a: start, b });
          setSelected([
            ...new Set([
              ...base,
              ...Object.keys(positions).filter((id) => {
                const p = positions[id];
                return (
                  p.x >= Math.min(start.x, b.x) &&
                  p.x <= Math.max(start.x, b.x) &&
                  p.y >= Math.min(start.y, b.y) &&
                  p.y <= Math.max(start.y, b.y)
                );
              }),
            ]),
          ]);
        },
        () => setBox(null),
      );
      return true;
    }
    if (event.pointerType === "touch") {
      const timer = setTimeout(() => create(start), 550);
      longPress.current = timer;
      const x = event.clientX,
        y = event.clientY;
      listen(
        (e) => {
          if (Math.hypot(e.clientX - x, e.clientY - y) > 8) clearTimeout(timer);
        },
        () => clearTimeout(timer),
      );
    }
    setSelected([]);
    setEdge(null);
    setMenu(null);
    if (fresh && !fresh.pending && !fresh.title.trim()) setFresh(null);
    return false;
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (
      !active ||
      (event.target instanceof Element && event.target.closest("input,textarea,select"))
    )
      return;
    const key = event.key.toLowerCase();
    if ((event.metaKey || event.ctrlKey) && key === "z") {
      event.preventDefault();
      if (event.shiftKey) editing?.onRedo();
      else editing?.onUndo();
    } else if (key === "escape") clear();
    else if (menu && /^[1-9]$/.test(key)) {
      const choice = menu.choices[Number(key) - 1];
      if (choice) stageChoice(choice, menu);
    } else if (pick && (key === "tab" || key === "enter")) {
      event.preventDefault();
      const targets = Object.keys(positions).filter(eligible);
      if (key === "tab" && targets.length) {
        const index = targets.indexOf(pick.target ?? "");
        setPick({
          ...pick,
          target:
            targets[
              index < 0
                ? event.shiftKey
                  ? targets.length - 1
                  : 0
                : (index + (event.shiftKey ? -1 : 1) + targets.length) % targets.length
            ],
        });
      } else if (pick.target) {
        if (pick.kind === "merge") {
          setMerge({ duplicate: pick.from, keeper: pick.target });
          setPick(null);
        } else link(pick.from, pick.target, positions[pick.target]);
      }
    } else if (key === "n") {
      event.preventDefault();
      create(center());
    } else if (key === "l" && selected.length === 1) {
      event.preventDefault();
      setPick({ kind: "link", from: selected[0] });
    } else if (key === "m") {
      event.preventDefault();
      beginMerge();
    } else if ((key === "enter" || key === "f2") && selected.length === 1) {
      event.preventDefault();
      setRename(selected[0]);
    } else if (key === "delete" || key === "backspace") {
      event.preventDefault();
      remove();
    }
  };
  const visibleFresh = fresh && !graph.nodes[fresh.pending?.id ?? ""] ? fresh : null;
  const types =
    visibleFresh && editing?.editOptions
      ? newNodeTypeChoices(
          editing.editOptions,
          graph.ontology,
          visibleFresh.from ? graph.nodes[visibleFresh.from] : undefined,
        )
      : [];
  const freshType = types.find((t) => t.name === visibleFresh?.type);
  const selectedPosition = selected.length ? positions[selected[0]] : null;
  const edgeAt =
    edge && positions[edge.source] && positions[edge.target]
      ? {
          x: (positions[edge.source].x + positions[edge.target].x) / 2,
          y: (positions[edge.source].y + positions[edge.target].y) / 2,
        }
      : null;
  const wirePath = (a: Point, b: Point) =>
    `M${a.x},${a.y} C${a.x + 80},${a.y} ${b.x + 80},${b.y} ${b.x},${b.y}`;
  const controls = active ? (
    <div className="dag-edit-controls" data-canvas-ui>
      <button type="button" onClick={() => create(center())}>
        <Plus size={15} /> Node
      </button>
      <button
        type="button"
        aria-label="Undo"
        disabled={!editing?.canUndo}
        onClick={editing?.onUndo}
      >
        <Undo2 size={15} />
      </button>
      <button
        type="button"
        aria-label="Redo"
        disabled={!editing?.canRedo}
        onClick={editing?.onRedo}
      >
        <Redo2 size={15} />
      </button>
      {pick && (
        <span className="dag-pick-chip">
          {pick.kind === "link" ? <Link2 size={14} /> : <GitMerge size={14} />}
          {graph.nodes[pick.from]?.title}
          <button type="button" aria-label="Cancel" onClick={() => setPick(null)}>
            <X size={14} />
          </button>
        </span>
      )}
    </div>
  ) : null;
  const overlays = active ? (
    <>
      <svg className="dag-editor-wire" width="1" height="1">
        {wire && (
          <path
            d={wirePath(port(wire.from), wire.to)}
            className={wire.target ? "is-snapped" : ""}
          />
        )}
        {pendingEdges
          .filter(
            (e) =>
              !edges.some(
                (real) =>
                  real.source === e.source &&
                  real.target === e.target &&
                  real.relation === e.relation,
              ),
          )
          .map((e) => {
            const a =
                positions[e.source] ??
                (fresh?.pending?.id === e.source
                  ? fresh.at
                  : pendingCards.find(({ node }) => node.id === e.source)?.at),
              b =
                positions[e.target] ??
                (fresh?.pending?.id === e.target
                  ? fresh.at
                  : pendingCards.find(({ node }) => node.id === e.target)?.at);
            return a && b ? (
              <path key={e.id} className="is-pending" d={wirePath({ x: a.x + W / 2, y: a.y }, b)} />
            ) : null;
          })}
      </svg>
      {pendingCards.map(
        ({ node, at }) =>
          !graph.nodes[node.id] && (
            <div
              key={node.id}
              className={`dag-staged-card is-pending ${"origin" in node && node.origin === "unknown" ? "needs-origin" : ""}`}
              data-canvas-ui
              style={{ left: at.x - (W * controlScale) / 2, top: at.y - (H * controlScale) / 2 }}
            >
              <strong>{node.title}</strong>
              {"origin" in node && node.origin === "unknown" && (
                <button
                  type="button"
                  aria-label="Edit node origin"
                  onClick={() => inspectPending(node)}
                >
                  <Pencil size={14} />
                </button>
              )}
            </div>
          ),
      )}
      {box && (
        <div
          className="dag-marquee"
          style={{
            left: Math.min(box.a.x, box.b.x),
            top: Math.min(box.a.y, box.b.y),
            width: Math.abs(box.a.x - box.b.x),
            height: Math.abs(box.a.y - box.b.y),
          }}
        />
      )}
      {selectedPosition && !rename && !pick && !merge && (
        <div
          className="dag-edit-float"
          data-canvas-ui
          style={clamped({ x: selectedPosition.x - 80, y: selectedPosition.y - H / 2 - 44 }, 160)}
        >
          {selected.length === 1 && (
            <>
              <button
                type="button"
                aria-label="Link"
                onClick={() => setPick({ kind: "link", from: selected[0] })}
              >
                <Link2 size={16} />
              </button>
              <button type="button" aria-label="Rename" onClick={() => setRename(selected[0])}>
                <Pencil size={16} />
              </button>
            </>
          )}
          {(selected.length === 1 ||
            (selected.length === 2 && sameType(selected[0], selected[1]))) && (
            <button type="button" aria-label="Merge" onClick={beginMerge}>
              <GitMerge size={16} />
            </button>
          )}
          <button
            type="button"
            aria-label="Delete"
            disabled={selected.some((id) => !!refused(id))}
            onClick={remove}
          >
            <Trash2 size={16} />
          </button>
        </div>
      )}
      {edge && edgeAt && !menu && (
        <div className="dag-edit-float dag-edge-chip" data-canvas-ui style={clamped(edgeAt)}>
          <button
            type="button"
            onClick={() =>
              setMenu({
                at: edgeAt,
                choices: choices(edge.source, edge.target, edge.id),
                replace: edge,
              })
            }
          >
            {humanize(edge.relation)} ▾
          </button>
          <button type="button" aria-label="Unlink" onClick={remove}>
            <Scissors size={16} />
          </button>
        </div>
      )}
      {menu && (
        <div className="dag-edit-menu" data-canvas-ui style={clamped(menu.at)}>
          {menu.choice ? (
            <>
              <strong>{humanize(menu.choice.relation)}</strong>
              <div className="dag-assessment-row">
                {(["direct", "indirect", "contextual"] as const).map((value) => (
                  <button
                    type="button"
                    key={value}
                    aria-pressed={menu.relevance === value}
                    onClick={() => setMenu({ ...menu, relevance: value })}
                  >
                    {humanize(value)}
                  </button>
                ))}
              </div>
              <div className="dag-assessment-row">
                {(["limited", "moderate", "strong"] as const).map((value) => (
                  <button
                    type="button"
                    key={value}
                    aria-pressed={menu.weight === value}
                    onClick={() => setMenu({ ...menu, weight: value })}
                  >
                    {humanize(value)}
                  </button>
                ))}
              </div>
              <button
                type="button"
                disabled={!menu.relevance || !menu.weight}
                onClick={() => stageChoice(menu.choice!, menu)}
              >
                Link
              </button>
            </>
          ) : (
            menu.choices.map((choice, index) => (
              <button
                type="button"
                key={`${choice.relation}/${choice.source}`}
                data-relation={choice.relation}
                className={choice.meta ? "is-meta" : ""}
                onClick={() => stageChoice(choice, menu)}
              >
                {menu.replace?.relation === choice.relation &&
                  menu.replace.source === choice.source && <Check size={13} />}
                <span>{humanize(choice.relation)}</span>
                {choice.source !== menu.choices[0].source && <span>↶</span>}
                <kbd>{index + 1}</kbd>
              </button>
            ))
          )}
        </div>
      )}
      {merge && (
        <div
          className="dag-edit-menu dag-merge-confirm"
          data-canvas-ui
          style={clamped(positions[merge.keeper] ?? center())}
        >
          <div>
            <s>{graph.nodes[merge.duplicate]?.title}</s>
            <button
              type="button"
              aria-label="Swap which node is kept"
              onClick={() => setMerge({ duplicate: merge.keeper, keeper: merge.duplicate })}
            >
              <ArrowLeftRight size={16} />
            </button>
            <strong>{graph.nodes[merge.keeper]?.title}</strong>
          </div>
          <div>
            <span>
              <Link2 size={13} />
              {
                edges.filter(
                  (e) =>
                    (e.source === merge.duplicate || e.target === merge.duplicate) &&
                    e.source !== e.target &&
                    e.source !== merge.keeper &&
                    e.target !== merge.keeper &&
                    !edges.some(
                      (other) =>
                        other.relation === e.relation &&
                        other.source === (e.source === merge.duplicate ? merge.keeper : e.source) &&
                        other.target === (e.target === merge.duplicate ? merge.keeper : e.target),
                    ),
                ).length
              }
            </span>
            <button type="button" onClick={() => setMerge(null)}>
              Cancel
            </button>
            <button
              type="button"
              aria-label="Confirm merge"
              disabled={!!refused(merge.duplicate)}
              onClick={() => {
                editing?.onMergeNodes(merge.duplicate, merge.keeper);
                setMerge(null);
                setSelected([]);
              }}
            >
              Merge
            </button>
          </div>
        </div>
      )}
      {visibleFresh && freshType && (
        <form
          className={`dag-staged-card ${visibleFresh.pending ? "is-pending" : ""} ${visibleFresh.pending && "origin" in visibleFresh.pending && visibleFresh.pending.origin === "unknown" ? "needs-origin" : ""}`}
          data-canvas-ui
          style={
            {
              left: visibleFresh.at.x - (W / 2) * controlScale,
              top: visibleFresh.at.y - (H / 2) * controlScale,
              "--accent": TYPE_LENS[freshType.base_type].color,
            } as CSSProperties
          }
          onSubmit={(e) => {
            e.preventDefault();
            saveFresh();
          }}
          onPointerDown={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !visibleFresh.pending) {
              e.preventDefault();
              e.stopPropagation();
              saveFresh();
            }
            if (e.key === "Escape" && !visibleFresh.pending) {
              e.stopPropagation();
              setFresh(null);
            }
          }}
        >
          <div className="dag-type-dots">
            <span>{humanize(visibleFresh.type)}</span>
            {types.map((type) => (
              <button
                type="button"
                key={type.name}
                data-node-type={type.name}
                aria-label={humanize(type.name)}
                aria-pressed={type.name === visibleFresh.type}
                disabled={!!visibleFresh.pending}
                style={{ "--dot": TYPE_LENS[type.base_type].color } as CSSProperties}
                onClick={() => setFresh({ ...visibleFresh, type: type.name, fields: {} })}
              />
            ))}
          </div>
          {visibleFresh.pending ? (
            <strong>{visibleFresh.title}</strong>
          ) : (
            <input
              autoFocus
              aria-label="Node title"
              placeholder="Title"
              value={visibleFresh.title}
              onChange={(e) => setFresh({ ...visibleFresh, title: e.target.value })}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Escape") setFresh(null);
                if (e.key === "Tab") {
                  e.preventDefault();
                  const index = types.findIndex((t) => t.name === visibleFresh.type);
                  setFresh({
                    ...visibleFresh,
                    type: types[(index + (e.shiftKey ? -1 : 1) + types.length) % types.length].name,
                    fields: {},
                  });
                }
              }}
            />
          )}
          {!visibleFresh.pending && (
            <div className="dag-required-fields">
              {requiredCanvasFields(graph.ontology, freshType).map((field) =>
                field.kind === "boolean" ? (
                  <select
                    required
                    key={field.name}
                    aria-label={humanize(field.name)}
                    value={visibleFresh.fields[field.name] ?? ""}
                    onChange={(e) =>
                      setFresh({
                        ...visibleFresh,
                        fields: { ...visibleFresh.fields, [field.name]: e.target.value },
                      })
                    }
                  >
                    <option value="">{humanize(field.name)}</option>
                    <option value="true">Yes</option>
                    <option value="false">No</option>
                  </select>
                ) : (
                  <input
                    required
                    key={field.name}
                    type={field.kind === "number" ? "number" : "text"}
                    step="any"
                    aria-label={humanize(field.name)}
                    placeholder={humanize(field.name)}
                    value={visibleFresh.fields[field.name] ?? ""}
                    onChange={(e) =>
                      setFresh({
                        ...visibleFresh,
                        fields: { ...visibleFresh.fields, [field.name]: e.target.value },
                      })
                    }
                  />
                ),
              )}
            </div>
          )}
          {visibleFresh.pending &&
            "origin" in visibleFresh.pending &&
            visibleFresh.pending.origin === "unknown" && (
              <button
                type="button"
                aria-label="Edit node origin"
                onClick={() => inspectPending(visibleFresh.pending!)}
              >
                <Pencil size={14} />
              </button>
            )}
          <button type="submit" hidden />
        </form>
      )}
    </>
  ) : null;
  return {
    controls,
    overlays,
    onNodeClick,
    onHandlePointerDown,
    onNodeDragMove,
    onNodeDragEnd,
    onCanvasPointerDown,
    onKeyDown,
    onHandleClick: (event: MouseEvent, id: string) => {
      event.stopPropagation();
      if (active && event.detail === 0) {
        setSelected([id]);
        setPick({ kind: "link", from: id });
      }
    },
    onNodeDoubleClick: (node: GraphNode) => {
      if (active) {
        setSelected([node.id]);
        setRename(node.id);
      }
    },
    onCanvasDoubleClick: (event: MouseEvent) => {
      if (active && emptyTarget(event.target)) {
        event.preventDefault();
        create(world(event.clientX, event.clientY));
      }
    },
    onEdgeClick: (event: MouseEvent, next: Edge) => {
      if (active) {
        event.stopPropagation();
        setEdge(next);
        setMenu(null);
        setSelected([]);
      }
    },
    nodeClass: (id: string) =>
      active
        ? [
            selected.includes(id) && "is-canvas-selected",
            (pick || wire) && eligible(id) && "is-link-candidate",
            (pick || wire) &&
              id !== (pick?.from ?? wire?.from) &&
              !eligible(id) &&
              "is-link-ineligible",
            (wire?.target === id || pick?.target === id) && "is-link-snap",
            mergeHover?.id === id && (mergeHover.armed ? "is-merge-armed" : "is-merge-hover"),
            shake.includes(id) && "is-shake",
          ]
            .filter(Boolean)
            .join(" ")
        : "",
    renameField: (node: GraphNode) =>
      active && rename === node.id ? (
        <input
          autoFocus
          className="dag-inline-title"
          aria-label="Node title"
          defaultValue={node.title}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Escape") setRename(null);
            if (e.key === "Enter") {
              e.preventDefault();
              const title = e.currentTarget.value.trim();
              if (title && title !== node.title) editing?.onRenameNode(node.id, title);
              setRename(null);
            }
          }}
          onBlur={() => setRename(null)}
        />
      ) : null,
  };
}
