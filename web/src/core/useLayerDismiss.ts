import { useEffect, type RefObject } from "react";

/**
 * Closes one open popover layer on Escape or on a press outside `inside`.
 *
 * Escape is taken in the capture phase and marked handled, so an enclosing
 * layer that skips a handled Escape (the chat header menu) stays open: each
 * Escape closes only the innermost layer, and focus returns to `trigger`.
 *
 * A layer that sits in the page flow passes `outside: "click"`: closing it
 * moves what is below it, so it closes after the press has landed on its
 * target, never under the pointer mid-press.
 */
export function useLayerDismiss(
  open: boolean,
  close: () => void,
  inside: readonly RefObject<HTMLElement | null>[],
  trigger: RefObject<HTMLElement | null>,
  outside: "pointerdown" | "click" = "pointerdown",
) {
  useEffect(() => {
    if (!open) return;
    const pressOutside = (event: Event) => {
      const target = event.target as Node;
      if (!inside.some((ref) => ref.current?.contains(target))) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      close();
      trigger.current?.focus();
    };
    document.addEventListener(outside, pressOutside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener(outside, pressOutside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open, close, inside, trigger, outside]);
}
