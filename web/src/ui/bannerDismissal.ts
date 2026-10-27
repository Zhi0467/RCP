// Every banner's dismissal. A key names one occurrence of its condition, so a
// new failure, release, or sign-out shows again; session memory still hides a
// banner when storage is unavailable.
const dismissed = new Set<string>();
const storageKey = (key: string) => `rcp:${key}`;

export function isBannerDismissed(key: string): boolean {
  if (dismissed.has(key)) return true;
  try {
    return localStorage.getItem(storageKey(key)) === "1";
  } catch {
    return false;
  }
}

export function dismissBanner(key: string): void {
  dismissed.add(key);
  try {
    localStorage.setItem(storageKey(key), "1");
  } catch {
    /* Session memory still hides it on this page. */
  }
}
