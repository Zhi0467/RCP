import { parseProjectHash } from "../experiments/experimentBoardModel.ts";

/** What went wrong on screen; the backend writes each one to its log. */
export type ClientEventKind = "notice" | "window_error" | "unhandled_rejection" | "render_failure";

type Send = (url: string, init: RequestInit) => Promise<unknown>;

// The backend cuts to the same length; cutting here keeps the request small.
const MESSAGE_MAX_CHARS = 500;

/**
 * Best-effort and silent. It bypasses `api()` so a failed send cannot show a
 * notice, mark the backend offline, or report itself, and it never throws:
 * a throw inside a window error listener would raise another error event.
 */
export function reportClientEvent(
  kind: ClientEventKind,
  message: string,
  send: Send = (url, init) => fetch(url, init),
): void {
  try {
    const route = parseProjectHash(globalThis.location?.hash ?? "");
    // A view name only: the hash itself can carry ids and sign-in codes.
    const view = route.projectId ? route.view : "index";
    void send("/api/client-events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      keepalive: true,
      body: JSON.stringify({ kind, message: message.slice(0, MESSAGE_MAX_CHARS), view }),
    }).catch(() => undefined);
  } catch {
    // Logging must never become a failure of its own.
  }
}

function describe(value: unknown): string {
  if (value instanceof Error) return `${value.name}: ${value.message}`;
  return String(value);
}

export function installClientEventReporting(
  target: Pick<EventTarget, "addEventListener"> = window,
  send?: Send,
): void {
  target.addEventListener("error", (event) => {
    const error = event as ErrorEvent;
    reportClientEvent("window_error", error.error ? describe(error.error) : error.message, send);
  });
  target.addEventListener("unhandledrejection", (event) => {
    reportClientEvent(
      "unhandled_rejection",
      describe((event as PromiseRejectionEvent).reason),
      send,
    );
  });
}
