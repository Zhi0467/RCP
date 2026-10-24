import type {
  ProviderCredentialSummary,
  ProviderLoginAccount,
  ProviderLoginState,
  ProviderSignInStatus,
  ProviderResumeSummary,
} from "../core/types";

const DAY_MS = 24 * 60 * 60 * 1000;

/** The execution account as a human names it, with the project machines that use it. */
export function accountLabel(
  account: Pick<ProviderLoginAccount, "host" | "machines">,
  spaceKind: "personal" | "team",
): string {
  const where = account.host || (spaceKind === "team" ? "Team server" : "Local machine");
  const aliases = account.machines.filter((alias) => alias !== account.host);
  return aliases.length ? `${where} (${aliases.join(", ")})` : where;
}

/** How a stored credential stands; any expiry is an estimate. */
export function tokenNote(token: ProviderCredentialSummary, now: Date): string {
  if (!token.estimated_expiry_at)
    return `Token saved by ${token.pasted_by}, ${token.verified_at ? "verified" : "not verified yet"}.`;
  const expiry = new Date(token.estimated_expiry_at);
  const daysLeft = Math.floor((expiry.getTime() - now.getTime()) / DAY_MS);
  const verified = token.verified_at ? "verified" : "not verified yet";
  if (daysLeft < 0) {
    return `Setup token saved by ${token.pasted_by}, ${verified}; its estimated lifetime ended. Paste a new one if turns fail.`;
  }
  return `Setup token saved by ${token.pasted_by}, ${verified}; expected to last about ${daysLeft} more days.`;
}

/** The one line a member reads while a device-code sign-in runs or after it ends. */
export function signInNote(status: ProviderSignInStatus): string {
  if (status.state === "succeeded") return "Signed in and verified with one authenticated request.";
  if (status.state === "failed")
    return status.detail ? `Sign-in failed: ${status.detail}` : "Sign-in failed.";
  if (status.user_code && status.verification_url) {
    return "Open the link below, sign in there, and enter this code. This page finishes on its own.";
  }
  return "Asking the provider for a device code…";
}

/** One sentence that says what a signed-out account needs, without naming internals. */
export function signedOutNote(state: {
  label?: string;
  provider: string;
  host: string;
  detail: string | null;
}): string {
  const reason = state.detail ? ` ${state.detail.replace(/\.*$/, "")}.` : "";
  return `${signedOutSummary(state)}.${reason}`;
}

/** The one-line form a phone shows until the notice is expanded. */
export function signedOutSummary(state: {
  label?: string;
  provider: string;
  host: string;
}): string {
  const named = state.label ?? state.provider;
  const provider = named.charAt(0).toUpperCase() + named.slice(1);
  return `${provider} is signed out${state.host ? ` on ${state.host}` : ""}`;
}

export function resumedNote(resumed: ProviderResumeSummary): string {
  return resumed.checked
    ? `Verified. Rechecked ${resumed.checked} ${resumed.checked === 1 ? "item" : "items"} for resumption.`
    : "Verified.";
}

type SignOut = Pick<ProviderLoginState, "provider" | "host" | "generation">;

// One sign-out of one account. Only a sign-in or sign-out starts a new
// generation; another failure of the same sign-out keeps it, so a dismissal holds.
export const signOutKey = (state: SignOut) => `${state.provider}:${state.host}:${state.generation}`;
const dismissalKey = (state: SignOut) => `rcp:login-notice-dismissed:${signOutKey(state)}`;
const dismissed = new Set<string>();

export function isLoginNoticeDismissed(state: SignOut): boolean {
  const key = dismissalKey(state);
  if (dismissed.has(key)) return true;
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

export function dismissLoginNotice(state: SignOut): void {
  const key = dismissalKey(state);
  dismissed.add(key);
  try {
    localStorage.setItem(key, "1");
  } catch {
    /* Session memory still hides it on this page. */
  }
}
