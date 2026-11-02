// One store for every backend resource a project tab shows.
//
// Each resource is held once, keyed by backend, member, project, name, and
// the ref or entity it applies to. Tabs register interest; the refresh
// scheduler (`refreshScheduler.ts`) reports watermarks and the store reads
// whatever moved. One acceptance rule covers every read: a response commits
// only if the key's lifecycle generation and the read's start sequence still
// match. A read's committed version is the watermark observed before it
// started, so data that moved during the read leaves debt for the next beat.

export interface ResourceRequest {
  name: string;
  ref?: string;
  entity?: string;
}

export interface ResourceKey extends ResourceRequest {
  backend: string;
  member: string;
  project: string;
  /** Client-side parameters that select a different read of the same name. */
  query?: string;
}

export interface ResourceProfile<T> {
  key: ResourceKey;
  /** Other watermarks whose live projections are included in this response. */
  watch?: readonly ResourceRequest[];
  fetch(signal: AbortSignal): Promise<T>;
  /** Keeps unchanged parts of the previous value so consumers keep identity. */
  reconcile?(previous: T | undefined, next: T): T;
}

export interface ResourceWatermark extends ResourceRequest {
  version: string;
}

export interface ObservationTicket {
  project: string;
  serial: number;
}

export interface PresentationTicket {
  owner: string;
  serial: number;
}

export interface ResourcePresentation {
  status: "loading" | "ready" | "error";
  error: unknown;
  /** Payloads are exposed together only after this entry is admitted. */
  views: readonly ResourceView<unknown>[];
}

interface PresentationEntry {
  ticket: PresentationTicket;
  activationKey: string;
  ids: string[];
  minimumObservation: number;
  minimumRead: number;
  proofs: Map<string, { epoch: string | null; version: string }>;
  admitted: boolean;
  error: unknown;
  view: ResourcePresentation | null;
}

interface PendingOperation {
  id: string;
  apply(value: unknown): unknown;
}

export interface ResourceView<T> {
  value: T | undefined;
  /** Epoch of the accepted value, which may lag the latest watermark. */
  epoch: string | null;
  /** No accepted read yet. */
  loading: boolean;
  /** The last read failed or the scheduler cannot reach the backend. */
  stale: boolean;
  error: unknown;
  /** Operation ids of commands still waiting on the server. */
  pending: readonly string[];
}

interface ResourceState {
  profile: ResourceProfile<unknown>;
  /** Registrations per owner; one owner may register a key more than once. */
  owners: Map<string, number>;
  /** Owners keeping a visited view cached without polling it. */
  dormant: Set<string>;
  value: unknown;
  hasValue: boolean;
  valueEpoch: string | null;
  invalidations: number;
  lastRead: number;
  waiters: { invalidation: number; resolve(): void; reject(error: unknown): void }[];
  overlays: Map<string, (value: unknown) => unknown>;
  committed: string | undefined;
  observed: string | undefined;
  /** Owes a read regardless of tokens (invalidated, new epoch, unsettled command). */
  owed: boolean;
  /** A watermark covering this key has arrived since it was registered. */
  seen: boolean;
  generation: number;
  sequence: number;
  inFlight: AbortController | null;
  error: unknown;
  failures: number;
  retryAt: number;
  pending: PendingOperation[];
  /** Start order of the newest command whose response replaced the value. */
  settledMutation: number;
  /** Accepted payload reads, so a command can tell if a read overtook it. */
  acceptedReads: number;
  acceptedReadStart: number;
  view: ResourceView<unknown> | null;
}

export const RESOURCE_READ_SLOTS = 3;
export const RESOURCE_BACKGROUND_READ_SLOTS = 1;
// A stalled read must free its slot and enter retry backoff.
export const RESOURCE_READ_TIMEOUT_MS = 30_000;
export const RESOURCE_RETRY_BACKOFF_MS = [3000, 6000, 12_000, 30_000] as const;

export function resourceKeyId(key: ResourceKey): string {
  return JSON.stringify([
    key.backend,
    key.member,
    key.project,
    key.name,
    key.ref ?? null,
    key.entity ?? null,
    key.query ?? null,
  ]);
}

