import type { DesktopBuildIdentity } from "../desktopRuntime";
import type { UpdateNotice } from "../types";

export function releaseNotice(notice: UpdateNotice | null, identity: DesktopBuildIdentity | null) {
  if (!notice || !notice.latest_version) return null;
  // A source app can run an older native shell than its updated checkout, so a
  // backend that is current says nothing about the app: compare the app itself.
  // Only a checkout on the release commit qualifies: one past it keeps the same
  // base version and must never be offered a downgrade.
  const sourceApp = notice.space === "personal" && identity?.kind === "source";
  const checkoutAtRelease = notice.status === "current" && notice.source_at_release;
  if (notice.status !== "update_available" && !(sourceApp && checkoutAtRelease)) return null;
  const current =
    notice.space === "personal" && identity ? identity.version : notice.current_version;
  if (
    !current ||
    !/^\d+\.\d+\.\d+$/.test(current) ||
    !/^\d+\.\d+\.\d+$/.test(notice.latest_version)
  )
    return null;
  const installed = current.split(".").map(Number);
  const latest = notice.latest_version.split(".").map(Number);
  const difference = latest
    .map((part, index) => part - installed[index])
    .find((part) => part !== 0);
  if (!difference || difference < 0) return null;
  const kind =
    notice.space === "team" ? "team" : identity?.kind === "prebuilt" ? "prebuilt" : "source";
  const download = kind === "prebuilt" && notice.companion_ready ? notice.download_url : null;
  return {
    kind,
    current,
    release: notice.latest_version,
    // Dismissing a prebuilt notice before its download exists must not hide the
    // later Download action for the same release.
    // Each installation owns its own dismissal: a team server and this app update
    // separately even for the same release.
    dismissKey: `${kind}:${notice.latest_version}${kind === "prebuilt" && !download ? ":pending" : ""}`,
    command:
      kind === "prebuilt"
        ? null
        : sourceApp
          ? `${notice.update_command ?? `scripts/update-from-source v${notice.latest_version}`} --desktop`
          : notice.update_command,
    download,
  };
}

const dismissed = new Set<string>();
const dismissalKey = (release: string) => `rcp:update-dismissed:${release}`;
export function isReleaseDismissed(release: string): boolean {
  if (dismissed.has(release)) return true;
  try {
    return localStorage.getItem(dismissalKey(release)) === "1";
  } catch {
    return false;
  }
}
export function dismissRelease(release: string): void {
  dismissed.add(release);
  try {
    localStorage.setItem(dismissalKey(release), "1");
  } catch {
    /* Session memory remains available. */
  }
}
