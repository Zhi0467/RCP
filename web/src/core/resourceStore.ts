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
  fetch(signal: AbortSignal): Promise<T>;
  /** Keeps unchanged parts of the previous value so consumers keep identity. */
  reconcile?(previous: T | undefined, next: T): T;
}

export interface ResourceWatermark extends ResourceRequest {
  version: string;
}

interface PendingOperation {
  id: string;
  apply(value: unknown): unknown;
}

export interface ResourceView<T> {
  value: T | undefined;
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
  value: unknown;
  hasValue: boolean;
  committed: string | undefined;
  observed: string | undefined;
  /** Owes a read regardless of tokens (invalidated, new epoch, unsettled command). */
  owed: boolean;
  generation: number;
  sequence: number;
  inFlight: AbortController | null;
  error: unknown;
  failures: number;
  retryAt: number;
  pending: PendingOperation[];
  /** Start order of the newest command whose response replaced the value. */
  settledMutation: number;
  view: ResourceView<unknown> | null;
}

export const RESOURCE_READ_SLOTS = 3;
export const RESOURCE_BACKGROUND_READ_SLOTS = 1;
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
  loading: true,
  stale: false,
  error: undefined,
  pending: [],
};

export class ResourceStore {
  private readonly states = new Map<string, ResourceState>();
  private readonly listeners = new Set<() => void>();
  private activeProject: string | null = null;
  private running = 0;
  private runningBackground = 0;
  private epoch: string | null = null;
  private mutations = 0;
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
        value: undefined,
        hasValue: false,
        committed: undefined,
        observed: undefined,
        owed: false,
        generation: 0,
        sequence: 0,
        inFlight: null,
        error: undefined,
        failures: 0,
        retryAt: 0,
        pending: [],
        settledMutation: 0,
        view: null,
      };
      this.states.set(id, state);
    } else {
      // The newest profile owns fetch; its key is the same by construction.
      state.profile = profile as ResourceProfile<unknown>;
    }
    state.owners.set(owner, (state.owners.get(owner) ?? 0) + 1);
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
      else state.owners.delete(owner);
      if (state.owners.size > 0) continue;
      state.inFlight?.abort();
      this.states.delete(id);
      removed = true;
    }
    if (removed) this.notify();
  }

  /** Every resource of a project the member can no longer read. */
  dropProject(project: string): void {
    let removed = false;
    for (const [id, state] of this.states) {
      if (state.profile.key.project !== project) continue;
      state.inFlight?.abort();
      this.states.delete(id);
      removed = true;
    }
    if (removed) this.notify();
  }

  /** Clears everything, e.g. after the backend or member changed. */
  reset(): void {
    for (const state of this.states.values()) state.inFlight?.abort();
    this.states.clear();
    this.epoch = null;
    this.notify();
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
      if (key.project !== project) continue;
      const entry: ResourceRequest = { name: key.name };
      if (key.ref !== undefined) entry.ref = key.ref;
      if (key.entity !== undefined) entry.entity = key.entity;
      seen.set(watermarkId(project, entry), entry);
    }
    return [...seen.values()];
  }

  /** A new epoch fences every read and forgets every version. */
  observeEpoch(epoch: string): void {
    if (this.epoch === epoch) return;
    const first = this.epoch === null;
    this.epoch = epoch;
    if (first) return;
    for (const state of this.states.values()) {
      state.inFlight?.abort();
      state.inFlight = null;
      state.generation += 1;
      state.committed = undefined;
      state.observed = undefined;
      state.owed = true;
      state.view = null;
    }
    this.notify();
    this.pump();
  }

  /** Records a project's watermarks; any resource whose token moved owes a read. */
  observe(project: string, watermarks: readonly ResourceWatermark[]): void {
    const versions = new Map(
      watermarks.map((entry) => [watermarkId(project, entry), entry.version]),
    );
    for (const state of this.states.values()) {
      const key = state.profile.key;
      if (key.project !== project) continue;
      const version = versions.get(watermarkId(project, key));
      if (version !== undefined) state.observed = version;
    }
    this.pump();
  }

  private owes(state: ResourceState): boolean {
    if (state.inFlight || state.retryAt > this.now()) return false;
    return !state.hasValue || state.owed || state.committed !== state.observed;
  }

  /** Starts owed reads within the slot budget, active project first. */
  pump(): void {
    const owed = [...this.states.values()].filter((state) => this.owes(state));
    owed.sort(
      (left, right) =>
        Number(right.profile.key.project === this.activeProject) -
        Number(left.profile.key.project === this.activeProject),
    );
    for (const state of owed) {
      if (this.running >= RESOURCE_READ_SLOTS) return;
      const background = state.profile.key.project !== this.activeProject;
      if (background && this.runningBackground >= RESOURCE_BACKGROUND_READ_SLOTS) continue;
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
    state.inFlight = controller;
    this.running += 1;
    if (background) this.runningBackground += 1;
    try {
      const value = await state.profile.fetch(controller.signal);
      if (this.accepts(state, generation, sequence)) {
        state.value = state.profile.reconcile
          ? state.profile.reconcile(state.hasValue ? state.value : undefined, value)
          : value;
        state.hasValue = true;
        state.committed = version;
        state.owed = false;
        state.error = undefined;
        state.failures = 0;
        state.retryAt = 0;
        this.changed(state);
      }
    } catch (error) {
      if (this.accepts(state, generation, sequence)) {
        state.error = error;
        state.failures += 1;
        state.retryAt = this.now() + retryBackoffMs(state.failures);
        this.changed(state);
      }
    } finally {
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
  ): { settle(values?: ReadonlyMap<string, unknown>): void; fail(): void } {
    const order = ++this.mutations;
    for (const [id, apply] of effects) {
      const state = this.states.get(id);
      if (!state) continue;
      this.fence(state);
      state.pending.push({ id: operationId, apply });
      state.view = null;
    }
    this.notify();
    const finish = (values: ReadonlyMap<string, unknown> | undefined, ok: boolean) => {
      for (const id of effects.keys()) {
        const state = this.states.get(id);
        if (!state) continue;
        state.pending = state.pending.filter((operation) => operation.id !== operationId);
        if (ok) {
          this.fence(state);
          // An older command's response never replaces a newer one's; it
          // only owes a read.
          if (values?.has(id) && order > state.settledMutation) {
            state.settledMutation = order;
            const value = values.get(id);
            state.value = state.profile.reconcile
              ? state.profile.reconcile(state.hasValue ? state.value : undefined, value)
              : value;
            state.hasValue = true;
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
      fail: () => finish(undefined, false),
    };
  }

  /** Publishes a value a mutation response carried for an existing key. */
  write(id: string, value: unknown): void {
    const state = this.states.get(id);
    if (!state) return;
    this.fence(state);
    state.value = state.profile.reconcile
      ? state.profile.reconcile(state.hasValue ? state.value : undefined, value)
      : value;
    state.hasValue = true;
    state.error = undefined;
    this.changed(state);
    this.pump();
  }

  /** Marks `id` as owing a read without waiting for its token to move. */
  invalidate(id: string): void {
    const state = this.states.get(id);
    if (!state) return;
    state.owed = true;
    state.retryAt = 0;
    this.pump();
  }

  view<T>(id: string): ResourceView<T> {
    const state = this.states.get(id);
    if (!state) return EMPTY_VIEW;
    if (!state.view) {
      let value = state.hasValue ? state.value : undefined;
      for (const operation of state.pending) value = operation.apply(value);
      state.view = {
        value,
        loading: !state.hasValue,
        stale: state.error !== undefined,
        error: state.error,
        pending: state.pending.map((operation) => operation.id),
      };
    }
    return state.view as ResourceView<T>;
  }
}