function watermarkId(project: string, entry: ResourceRequest): string {
  return JSON.stringify([project, entry.name, entry.ref ?? null, entry.entity ?? null]);
}

export function retryBackoffMs(failures: number): number {
  return RESOURCE_RETRY_BACKOFF_MS[Math.min(failures, RESOURCE_RETRY_BACKOFF_MS.length) - 1] ?? 0;
}

const EMPTY_VIEW: ResourceView<never> = {
  value: undefined,
  epoch: null,
  loading: true,
  stale: false,
  error: undefined,
  pending: [],
};

const EMPTY_PRESENTATION: ResourcePresentation = {
  status: "loading",
  error: undefined,
  views: [],
};

export function presentedResource<T>(presentation: ResourcePresentation): ResourceView<T> {
  return (
    (presentation.views[0] as ResourceView<T> | undefined) ?? {
      ...EMPTY_VIEW,
      loading: presentation.status !== "error",
      stale: presentation.status === "error",
      error: presentation.error,
    }
  );
}

export class ResourceStore {
  private readonly states = new Map<string, ResourceState>();
  private readonly listeners = new Set<() => void>();
  private readonly presentations = new Map<string, PresentationEntry>();
  private presentationSerial = 0;
  private observationSerial = 0;
  private activeProject: string | null = null;
  private running = 0;
  private runningBackground = 0;
  private epoch: string | null = null;
  /**
   * Set by an attached scheduler. Then a new key waits for its first
   * watermark before reading, so the read commits a real version instead of
   * being repeated when that watermark arrives.
   */
  private requestWatermark: (() => void) | null = null;
  private mutations = 0;
  private reads = 0;
  private readonly now: () => number;

  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private notify(): void {
    for (const listener of this.listeners) listener();
  }

  private changed(state: ResourceState): void {
    state.view = null;
    this.notify();
  }

  /** Registers `owner`'s interest; the first owner schedules the first read. */
  register<T>(owner: string, profile: ResourceProfile<T>): string {
    const id = resourceKeyId(profile.key);
    let state = this.states.get(id);
    if (!state) {
      state = {
        profile: profile as ResourceProfile<unknown>,
        owners: new Map(),
        dormant: new Set(),
        value: undefined,
        hasValue: false,
        valueEpoch: null,
        invalidations: 0,
        lastRead: 0,
        waiters: [],
        overlays: new Map(),
        committed: undefined,
        observed: undefined,
        owed: false,
        seen: false,
        generation: 0,
        sequence: 0,
        inFlight: null,
        error: undefined,
        failures: 0,
        retryAt: 0,
        pending: [],
        settledMutation: 0,
        acceptedReads: 0,
        acceptedReadStart: 0,
        view: null,
      };
      this.states.set(id, state);
    } else {
      // The newest profile owns fetch; its key is the same by construction.
      state.profile = profile as ResourceProfile<unknown>;
    }
    state.owners.set(owner, (state.owners.get(owner) ?? 0) + 1);
    if (!state.seen) this.requestWatermark?.();
    state.dormant.delete(owner);
    this.pump();
    return id;
  }

  /**
   * Drops one of `owner`'s registrations of each id in `ids`, or every
   * registration of `owner` when omitted (a closed tab).
   */
  release(owner: string, ids?: Iterable<string>): void {
    const targets = ids ? [...ids] : [...this.states.keys()];
    let removed = false;
    for (const id of targets) {
      const state = this.states.get(id);
      const count = state?.owners.get(owner);
      if (!state || count === undefined) continue;
      if (ids && count > 1) state.owners.set(owner, count - 1);
      else {
        state.owners.delete(owner);
        state.dormant.delete(owner);
      }
      if (state.owners.size > 0) continue;
      state.inFlight?.abort();
      this.states.delete(id);
      this.finishRefreshes(state, Infinity, new Error("Resource released"));
      removed = true;
    }
    if (removed) this.notify();
  }

  /**
   * Keeps `owner`'s views of `ids` cached without polling them; `register`
   * resumes them. Presentation admission validates a resumed view separately.
   */
  suspend(owner: string, ids: Iterable<string>): void {
    for (const id of ids) {
      const state = this.states.get(id);
      if (!state?.owners.has(owner)) continue;
      state.dormant.add(owner);
      if (this.watched(state)) continue;
      this.fence(state);
      state.generation += 1;
    }
  }

