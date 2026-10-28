import type { BackendIdentityEventDetail } from "../core/desktopRuntime";
import type { PaperFile } from "../core/types";
import * as Y from "yjs";
import { WebsocketProvider } from "y-websocket";
import { IndexeddbPersistence } from "y-indexeddb";
import { readTailAsUint8Array } from "lib0/decoding";

export interface PaperDocumentKey {
  backend: string;
  member: string;
  project: string;
  file: string;
}
export interface PaperPresence {
  id: number;
  name: string;
  color: string;
}
export interface PaperDocumentState {
  text: string;
  saved: boolean;
  ready: boolean;
  connected: boolean;
  error: string | null;
  /** The server's final close code; the editor is read-only until Reconnect. */
  closed: number | null;
  presence: PaperPresence[];
}
export function paperDocumentKey(key: PaperDocumentKey): string {
  return JSON.stringify([key.backend, key.member, key.project, key.file]);
}
/** The backend part of a document key: the server origin and the space it serves. */
export function paperBackendKey(apiBase: string, spaceId: string): string {
  return `${new URL(apiBase, window.location.href).origin}:${spaceId}`;
}
export function vectorCovers(ack: Uint8Array, local: Uint8Array): boolean {
  const saved = Y.decodeStateVector(ack);
  return [...Y.decodeStateVector(local)].every(
    ([client, clock]) => (saved.get(client) ?? 0) >= clock,
  );
}
const owners = new Map<string, PaperDocument>();
/** Owners whose local text the server has not acknowledged, so a view can reach a file
 * that left the tree while it still holds unsent text. */
let unsent: readonly PaperDocument[] = [];
const unsentListeners = new Set<() => void>();
let unsentDirty = false;
// Owners are acquired while a view renders, so the list settles after it, never during.
function refreshUnsent() {
  if (unsentDirty) return;
  unsentDirty = true;
  queueMicrotask(() => {
    unsentDirty = false;
    const next = [...owners.values()].filter((owner) => !owner.getSnapshot().saved);
    if (next.length === unsent.length && next.every((owner, index) => owner === unsent[index]))
      return;
    unsent = next;
    unsentListeners.forEach((listener) => listener());
  });
}
// The files of one member's project that hold unsent text, kept beside their IndexedDB
// stores so a file deleted meanwhile is still reachable after a reload.
type PaperProjectKey = Omit<PaperDocumentKey, "file">;
function inventoryName(key: PaperProjectKey) {
  return `rcp:paper-unsent:${JSON.stringify([key.backend, key.member, key.project])}`;
}
function readInventory(key: PaperProjectKey): Record<string, PaperFile> {
  try {
    return JSON.parse(localStorage.getItem(inventoryName(key)) ?? "{}") as Record<
      string,
      PaperFile
    >;
  } catch {
    return {};
  }
}
function updateInventory(key: PaperDocumentKey, listing: PaperFile | null) {
  const entries = readInventory(key);
  if (listing) entries[key.file] = listing;
  else delete entries[key.file];
  try {
    if (Object.keys(entries).length)
      localStorage.setItem(inventoryName(key), JSON.stringify(entries));
    else localStorage.removeItem(inventoryName(key));
  } catch {
    // Without browser storage only the documents open in this page stay reachable.
  }
}
/** Reopens this project's files that held unsent text and are no longer listed. */
export function recoverUnsentPaperDocuments(
  key: PaperProjectKey,
  listed: ReadonlySet<string>,
  apiBase: string,
  name: string,
) {
  for (const [file, listing] of Object.entries(readInventory(key))) {
    if (listed.has(file)) continue;
    acquirePaperDocument({ ...key, file }, apiBase, name).listing ??= listing;
  }
}
export const unsentPaperDocuments = {
  subscribe(listener: () => void) {
    unsentListeners.add(listener);
    return () => {
      unsentListeners.delete(listener);
    };
  },
  getSnapshot: () => unsent,
};

/** Delete sets do not advance a Yjs state vector. Give a local delete-only edit
 * a causal clock so an older in-flight type-100 ack cannot cover it. S1 persists
 * the whole Y.Doc; this map never enters the file's text or published tree. */
