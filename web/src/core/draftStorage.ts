/**
 * Unsent drafts in browser storage belong to the member who typed them. Every
 * draft key is built here from the signed-in member's user id, so a different
 * member on the same browser never sees or submits another member's draft.
 * Without a known member there is no key: nothing is restored or saved.
 */
export function memberDraftKey(actorId: string | null, draftKey: string): string | null {
  return actorId ? `rcp:member-draft:${encodeURIComponent(actorId)}:${draftKey}` : null;
}

/** Draft keys written before drafts were member-scoped; their owner is unknown. */
const UNSCOPED_DRAFT_PREFIXES = [
  "rcp:human-draft:",
  "rcp:chat-draft:",
  "rcp:chat-inputs:",
  "rcp:chat-annotations:",
  "rcp:settings-draft:",
];

/** Delete unscoped drafts rather than hand them to whoever signs in next. */
export function dropUnscopedDrafts(storage: Pick<Storage, "length" | "key" | "removeItem">): void {
  for (let index = storage.length - 1; index >= 0; index -= 1) {
    const key = storage.key(index);
    if (key && UNSCOPED_DRAFT_PREFIXES.some((prefix) => key.startsWith(prefix))) {
      storage.removeItem(key);
    }
  }
}