  private watched(state: ResourceState): boolean {
    return state.owners.size > state.dormant.size;
  }

  isActiveProject(project: string): boolean {
    return this.activeProject === project;
  }

  /** Every resource of a project the member can no longer read. */
  dropProject(project: string): void {
    let removed = false;
    for (const [id, state] of this.states) {
      if (state.profile.key.project !== project) continue;
      state.inFlight?.abort();
      this.states.delete(id);
      this.finishRefreshes(state, Infinity, new Error("Resource released"));
      removed = true;
    }
    if (removed) this.notify();
  }

  /** Clears everything, e.g. after the backend or member changed. */
  reset(): void {
    for (const state of this.states.values()) {
      state.inFlight?.abort();
      this.finishRefreshes(state, Infinity, new Error("Resource store reset"));
    }
    this.states.clear();
    this.presentations.clear();
    this.epoch = null;
    this.notify();
  }

  attachScheduler(requestWatermark: () => void): () => void {
    this.requestWatermark = requestWatermark;
    return () => {
      if (this.requestWatermark === requestWatermark) this.requestWatermark = null;
    };
  }

  /** Open projects with a watched key still waiting for its first watermark. */
  awaitingWatermark(): Set<string> {
    const projects = new Set<string>();
    if (!this.requestWatermark) return projects;
    for (const state of this.states.values()) {
      if (!state.seen && this.watched(state)) projects.add(state.profile.key.project);
    }
    for (const entry of this.presentations.values()) {
      if (entry.admitted) continue;
      for (const id of entry.ids) {
        const state = this.states.get(id);
        if (state && !entry.proofs.has(id)) projects.add(state.profile.key.project);
      }
    }
    return projects;
  }

  /** Capture issuance order, never response arrival order. */
  beginObservation(project: string): ObservationTicket {
    return { project, serial: ++this.observationSerial };
  }

  failObservation(ticket: ObservationTicket, error: unknown): void {
    for (const entry of this.presentations.values()) {
      if (
        !entry.admitted &&
        ticket.serial >= entry.minimumObservation &&
        entry.ids.some((id) => this.states.get(id)?.profile.key.project === ticket.project)
      )
        entry.error = error;
    }
    this.notify();
  }

  /** Starts one owner's presentation without evicting or fencing shared data. */
  beginPresentation(
    owner: string,
    activationKey: string,
    ids: readonly string[],
  ): PresentationTicket {
    const ticket = { owner, serial: ++this.presentationSerial };
    this.presentations.set(owner, {
      ticket,
      activationKey,
      ids: [...ids],
      minimumObservation: this.observationSerial + 1,
      minimumRead: this.reads + 1,
      proofs: new Map(),
      admitted: false,
      error: undefined,
      view: null,
    });
    if (this.requestWatermark && ids.length) this.requestWatermark();
    else {
      // Existing in-flight reads may finish for other owners. Their invalidation
      // debt causes a confirming read issued after this entry, without aborting them.
      for (const id of ids) this.invalidate(id);
    }
    this.notify();
    return ticket;
  }

  endPresentation(ticket: PresentationTicket): void {
    if (this.presentations.get(ticket.owner)?.ticket !== ticket) return;
    this.presentations.delete(ticket.owner);
    this.notify();
  }

  /** A render for a new entry cannot see a previous entry's retained frame. */
  presentationView(owner: string, activationKey: string): ResourcePresentation {
    const entry = this.presentations.get(owner);
    if (!entry || entry.activationKey !== activationKey) return EMPTY_PRESENTATION;
    const states = entry.ids.map((id) => this.states.get(id));
    if (!entry.admitted) {
      entry.admitted = states.every((state, index) => {
        if (!state?.hasValue || state.owed || state.valueEpoch !== this.epoch) return false;
        if (!this.requestWatermark) return state.acceptedReadStart >= entry.minimumRead;
        const proof = entry.proofs.get(entry.ids[index]);
        return proof?.epoch === state.valueEpoch && proof?.version === state.committed;
      });
    }
    const error = entry.admitted
      ? undefined
      : (entry.error ?? states.find((state) => state?.error !== undefined)?.error);
    const status = entry.admitted ? "ready" : error !== undefined ? "error" : "loading";
    const views = entry.admitted ? entry.ids.map((id) => this.view(id)) : [];
    const previous = entry.view;
    if (
      previous?.status === status &&
      previous.error === error &&
      previous.views.length === views.length &&
      views.every((view, index) => view === previous.views[index])
    )
      return previous;
    entry.view = { status, error, views };
    return entry.view;
  }

