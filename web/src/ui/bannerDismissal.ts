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

// A cleared condition forgets its dismissals, so its next occurrence shows.
export function forgetBanners(prefix: string): void {
  for (const key of dismissed) if (key.startsWith(prefix)) dismissed.delete(key);
  try {
    for (let index = localStorage.length - 1; index >= 0; index--) {
      const key = localStorage.key(index);
      if (key?.startsWith(storageKey(prefix))) localStorage.removeItem(key);
    }
  } catch {
    /* Nothing persisted to forget. */
  }
}