export function trackDeletionClock(doc: Y.Doc, text: Y.Text) {
  const clock = doc.getMap<number>("rcp_durability");
  doc.on("afterTransaction", (transaction: Y.Transaction) => {
    if (
      transaction.local &&
      [...transaction.changed.keys()].some((type: unknown) => type === text) &&
      transaction.deleteSet.clients.size > 0 &&
      transaction.beforeState.get(doc.clientID) === transaction.afterState.get(doc.clientID)
    ) {
      clock.set("deletion", (clock.get("deletion") ?? 0) + 1);
    }
  });
}

function checkLocalStorage(): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("rcp:paper-storage-check");
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("Paper local storage is blocked"));
    request.onsuccess = () => {
      request.result.close();
      resolve();
    };
  });
}

/** Owns the document independently of the editor and its route lifetime. */
export class PaperDocument {
  doc = new Y.Doc();
  text = this.doc.getText("text");
  private persistence: IndexeddbPersistence | null = null;
  provider!: WebsocketProvider;
  /** The file as last listed in the tree, kept to show it after it leaves the tree. */
  listing: PaperFile | null = null;
  private rejected = false;
  /** Whether the current connection received anything: a close before that is admission,
   * never a refusal of this document's text. */
  private heard = false;
  /** Disconnected until the member is verified again after a backend change. */
  private paused = false;
  private recovering = false;
  private ack: Uint8Array | null = null;
  /** The state vector after the latest local change. Content the server delivered is
   * already durable there, so only local content waits for an acknowledgement. */
  private local: Uint8Array | null = null;
  private awaitingAck = true;
  private listeners = new Set<() => void>();
  private references = 0;
  private localPresence: Record<string, unknown> | null = null;
  private disposal: ReturnType<typeof setTimeout> | undefined;
  private disposed = false;
  private discarded = false;
  private inventoried = false;
  private storageStarting = false;
  private snapshot: PaperDocumentState = {
    text: "",
    saved: false,
    ready: false,
    connected: false,
    error: null,
    closed: null,
    presence: [],
  };

