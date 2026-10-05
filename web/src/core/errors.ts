import { ApiError } from "./api";

/** The sentence a person should read for a thrown failure.
 *
 * `String(error)` renders `Error.toString()`, which prefixes the class name and
 * leaks `ApiError: Not Found` into interface copy. Callers want the reported
 * cause on its own so they can frame it with what the failure actually blocks.
 */
export function errorMessage(failure: unknown): string {
  if (failure instanceof ApiError) {
    const detail = failure.message.trim();
    return detail || `The server answered ${failure.status} without a reason.`;
  }
  if (failure instanceof Error) {
    return failure.message.trim() || failure.name;
  }
  const text = String(failure).trim();
  return text || "The cause was not reported.";
}