  setActiveProject(project: string | null): void {
    this.activeProject = project;
    this.pump();
  }

  /** The watermark request body for each open project: what its owners watch. */
  requests(project: string): ResourceRequest[] {
    const seen = new Map<string, ResourceRequest>();
    for (const state of this.states.values()) {
      const key = state.profile.key;
      if (key.project !== project || !this.watched(state)) continue;
      const entry: ResourceRequest = { name: key.name };
      if (key.ref !== undefined) entry.ref = key.ref;
      if (key.entity !== undefined) entry.entity = key.entity;
      seen.set(watermarkId(project, entry), entry);
      for (const dependency of state.profile.watch ?? [])
        seen.set(watermarkId(project, dependency), dependency);
    }
    return [...seen.values()];
  }

  /** A new epoch fences every read and forgets every version. */
  observeEpoch(epoch: string): void {
    if (this.epoch === epoch) return;
    const first = this.epoch === null;
    this.epoch = epoch;
    if (first) return;
    for (const entry of this.presentations.values()) {
      entry.admitted = false;
      entry.proofs.clear();
      entry.error = undefined;
    }
    for (const state of this.states.values()) {
      state.inFlight?.abort();
      state.inFlight = null;
      state.generation += 1;
      state.committed = undefined;
      state.observed = undefined;
      state.seen = false;
      state.owed = true;
      // A reachable new epoch ends any backoff earned against the old one.
      state.failures = 0;
      state.retryAt = 0;
      state.view = null;
    }
    this.notify();
    this.pump();
  }

  /** Records a project's watermarks; any resource whose token moved owes a read. */
  observe(
    project: string,
    watermarks: readonly ResourceWatermark[],
    ticket = this.beginObservation(project),
  ): void {
    const versions = new Map(
      watermarks.map((entry) => [watermarkId(project, entry), entry.version]),
    );
    for (const state of this.states.values()) {
      const key = state.profile.key;
      if (key.project !== project || !this.watched(state)) continue;
      const dependencies = [key, ...(state.profile.watch ?? [])];
      const tokens = dependencies.map((entry) => versions.get(watermarkId(project, entry)));
      if (tokens.some((token) => token === undefined)) continue;
      const version = tokens.length === 1 ? (tokens[0] as string) : JSON.stringify(tokens);
      state.observed = version;
      state.seen = true;
      for (const entry of this.presentations.values()) {
        const id = resourceKeyId(key);
        if (
          ticket.project !== project ||
          ticket.serial < entry.minimumObservation ||
          !entry.ids.includes(id)
        )
          continue;
        entry.proofs.set(id, { epoch: this.epoch, version });
        entry.error = undefined;
      }
    }
    this.notify();
    this.pump();
  }

  private owes(state: ResourceState): boolean {
    if (!this.watched(state) || state.inFlight || state.retryAt > this.now()) return false;
    if (this.requestWatermark && !state.seen) return false;
    return !state.hasValue || state.owed || state.committed !== state.observed;
  }

  /** Starts owed reads within the slot budget, active project first. */
  pump(): void {
    const owed = [...this.states.values()].filter((state) => this.owes(state));
    owed.sort(
      (left, right) =>
        Number(right.profile.key.project === this.activeProject) -
          Number(left.profile.key.project === this.activeProject) || left.lastRead - right.lastRead,
    );
    const backgroundWaiting = owed.some(
      (state) => state.profile.key.project !== this.activeProject,
    );
    for (const state of owed) {
      if (this.running >= RESOURCE_READ_SLOTS) return;
      const background = state.profile.key.project !== this.activeProject;
      if (background && this.runningBackground >= RESOURCE_BACKGROUND_READ_SLOTS) continue;
      // Leave the next available slot to queued background work.
      if (
        !background &&
        backgroundWaiting &&
        this.runningBackground === 0 &&
        this.running >= RESOURCE_READ_SLOTS - RESOURCE_BACKGROUND_READ_SLOTS
      )
        continue;
      void this.read(state, background);
    }
  }