  constructor(
    readonly key: PaperDocumentKey,
    private readonly apiBase: string,
    readonly name: string,
  ) {
    this.initializeDocument();
    void this.startStorage();
  }
  private initializeDocument() {
    trackDeletionClock(this.doc, this.text);
    const url = new URL(
      `${this.apiBase}/paper/files/${encodeURIComponent(this.key.file)}`,
      window.location.href,
    );
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    this.provider = new WebsocketProvider(url.href, "live", this.doc, {
      connect: false,
      // S1 acknowledges the sender. A sync request also obtains an ack covering
      // updates received from other members, without treating broadcast as Saved.
      resyncInterval: 5_000,
      // Identity-isolated persistence and server admission, never a cross-member tab channel.
      disableBc: true,
    });
    const handlers = this.provider.messageHandlers;
    for (const type of [0, 1]) {
      const handle = handlers[type];
      handlers[type] = (...message) => {
        this.heard = true;
        handle(...message);
      };
    }
    handlers[100] = (_encoder, decoder) => {
      this.heard = true;
      this.ack = readTailAsUint8Array(decoder);
      this.awaitingAck = false;
      this.emit();
    };
    // Send each synchronous burst of local transactions as one message: a delete and the
    // deletion clock it bumps travel together instead of doubling the message rate.
    const send = this.provider._updateHandler;
    this.doc.off("update", send);
    let burst: Uint8Array[] = [];
    this.doc.on("update", (update: Uint8Array, origin: unknown) => {
      if (origin === this.provider) return;
      burst.push(update);
      if (burst.length > 1) return;
      queueMicrotask(() => {
        const updates = burst;
        burst = [];
        send(updates.length === 1 ? updates[0] : Y.mergeUpdates(updates), null);
      });
    });
    this.doc.on("update", (_update: Uint8Array, origin: unknown) => {
      if (origin !== this.provider) {
        this.awaitingAck = true;
        this.local = Y.encodeStateVector(this.doc);
      }
      this.emit();
    });
    this.provider.on("status", ({ status }) => {
      if (status === "connecting") this.heard = false;
      this.snapshot = { ...this.snapshot, connected: status === "connected" };
      this.emit();
    });
    this.provider.on("closed", ({ code }) => {
      this.rejected = (code === 4413 || code === 4400) && this.heard && !this.isSaved();
      this.snapshot = {
        ...this.snapshot,
        error: `Paper connection closed (${code})`,
        closed: code,
      };
      this.emit();
    });
    this.provider.awareness.on("change", () => this.emit());
  }
  private async startStorage() {
    if (this.storageStarting || this.disposed) return;
    this.storageStarting = true;
    try {
      // y-indexeddb exposes no rejected whenSynced promise. Check admission
      // before constructing it so denied storage has an explicit retry path.
      await checkLocalStorage();
      if (this.disposed) return;
      await (this.persistence?.whenSynced ?? this.openPersistence());
      if (this.disposed) return;
      this.snapshot = { ...this.snapshot, ready: true, error: null };
      if (!this.paused) this.provider.connect();
    } catch (error) {
      this.snapshot = { ...this.snapshot, error: String(error) };
    } finally {
      this.storageStarting = false;
      this.emit();
    }
  }
  private get storageName() {
    return `rcp:paper:${paperDocumentKey(this.key)}`;
  }
  private async openPersistence() {
    const persistence = new IndexeddbPersistence(this.storageName, this.doc);
    this.persistence = persistence;
    await persistence.whenSynced;
    // Another tab of this member deletes the store when it discards a refused update.
    // A new store opened here starts with this whole document, so nothing unsent is lost.
    persistence.db?.addEventListener("versionchange", () => {
      if (this.persistence !== persistence || this.disposed) return;
      void persistence.destroy();
      this.openPersistence().catch((error) => {
        this.snapshot = { ...this.snapshot, error: String(error) };
        this.emit();
      });
    });
  }
  private isSaved() {
    return (
      !this.awaitingAck &&
      this.ack !== null &&
      (this.local === null || vectorCovers(this.ack, this.local))
    );
  }
  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private emit() {
    if (this.disposed) return;
    const presence: PaperPresence[] = [];
    this.provider.awareness.getStates().forEach((state, id) => {
      if (typeof state.user?.name === "string")
        presence.push({
          id,
          name: state.user.name,
          color: /^#[0-9a-f]{6}$/i.test(state.user.color) ? state.user.color : "#386792",
        });
    });
    this.snapshot = {
      ...this.snapshot,
      text: this.text.toString(),
      presence,
      saved: this.isSaved(),
    };
    const keep = !this.snapshot.saved && this.listing !== null && !this.discarded;
    if (keep !== this.inventoried) {
      this.inventoried = keep;
      updateInventory(this.key, keep ? this.listing : null);
    }
    this.listeners.forEach((listener) => listener());
    refreshUnsent();
    this.scheduleDisposal();
  }
  retain() {
    this.references++;
    clearTimeout(this.disposal);
    this.disposal = undefined;
    const colors = ["#386792", "#ba731e", "#785a7c", "#326d72"];
    const hash = [...this.key.member].reduce((sum, char) => sum + char.charCodeAt(0), 0);
    const color = colors[hash % colors.length];
    this.provider.awareness.setLocalState({
      ...this.localPresence,
      ...this.provider.awareness.getLocalState(),
      user: { name: this.name, color, colorLight: `${color}33` },
    });
    return () => {
      this.references--;
      if (!this.references) {
        this.localPresence = this.provider.awareness.getLocalState();
        this.provider.awareness.setLocalState(null);
        if (this.disposed) this.closeStorage();
      }
      this.scheduleDisposal();
    };
  }
  private scheduleDisposal() {
    if (this.references || !this.snapshot.saved || this.disposal) return;
    this.disposal = setTimeout(() => this.destroy(), 30_000);
  }
  async reconnect() {
    if (this.recovering) return;
    if (this.rejected) {
      this.recovering = true;
      this.snapshot = {
        ...this.snapshot,
        ready: false,
        connected: false,
        error: null,
        closed: null,
      };
      this.emit();
      try {
        const presence = this.provider.awareness.getLocalState();
        this.provider.destroy();
        await this.persistence?.destroy();
        // Await deletion: y-indexeddb.clearData does not await deleteDB itself.
        await new Promise<void>((resolve, reject) => {
          const request = indexedDB.deleteDatabase(`rcp:paper:${paperDocumentKey(this.key)}`);
          request.onsuccess = () => resolve();
          request.onerror = () => reject(request.error);
          request.onblocked = () => reject(new Error("Paper local storage is blocked"));
        });
        if (this.disposed) return;
        this.persistence = null;
        this.doc.destroy();
        this.doc = new Y.Doc();
        this.text = this.doc.getText("text");
        this.ack = null;
        this.local = null;
        this.awaitingAck = true;
        this.initializeDocument();
        this.provider.awareness.setLocalState(presence);
        this.rejected = false;
        await this.startStorage();
      } catch (error) {
        this.snapshot = { ...this.snapshot, error: String(error) };
      } finally {
        this.recovering = false;
        this.emit();
      }
      return;
    }
    this.snapshot = { ...this.snapshot, error: null, closed: null };
    if (!this.snapshot.ready) void this.startStorage();
    else if (!this.paused) this.provider.connect();
    this.emit();
  }
  /** Holds the connection, keeping the document editable, until `resume`. */
  pause() {
    if (this.disposed || this.paused) return;
    this.paused = true;
    this.provider.disconnect();
    // Only the server that is reached next can say what it holds.
    this.awaitingAck = true;
    this.emit();
  }
  resume() {
    if (this.disposed || !this.paused) return;
    this.paused = false;
    if (this.snapshot.ready && this.snapshot.closed === null) this.provider.connect();
  }
  /** Cuts the server link at once. A mounted editor still writes into this doc
   * until it unmounts, so local storage stays attached until the last release. */
  destroy() {
    if (this.disposed) return;
    this.disposed = true;
    clearTimeout(this.disposal);
    this.provider.destroy();
    owners.delete(paperDocumentKey(this.key));
    refreshUnsent();
    if (!this.references) this.closeStorage();
  }
  /** Ends a document whose text now lives elsewhere, deleting its local store. */
  discard() {
    this.discarded = true;
    this.inventoried = false;
    updateInventory(this.key, null);
    this.destroy();
  }
  private closeStorage() {
    const closed = this.persistence?.destroy();
    this.doc.destroy();
    if (this.discarded)
      void Promise.resolve(closed).then(() => indexedDB.deleteDatabase(this.storageName));
  }
}
export function acquirePaperDocument(key: PaperDocumentKey, apiBase: string, name: string) {
  const id = paperDocumentKey(key);
  let owner = owners.get(id);
  if (!owner) {
    owner = new PaperDocument(key, apiBase, name);
    owners.set(id, owner);
    refreshUnsent();
  }
  return owner;
}
/** A backend change or an ended session holds every document, editable, until the
 * member is verified again: nothing typed meanwhile is lost or sent as someone else. */
