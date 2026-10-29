/**
 * Per-viewer conveniences in browser storage, such as a width or a remembered
 * view. Storage can be blocked or empty, so a read falls back to null and a
 * failed write is dropped: nothing here may affect what the page can do.
 */
export function readPref(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** Stores `value` under `key`, or removes the key for null. */
export function writePref(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* A preference is a convenience. */
  }
}
