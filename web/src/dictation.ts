/** Pure composer rules for dictation through macOS or a member's service connection. */

/** The draft range one dictation session may rewrite. */
export interface DictationSpan {
  sessionId: string;
  start: number;
  end: number;
}

/** The first of the service's accepted formats this browser can record, in service order. */
export function chooseRecordingFormat(
  formats: readonly string[],
  isTypeSupported: (type: string) => boolean,
): string | null {
  return formats.find((format) => isTypeSupported(format)) ?? null;
}

/** The span a result for `sessionId` may still write; null once typing invalidated it. */
export function liveDictationSpan(
  active: DictationSpan | null,
  sessionId: string,
): DictationSpan | null {
  return active?.sessionId === sessionId ? active : null;
}

const SERVICE_FAILURES: Record<string, string> = {
  connection_not_found: "That connection is no longer connected; choose another in Space settings.",
  connection_check_failed: "The service did not accept the check.",
  audio_too_large: "The recording is too large to transcribe.",
  audio_type_unsupported: "The service does not accept this recording's format.",
  transcription_busy: "Another transcription is still running; try again in a moment.",
  transcription_upstream_failed: "The transcription service failed.",
  address_not_allowed: "Use an https address, or http only on the RCP server itself.",
};

/**
 * One sentence for a coded service-connection failure, or null when the failure
 * carries no route code. The route's message is already sanitized of the key.
 */
export function serviceConnectionFailure(failure: unknown): string | null {
  if (!(failure instanceof Error)) return null;
  let detail: unknown;
  try {
    detail = JSON.parse(failure.message);
  } catch {
    return null;
  }
  if (!detail || typeof detail !== "object") return null;
  const { code, message } = detail as { code?: unknown; message?: unknown };
  if (typeof code !== "string") return null;
  const reported = typeof message === "string" && message !== code ? message : "";
  return [SERVICE_FAILURES[code], reported].filter(Boolean).join(" ") || code;
}
