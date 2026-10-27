import { useSyncExternalStore } from "react";

/**
 * The chat composer's text, held outside the chat's React state.
 *
 * A keystroke re-renders only the components that read the draft, never the
 * transcript above it; event handlers read it with `get()` at the moment they run.
 */
export interface ComposerDraft {
  get(): string;
  set(next: string | ((current: string) => string)): void;
  subscribe(listener: () => void): () => void;
}

export function createComposerDraft(initial: string): ComposerDraft {
  let text = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => text,
    set(next) {
      const value = typeof next === "function" ? next(text) : next;
      if (value === text) return;
      text = value;
      listeners.forEach((listener) => listener());
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** Re-render when `select(text)` changes; `select` must return a primitive. */
export function useComposerDraft<T = string>(
  draft: ComposerDraft,
  select: (text: string) => T = (text) => text as T,
): T {
  const snapshot = () => select(draft.get());
  return useSyncExternalStore(draft.subscribe, snapshot, snapshot);
}
