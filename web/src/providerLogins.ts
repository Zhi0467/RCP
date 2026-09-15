import type {
  ProviderCredentialSummary,
  ProviderLoginAccount,
  ProviderSignInStatus,
  ProviderResumeSummary,
} from "./types";

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
    return `Open the link, sign in, and enter the code.`;
  }
  return "Starting sign-in and waiting for its device code…";
}

export function resumedNote(resumed: ProviderResumeSummary): string {
  return resumed.checked
    ? `Verified. Rechecked ${resumed.checked} ${resumed.checked === 1 ? "item" : "items"} for resumption.`
    : "Verified.";
}
