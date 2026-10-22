/**
 * The row an arrow, Home, or End key moves focus to within a popover's rows,
 * wrapping at the ends; null for any other key or a target outside the rows.
 */
export function rovingFocusTarget<T extends HTMLElement>(
  rows: readonly T[],
  current: EventTarget,
  key: string,
): T | null {
  const index = rows.indexOf(current as T);
  if (index < 0) return null;
  if (key === "ArrowDown") return rows[(index + 1) % rows.length];
  if (key === "ArrowUp") return rows[(index - 1 + rows.length) % rows.length];
  if (key === "Home") return rows[0];
  if (key === "End") return rows[rows.length - 1];
  return null;
}
