/**
 * A desktop space switch replaces this document, and WebKit rejects the page's
 * in-flight requests as network failures while it is still on screen; no event
 * announces that cross-origin exit, so its initiator calls beginPageExit. Such a
 * failure is held while the exit is under way: if the page leaves, nothing is left
 * to tell. If the page is still here when the exit window closes, it did not
 * leave, and each held failure surfaces then.
 */
const PAGE_EXIT_WINDOW_MS = 10_000;

let exit: { closed: Promise<void>; close: () => void } | null = null;
let exitTimer: ReturnType<typeof setTimeout> | undefined;

export function beginPageExit(): void {
  if (!exit) {
    let close = () => {};
    const closed = new Promise<void>((resolve) => {
      close = resolve;
    });
    exit = { closed, close };
  }
  clearTimeout(exitTimer);
  exitTimer = setTimeout(endPageExit, PAGE_EXIT_WINDOW_MS);
}

/** The navigation was refused, so the page stays and held failures surface now. */
export function endPageExit(): void {
  clearTimeout(exitTimer);
  const current = exit;
  exit = null;
  current?.close();
}

/** Resolves now, or, for a fetch failure during this page's exit, once the page stays. */
export function pageStays(error: unknown): Promise<void> {
  return error instanceof TypeError && exit ? exit.closed : Promise.resolve();
}
