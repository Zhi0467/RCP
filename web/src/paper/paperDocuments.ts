import type { BackendIdentityEventDetail } from "../core/desktopRuntime";
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
export function vectorCovers(ack: Uint8Array, local: Uint8Array): boolean {
  const saved = Y.decodeStateVector(ack);
  return [...Y.decodeStateVector(local)].every(
    ([client, clock]) => (saved.get(client) ?? 0) >= clock,
  );
}
const owners = new Map<string, PaperDocument>();

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
  private rejected = false;
  private recovering = false;
  private ack: Uint8Array | null = null;
  private awaitingAck = true;
  private listeners = new Set<() => void>();
  private references = 0;
  private localPresence: Record<string, unknown> | null = null;
  private disposal: ReturnType<typeof setTimeout> | undefined;
  private disposed = false;
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
    this.provider.messageHandlers[100] = (_encoder, decoder) => {
      this.ack = readTailAsUint8Array(decoder);
      this.awaitingAck = false;
      this.emit();
    };
    this.doc.on("update", (_update: Uint8Array, origin: unknown) => {
      if (origin !== this.provider) this.awaitingAck = true;
      this.emit();
    });
    this.provider.on("status", ({ status }) => {
      this.snapshot = { ...this.snapshot, connected: status === "connected" };
      this.emit();
    });
    this.provider.on("closed", ({ code }) => {
      this.rejected = code === 4413 || code === 4400;
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
      this.persistence ??= new IndexeddbPersistence(
        `rcp:paper:${paperDocumentKey(this.key)}`,
        this.doc,
      );
      await this.persistence.whenSynced;
      if (this.disposed) return;
      this.snapshot = { ...this.snapshot, ready: true, error: null };
      this.provider.connect();
    } catch (error) {
      this.snapshot = { ...this.snapshot, error: String(error) };
    } finally {
      this.storageStarting = false;
      this.emit();
    }
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
      saved:
        !this.awaitingAck &&
        this.ack !== null &&
        vectorCovers(this.ack, Y.encodeStateVector(this.doc)),
    };
    this.listeners.forEach((listener) => listener());
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
    if (this.snapshot.ready) this.provider.connect();
    else void this.startStorage();
    this.emit();
  }
  destroy() {
    if (this.disposed) return;
    this.disposed = true;
    clearTimeout(this.disposal);
    this.provider.destroy();
    void this.persistence?.destroy();
    this.doc.destroy();
    owners.delete(paperDocumentKey(this.key));
  }
}
export function acquirePaperDocument(key: PaperDocumentKey, apiBase: string, name: string) {
  const id = paperDocumentKey(key);
  let owner = owners.get(id);
  if (!owner) {
    owner = new PaperDocument(key, apiBase, name);
    owners.set(id, owner);
  }
  return owner;
}
export function disconnectPaperDocuments() {
  for (const owner of [...owners.values()]) owner.destroy();
}
// Reverification also fires after transport recovery. Only an observed identity
// change invalidates document owners; a failed probe has no new identity.
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
  if (changed) disconnectPaperDocuments();
  return changed;
}
if (typeof window !== "undefined") {
  window.addEventListener("rcp:session-required", disconnectPaperDocuments);
  window.addEventListener("rcp:read-access-lost", disconnectPaperDocuments);
  window.addEventListener("rcp:backend-identity", (event) => {
    if (!observePaperBackend((event as CustomEvent<BackendIdentityEventDetail>).detail)) return;
    window.dispatchEvent(new Event("rcp:paper-backend-changed"));
  });
}