  /** The earliest time a failed read may retry, for the scheduler's timer. */
  nextRetryAt(): number | null {
    let next: number | null = null;
    for (const state of this.states.values()) {
      if (state.retryAt > this.now() && (next === null || state.retryAt < next)) {
        next = state.retryAt;
      }
    }
    return next;
  }

  private async read(state: ResourceState, background: boolean): Promise<void> {
    const controller = new AbortController();
    const generation = state.generation;
    const sequence = ++state.sequence;
    const version = state.observed;
    const invalidation = state.invalidations;
    state.lastRead = ++this.reads;
    const readStart = state.lastRead;
    state.inFlight = controller;
    this.running += 1;
    if (background) this.runningBackground += 1;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        const error = new Error("Resource read timed out");
        controller.abort(error);
        reject(error);
      }, RESOURCE_READ_TIMEOUT_MS);
      // Node test runs must not wait on an abandoned read's deadline.
      (timer as { unref?: () => void }).unref?.();
    });
    try {
      const value = await Promise.race([state.profile.fetch(controller.signal), timeout]);
      if (this.accepts(state, generation, sequence)) {
        state.value = state.profile.reconcile
          ? state.profile.reconcile(
              state.valueEpoch === this.epoch ? state.value : undefined,
              value,
            )
          : value;
        state.hasValue = true;
        state.valueEpoch = this.epoch;
        state.committed = version;
        state.acceptedReads += 1;
        state.acceptedReadStart = readStart;
        state.owed = state.invalidations !== invalidation;
        state.error = undefined;
        state.failures = 0;
        state.retryAt = 0;
        this.finishRefreshes(state, invalidation);
        this.changed(state);
      }
    } catch (error) {
      if (this.accepts(state, generation, sequence)) {
        state.error = error;
        this.finishRefreshes(state, invalidation, error);
        state.failures += 1;
        state.retryAt = this.now() + retryBackoffMs(state.failures);
        this.changed(state);
      }
    } finally {
      clearTimeout(timer);
      if (state.inFlight === controller) state.inFlight = null;
      this.running -= 1;
      if (background) this.runningBackground -= 1;
      this.pump();
    }
  }

  private accepts(state: ResourceState, generation: number, sequence: number): boolean {
    return (
      this.states.get(resourceKeyId(state.profile.key)) === state &&
      state.generation === generation &&
      state.sequence === sequence
    );
  }

  /** Fences in-flight reads of `id`; a later read starts fresh. */
  private fence(state: ResourceState): void {
    state.sequence += 1;
    state.inFlight?.abort();
    state.inFlight = null;
  }

  /**
   * Shows a command's intent at once on every subscriber of `effects`' keys.
   * `settle` replaces the overlay with the server's values (keys it omits are
   * reread); `fail` removes only this operation's overlay.
   */
  beginMutation(
    operationId: string,
    effects: ReadonlyMap<string, (value: unknown) => unknown>,
  ): {
    settle(values?: ReadonlyMap<string, unknown>): void;
    fail(feedback?: ReadonlyMap<string, PendingOperation>): void;
  } {
    const order = ++this.mutations;
    // A response is accepted only within the generation and read history it
    // started from: an epoch change or an accepted read outranks it.
    const startedAt = new Map<
      string,
      { state: ResourceState; generation: number; reads: number }
    >();
    for (const [id, apply] of effects) {
      const state = this.states.get(id);
      if (!state) continue;
      this.fence(state);
      startedAt.set(id, { state, generation: state.generation, reads: state.acceptedReads });
      state.pending.push({ id: operationId, apply });
      state.view = null;
    }
    this.notify();
    const finish = (
      values: ReadonlyMap<string, unknown> | undefined,
      ok: boolean,
      feedback?: ReadonlyMap<string, PendingOperation>,
    ) => {
      for (const id of effects.keys()) {
        const state = this.states.get(id);
        if (!state || startedAt.get(id)?.state !== state) continue;
        state.pending = state.pending.filter((operation) => operation.id !== operationId);
        const overlay = feedback?.get(id);
        if (overlay) state.overlays.set(overlay.id, overlay.apply);
        if (ok) {
          this.fence(state);
          const start = startedAt.get(id);
          const current =
            order > state.settledMutation &&
            start?.generation === state.generation &&
            start.reads === state.acceptedReads;
          // Any successful settlement fences older commands, with or without values.
          if (order > state.settledMutation) state.settledMutation = order;
          if (current && values?.has(id)) {
            const value = values.get(id);
            state.value = state.profile.reconcile
              ? state.profile.reconcile(
                  state.valueEpoch === this.epoch ? state.value : undefined,
                  value,
                )
              : value;
            state.hasValue = true;
            state.valueEpoch = this.epoch;
            // The response's own version is unknown, so the version of the
            // data it replaces stays committed: a token that moved meanwhile
            // still owes a read.
            state.error = undefined;
          } else {
            state.owed = true;
          }
        }
        state.view = null;
      }
      this.notify();
      this.pump();
    };
    return {
      settle: (values) => finish(values, true),
      fail: (feedback) => finish(undefined, false, feedback),
    };
  }

  /** Publishes a value a mutation response carried for an existing key. */
  write(id: string, value: unknown): void {
    const state = this.states.get(id);
    if (!state) return;
    this.fence(state);
    state.value = state.profile.reconcile
      ? state.profile.reconcile(state.valueEpoch === this.epoch ? state.value : undefined, value)
      : value;
    state.hasValue = true;
    state.valueEpoch = this.epoch;
    state.error = undefined;
    // A response may be older than a read accepted while it was in flight, so
    // one read confirms it.
    state.owed = true;
    this.changed(state);
    this.pump();
  }

  /** The backend is reachable again: reads that failed while it was away retry now. */
  endBackoff(): void {
    for (const state of this.states.values()) {
      state.failures = 0;
      state.retryAt = 0;
    }
    this.pump();
  }

  /** Marks `id` as owing a read without waiting for its token to move. */
  invalidate(id: string): void {
    const state = this.states.get(id);
    if (!state) return;
    state.owed = true;
    state.invalidations += 1;
    state.retryAt = 0;
    this.pump();
  }

  /** Invalidates and waits for a read started after this request. */
  refresh(id: string): Promise<void> {
    const state = this.states.get(id);
    if (!state) return Promise.reject(new Error("Resource is not registered"));
    return new Promise((resolve, reject) => {
      state.waiters.push({ invalidation: state.invalidations + 1, resolve, reject });
      this.invalidate(id);
    });
  }

  private finishRefreshes(state: ResourceState, invalidation: number, error?: unknown): void {
    const ready = state.waiters.filter((waiter) => waiter.invalidation <= invalidation);
    state.waiters = state.waiters.filter((waiter) => waiter.invalidation > invalidation);
    for (const waiter of ready) {
      if (error !== undefined) waiter.reject(error);
      else waiter.resolve();
    }
  }

  /** Clears local operation feedback when its owner starts the next action. */
  clearOverlay(id: string, operationId: string): void {
    const state = this.states.get(id);
    if (!state) return;
    state.overlays.delete(operationId);
    this.changed(state);
  }

  /** Accepted data only, for response merges that must not persist other overlays. */
  committedValue<T>(id: string): T | undefined {
    return this.states.get(id)?.value as T | undefined;
  }

  view<T>(id: string): ResourceView<T> {
    const state = this.states.get(id);
    if (!state) return EMPTY_VIEW;
    if (!state.view) {
      let value = state.hasValue ? state.value : undefined;
      for (const operation of state.pending) value = operation.apply(value);
      for (const apply of state.overlays.values()) value = apply(value);
      state.view = {
        value,
        epoch: state.valueEpoch,
        loading: !state.hasValue,
        stale: state.error !== undefined,
        error: state.error,
        pending: state.pending.map((operation) => operation.id),
      };
    }
    return state.view as ResourceView<T>;
  }
}
