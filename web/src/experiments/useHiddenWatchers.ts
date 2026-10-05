import { useState, useSyncExternalStore } from "react";
import type { WatcherRecord } from "../types";

const changeEvent = "rcp:hidden-watchers-changed";

function subscribe(onChange: () => void) {
  window.addEventListener("storage", onChange);
  window.addEventListener(changeEvent, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(changeEvent, onChange);
  };
}

function read(key: string): string {
  try {
    return localStorage.getItem(key) ?? "[]";
  } catch {
    return "[]";
  }
}

function parse(value: string): Set<string> {
  try {
    const ids: unknown = JSON.parse(value);
    return new Set(Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

export function useHiddenWatchers(apiBase: string) {
  const key = `rcp:hidden-watchers:${apiBase}`;
  const stored = useSyncExternalStore(
    subscribe,
    () => read(key),
    () => "[]",
  );
  const hiddenIds = parse(stored);
  const [error, setError] = useState<string | null>(null);

  const save = (update: (ids: Set<string>) => void) => {
    const ids = parse(read(key));
    update(ids);
    try {
      localStorage.setItem(key, JSON.stringify([...ids]));
      setError(null);
      window.dispatchEvent(new Event(changeEvent));
    } catch {
      setError("Could not save watcher visibility on this device.");
    }
  };

  return {
    error,
    isHidden: (watcher: WatcherRecord) =>
      watcher.status === "completed" && hiddenIds.has(watcher.watcher_id),
    hide: (watcherId: string) => save((ids) => ids.add(watcherId)),
    show: (watcherIds: string[]) => save((ids) => watcherIds.forEach((id) => ids.delete(id))),
  };
}
