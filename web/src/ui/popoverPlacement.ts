const VIEWPORT_GUTTER = 16;
const TRIGGER_GAP = 4;

export type PopoverPlacement = { top?: number; bottom?: number; right: number; maxHeight: number };

/** Right-aligned beside the trigger, shifted left only as far as the viewport allows
 *  for a popover `width` wide. It opens on the side with more room and never runs
 *  past the viewport, so a long list scrolls inside it. */
export function popoverPlacement(
  trigger: HTMLElement | null,
  width: number,
): PopoverPlacement | null {
  const rect = trigger?.getBoundingClientRect();
  if (!rect) return null;
  const fitted = Math.min(width, window.innerWidth - 2 * VIEWPORT_GUTTER);
  const right = Math.max(VIEWPORT_GUTTER, window.innerWidth - rect.right);
  const below = window.innerHeight - rect.bottom - TRIGGER_GAP - VIEWPORT_GUTTER;
  const above = rect.top - TRIGGER_GAP - VIEWPORT_GUTTER;
  const cap = window.innerHeight * 0.65;
  return {
    ...(below >= above
      ? { top: rect.bottom + TRIGGER_GAP }
      : { bottom: window.innerHeight - rect.top + TRIGGER_GAP }),
    right: Math.min(right, window.innerWidth - VIEWPORT_GUTTER - fitted),
    maxHeight: Math.min(cap, Math.max(below, above)),
  };
}
