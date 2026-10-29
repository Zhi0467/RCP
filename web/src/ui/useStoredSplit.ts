import { useState } from "react";
import { readPref, writePref } from "../core/prefStorage";

const readWidth = (key: string) => {
  const value = Number(readPref(key));
  return value > 0 ? value : null;
};

/**
 * A dragged width kept per browser under `key`. The viewer's choice is stored as
 * dragged and only clamped for display, so a narrow window never overwrites it.
 * `chosen` is false until the viewer drags, and `width` then shows `fallback`.
 */
export function useStoredSplit(
  key: string,
  bounds: { minimum: number; maximum: number },
  fallback: number,
) {
  const [choice, setChoice] = useState(() => ({ key, value: readWidth(key) }));
  // Another key, such as another project's, reads its own stored choice.
  const value = choice.key === key ? choice.value : readWidth(key);
  return {
    chosen: value !== null,
    width: Math.min(bounds.maximum, Math.max(bounds.minimum, value ?? fallback)),
    resize: (next: number) => {
      setChoice({ key, value: next });
      writePref(key, String(Math.round(next)));
    },
  };
}