export function pausePaperDocuments() {
  for (const owner of owners.values()) owner.pause();
}
/** Resumes the verified member's documents and ends every other member's. */
export function resumePaperDocuments(backend: string, member: string) {
  for (const owner of [...owners.values()]) {
    if (owner.key.backend === backend && owner.key.member === member) owner.resume();
    else owner.destroy();
  }
}
// Reverification also fires after transport recovery. Only an observed identity
// change pauses document owners; a failed probe has no new identity.
export function paperBackendIdentityTracker() {
  let previous: string | null = null;
  return (detail: BackendIdentityEventDetail) => {
    if (!detail.health) return false;
    const { instance_id, data_dir_id, version } = detail.health;
    const next = JSON.stringify([instance_id, data_dir_id, version]);
    const changed = previous !== null && previous !== next;
    previous = next;
    return changed;
  };
}
const backendChanged = paperBackendIdentityTracker();
export function observePaperBackend(detail: BackendIdentityEventDetail) {
  const changed = backendChanged(detail);
  if (changed) pausePaperDocuments();
  return changed;
}
// Lost project access needs nothing here: the server closes those documents with 4403,
// and they keep their unsent text.
if (typeof window !== "undefined") {
  window.addEventListener("rcp:session-required", pausePaperDocuments);
  window.addEventListener("rcp:backend-identity", (event) => {
    if (!observePaperBackend((event as CustomEvent<BackendIdentityEventDetail>).detail)) return;
    window.dispatchEvent(new Event("rcp:paper-backend-changed"));
  });
}
