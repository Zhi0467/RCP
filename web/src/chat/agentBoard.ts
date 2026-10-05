import type { ConversationAgentGroup } from "./chatWorkspace";

/** The Agents board's columns. State columns follow the run; only Archived is a human choice. */
export type AgentBoardColumn = "needs_you" | "working" | "done" | "archived";
export const AGENT_BOARD_COLUMNS: readonly AgentBoardColumn[] = [
  "needs_you",
  "working",
  "done",
  "archived",
];
const ORDER_LIMIT = 500;

export function agentBoardColumn(group: ConversationAgentGroup): AgentBoardColumn {
  return group === "working" ? "working" : group === "done" ? "done" : "needs_you";
}

/** Pins lead in pin order, then cards the human never placed (newest first, as
 *  given), then the human's own order. */
export function orderAgentBoardCards<T>(
  items: readonly T[],
  id: (item: T) => string,
  order: readonly string[],
  pinned: readonly string[] = [],
): T[] {
  const pinRank = new Map(pinned.map((chatId, index) => [chatId, index]));
  const rank = new Map(order.map((chatId, index) => [chatId, index]));
  const key = (item: T, index: number): [number, number] => {
    const chatId = id(item);
    if (pinRank.has(chatId)) return [0, pinRank.get(chatId)!];
    if (!rank.has(chatId)) return [1, index];
    return [2, rank.get(chatId)!];
  };
  return items
    .map((item, index) => ({ item, key: key(item, index) }))
    .sort((left, right) => left.key[0] - right.key[0] || left.key[1] - right.key[1])
    .map(({ item }) => item);
}

/** A column's card sequence once `dragged` lands before `beforeId`, or last. */
export function placeAgentBoardCard(
  columnIds: readonly string[],
  dragged: string,
  beforeId: string | null,
): string[] {
  const column = columnIds.filter((chatId) => chatId !== dragged);
  const at = beforeId === null ? -1 : column.indexOf(beforeId);
  column.splice(at < 0 ? column.length : at, 0, dragged);
  return column;
}

/** The saved order with one column's sequence written whole, so its cards keep
 *  their places as new agents arrive. */
export function saveAgentBoardColumn(
  order: readonly string[],
  columnIds: readonly string[],
): string[] {
  const placed = new Set(columnIds);
  return [...columnIds, ...order.filter((chatId) => !placed.has(chatId))].slice(0, ORDER_LIMIT);
}

export type AgentBoardDrop = "reorder" | "archive" | "restore";

/** A working agent cannot be archived; a state column takes no drop from another
 *  state column, because the run decides the state. */
export function agentBoardDrop(
  from: AgentBoardColumn,
  to: AgentBoardColumn,
  working: boolean,
): AgentBoardDrop | null {
  if (from === to) return "reorder";
  if (to === "archived") return working ? null : "archive";
  if (from === "archived") return "restore";
  return null;
}

export function agentBoardOrderStorageKey(projectId: string): string {
  return `rcp:agent-board-order:${projectId}`;
}

export function readAgentBoardOrder(projectId: string): string[] {
  try {
    const value: unknown = JSON.parse(
      window.localStorage.getItem(agentBoardOrderStorageKey(projectId)) ?? "[]",
    );
    return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
  } catch {
    return [];
  }
}

export function writeAgentBoardOrder(projectId: string, order: readonly string[]): void {
  try {
    window.localStorage.setItem(agentBoardOrderStorageKey(projectId), JSON.stringify(order));
  } catch {
    // Card order is a per-viewer convenience; a storage failure keeps this session's order.
  }
}
