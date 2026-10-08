import { Fragment, useRef, useState, type ReactNode } from "react";
import {
  AGENT_BOARD_COLUMNS,
  agentBoardDrop,
  placeAgentBoardCard,
  type AgentBoardColumn,
  type AgentBoardDrop,
} from "./agentBoardModel";

export interface AgentBoardCard {
  id: string;
  title: string;
  column: AgentBoardColumn;
  working: boolean;
}

interface Props<C extends AgentBoardCard> {
  columns: Record<AgentBoardColumn, C[]>;
  labels: Record<AgentBoardColumn, string>;
  renderCard: (card: C) => ReactNode;
  onOpen: (card: C) => void;
  onDrop: (card: C, drop: AgentBoardDrop, columnIds: string[]) => void;
}

interface DragState {
  id: string;
  from: AgentBoardColumn;
  pointerId: number;
  startX: number;
  startY: number;
  active: boolean;
}

interface DropTarget {
  column: AgentBoardColumn;
  beforeId: string | null;
}

const DRAG_THRESHOLD = 6;

export function AgentBoard<C extends AgentBoardCard>({
  columns,
  labels,
  renderCard,
  onOpen,
  onDrop,
}: Props<C>) {
  // Folding belongs to this viewer; it never changes the shared archive set.
  const [archivedOpen, setArchivedOpen] = useState(false);
  const drag = useRef<DragState | null>(null);
  const suppressClick = useRef(false);
  const [offset, setOffset] = useState<{ id: string; x: number; y: number } | null>(null);
  const [target, setTarget] = useState<DropTarget | null>(null);
  const cardById = (id: string) =>
    AGENT_BOARD_COLUMNS.flatMap((column) => columns[column]).find((card) => card.id === id);
  const columnIds = (column: AgentBoardColumn) => columns[column].map((card) => card.id);

  const dropFor = (state: DragState, at: DropTarget | null) => {
    const card = cardById(state.id);
    return card && at ? agentBoardDrop(state.from, at.column, card.working) : null;
  };

  const targetAt = (x: number, y: number, dragged: string): DropTarget | null => {
    const columnElement = document
      .elementFromPoint(x, y)
      ?.closest<HTMLElement>("[data-board-column]");
    const column = columnElement?.dataset.boardColumn as AgentBoardColumn | undefined;
    if (!columnElement || !column) return null;
    const before = [...columnElement.querySelectorAll<HTMLElement>("[data-card-id]")].find(
      (element) => {
        if (element.dataset.cardId === dragged) return false;
        const box = element.getBoundingClientRect();
        return y < box.top + box.height / 2;
      },
    );
    return { column, beforeId: before?.dataset.cardId ?? null };
  };

  const finish = () => {
    drag.current = null;
    setOffset(null);
    setTarget(null);
  };

  const commit = (card: C, drop: AgentBoardDrop, at: DropTarget) => {
    onDrop(card, drop, placeAgentBoardCard(columnIds(at.column), card.id, at.beforeId));
  };

  // Read by the window listeners, so a drag always sees the current columns.
  const pointer = useRef({ move: (_event: PointerEvent) => {}, end: (_event: PointerEvent) => {} });
  pointer.current = {
    move: (event) => {
      const state = drag.current;
      if (!state || state.pointerId !== event.pointerId) return;
      const x = event.clientX - state.startX;
      const y = event.clientY - state.startY;
      if (!state.active && Math.hypot(x, y) < DRAG_THRESHOLD) return;
      state.active = true;
      setOffset({ id: state.id, x, y });
      setTarget(targetAt(event.clientX, event.clientY, state.id));
    },
    end: (event) => {
      const state = drag.current;
      if (!state || state.pointerId !== event.pointerId) return;
      if (state.active && event.type === "pointerup") {
        // The click that follows this release lands before any timer runs.
        suppressClick.current = true;
        window.setTimeout(() => (suppressClick.current = false));
        const card = cardById(state.id);
        const at = targetAt(event.clientX, event.clientY, state.id);
        const drop = dropFor(state, at);
        if (card && drop && at) commit(card, drop, at);
      }
      finish();
    },
  };

  const shell = (card: C, column: AgentBoardColumn) => {
    const dragging = offset?.id === card.id;
    const moveBy = (step: -1 | 1) => {
      const ids = columnIds(column);
      const index = ids.indexOf(card.id);
      const next = index + step;
      if (index < 0 || next < 0 || next >= ids.length) return;
      [ids[index], ids[next]] = [ids[next], ids[index]];
      onDrop(card, "reorder", ids);
    };
    const open = (
      <button
        className="agent-card-open"
        type="button"
        aria-label={`Open ${card.title}`}
        aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown"
        onClick={() => onOpen(card)}
        onKeyDown={(event) => {
          if (!event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
          event.preventDefault();
          moveBy(event.key === "ArrowUp" ? -1 : 1);
        }}
      />
    );
    return (
      <div
        className={`agent-card${dragging ? " dragging" : ""}`}
        data-card-id={card.id}
        data-state={card.working ? "working" : undefined}
        style={dragging ? { transform: `translate(${offset.x}px, ${offset.y}px)` } : undefined}
        onClickCapture={(event) => {
          // The click that ends a drag must not also open the card.
          if (!suppressClick.current) return;
          suppressClick.current = false;
          event.preventDefault();
          event.stopPropagation();
        }}
        onPointerDown={(event) => {
          // Touch keeps scrolling the board; a card there moves through its menu.
          if (event.button !== 0 || event.pointerType === "touch") return;
          if (
            event.target instanceof Element &&
            event.target.closest("button:not(.agent-card-open), input, [role=menu]")
          )
            return;
          drag.current = {
            id: card.id,
            from: column,
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            active: false,
          };
          // A quick drag leaves the card before it starts, so the press is followed
          // on the window until it ends.
          const move = (moved: PointerEvent) => pointer.current.move(moved);
          const end = (ended: PointerEvent) => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", end);
            window.removeEventListener("pointercancel", end);
            pointer.current.end(ended);
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", end);
          window.addEventListener("pointercancel", end);
        }}
      >
        {open}
        {renderCard(card)}
      </div>
    );
  };

  const activeDrop = drag.current?.active ? dropFor(drag.current, target) : null;
  return (
    <div className={`agent-board${offset ? " is-dragging" : ""}`}>
      {AGENT_BOARD_COLUMNS.map((column) => {
        const cards = columns[column];
        const here = target?.column === column && offset !== null;
        const allowed =
          offset && drag.current
            ? agentBoardDrop(drag.current.from, column, cardById(offset.id)?.working ?? false)
            : null;
        const line = <div className="agent-board-drop-line" aria-hidden="true" />;
        return (
          <section
            className="agent-board-column"
            data-board-column={column}
            data-drop={offset ? (allowed ? "allowed" : "refused") : undefined}
            aria-label={labels[column]}
            key={column}
          >
            <header className="agent-board-column-head">
              <span className="agent-board-dot" aria-hidden="true" />
              <h3>
                {column === "archived" ? (
                  <button
                    type="button"
                    className="agent-board-fold"
                    aria-expanded={archivedOpen}
                    aria-controls="agent-board-archived-cards"
                    onClick={() => setArchivedOpen((open) => !open)}
                  >
                    {labels[column]} <span aria-hidden="true">{archivedOpen ? "▾" : "▸"}</span>
                  </button>
                ) : (
                  labels[column]
                )}
              </h3>
              <span className="agent-board-count">{cards.length}</span>
            </header>
            <div
              className="agent-board-cards"
              id={column === "archived" ? "agent-board-archived-cards" : undefined}
              hidden={column === "archived" && !archivedOpen}
            >
              {cards.map((card) => (
                <Fragment key={card.id}>
                  {here && activeDrop && target?.beforeId === card.id && line}
                  {shell(card, column)}
                </Fragment>
              ))}
              {here && activeDrop && target?.beforeId === null && line}
              {cards.length === 0 && <p className="agent-board-empty">Nothing here.</p>}
            </div>
          </section>
        );
      })}
    </div>
  );
}
