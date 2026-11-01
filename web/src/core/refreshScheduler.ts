// The one timer that keeps open project tabs current.
//
// Each beat sends one batched watermark request for every open project: the
// active project is due every beat, hidden projects every third beat, and any
// project whose `valid_until` has passed. Only one request is in flight; the
// next beat is scheduled after it settles. Failures back off 3, 6, 12, 30 s
// while every tab keeps its last good data. The scheduler never names a
// resource; owners register profiles with the store.

import { retryBackoffMs, type ResourceStore, type ResourceWatermark } from "./resourceStore";

export const REFRESH_BEAT_MS = 1000;
export const HIDDEN_PROJECT_BEAT_EVERY = 3;
// A browser-side attempt budget, like IDEMPOTENT_TASK_START_TIMEOUT_MS; the
// server answers from counters and never approaches it.
export const WATERMARK_REQUEST_TIMEOUT_MS = 5000;

export interface OpenProject {
  projectId: string;
  currentRef: string;
}

export interface ProjectSyncHealth {
  last_completed_at: string | null;
  status: "ok" | "syncing" | "error" | "local";
  error: string | null;
}

export interface WatermarkRequest {
  client_id: string;
  open: {
    project_id: string;
    current_ref: string;
    resources: ReturnType<ResourceStore["requests"]>;
  }[];
  due: string[];
  activated?: string;
}

export interface WatermarkResponse {
  epoch: string;
  as_of: string;
  projects: {
    requested_id: string;
    project_id: string;
    resources: (ResourceWatermark & { valid_until: string | null })[];
    sync: ProjectSyncHealth;
  }[];
  unavailable: string[];
}

export interface RefreshHealth {
  /** The latest watermark request failed. */
  failing: boolean;
  sync: ReadonlyMap<string, ProjectSyncHealth>;
}

export interface RefreshClock {
  now(): number;
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface RefreshVisibility {
  isHidden(): boolean;
  listen(callback: () => void): () => void;
}

export interface RefreshSchedulerOptions {
  store: ResourceStore;
  clientId: string;
  openProjects(): readonly OpenProject[];
  activeProjectId(): string | null;
  send(request: WatermarkRequest, signal: AbortSignal): Promise<WatermarkResponse>;
  /** A requested project the member can no longer read. */
  onLostAccess(projectId: string): void;
  clock?: RefreshClock;
  visibility?: RefreshVisibility;
}

const defaultClock: RefreshClock = {
  now: () => Date.now(),
  setTimeout: (callback, ms) => setTimeout(callback, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

const documentVisibility: RefreshVisibility = {
  isHidden: () => typeof document !== "undefined" && document.visibilityState === "hidden",
  listen: (callback) => {
    if (typeof document === "undefined") return () => {};
    document.addEventListener("visibilitychange", callback);
    return () => document.removeEventListener("visibilitychange", callback);
  },
};

export class RefreshScheduler {
  private readonly options: RefreshSchedulerOptions;
  private readonly clock: RefreshClock;
  private readonly visibility: RefreshVisibility;
  private readonly listeners = new Set<() => void>();
  private timer: unknown = null;
  private retryTimer: unknown = null;
  private inFlight: AbortController | null = null;
  private beat = 0;
  private failures = 0;
  private activated: string | null = null;
  private readonly validUntil = new Map<string, number>();
  private stopVisibility: (() => void) | null = null;
  private health: RefreshHealth = { failing: false, sync: new Map() };

  constructor(options: RefreshSchedulerOptions) {
    this.options = options;
    this.clock = options.clock ?? defaultClock;
    this.visibility = options.visibility ?? documentVisibility;
    options.store.attachScheduler(() => this.now());
  }

  start(): void {
    this.stopVisibility = this.visibility.listen(() => {
      if (!this.visibility.isHidden()) this.now();
    });
    this.schedule(0);
  }

  stop(): void {
    this.stopVisibility?.();
    this.stopVisibility = null;
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
    if (this.retryTimer !== null) this.clock.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.inFlight?.abort();
    this.inFlight = null;
  }

  /** A tab was activated: validate it now and ask for an immediate remote sync. */
  activate(projectId: string): void {
    this.activated = projectId;
    this.options.store.setActiveProject(projectId);
    this.now();
  }

  /** Runs a beat as soon as no request is in flight. */
  now(): void {
    if (!this.stopVisibility || this.inFlight) return;
    this.schedule(0);
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getHealth = (): RefreshHealth => this.health;

  // Subscribers re-render the whole app shell, so an unchanged beat publishes nothing.
  private setHealth(next: RefreshHealth): void {
    if (next.failing === this.health.failing && sameSync(next.sync, this.health.sync)) return;
    this.health = next;
    for (const listener of this.listeners) listener();
  }

  private schedule(ms: number): void {
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      void this.run();
    }, ms);
  }

  private due(open: readonly OpenProject[], active: string | null): string[] {
    const hiddenDue = this.beat % HIDDEN_PROJECT_BEAT_EVERY === 0;
    const now = this.clock.now();
    const awaiting = this.options.store.awaitingWatermark();
    return open
      .map((project) => project.projectId)
      .filter(
        (id) =>
          id === active ||
          id === this.activated ||
          awaiting.has(id) ||
          hiddenDue ||
          (this.validUntil.get(id) ?? Number.POSITIVE_INFINITY) <= now,
      );
  }

  /** Entries created after request issuance need another observation immediately. */
  private newlyAwaiting(open: readonly OpenProject[]): boolean {
    const awaiting = this.options.store.awaitingWatermark();
    return open.some((project) => awaiting.has(project.projectId));
  }

  private async run(): Promise<void> {
    if (this.visibility.isHidden()) return;
    const { store } = this.options;
    const open = this.options.openProjects();
    if (open.length === 0) {
      this.schedule(REFRESH_BEAT_MS);
      return;
    }
    const active = this.options.activeProjectId();
    store.setActiveProject(active);
    const due = this.due(open, active);
    const activated = this.activated;
    this.activated = null;
    this.beat += 1;
    const request: WatermarkRequest = {
      client_id: this.options.clientId,
      open: open.map((project) => ({
        project_id: project.projectId,
        current_ref: project.currentRef,
        resources: store.requests(project.projectId),
      })),
      due,
      ...(activated ? { activated } : {}),
    };
    const controller = new AbortController();
    const observations = new Map(
      open.map((project) => [project.projectId, store.beginObservation(project.projectId)]),
    );
    this.inFlight = controller;
    let activatedDuringFailure = false;
    const timeout = this.clock.setTimeout(() => controller.abort(), WATERMARK_REQUEST_TIMEOUT_MS);
    try {
      const response = await this.options.send(request, controller.signal);
      if (this.inFlight !== controller) return;
      this.failures = 0;
      store.observeEpoch(response.epoch);
      const sync = new Map(this.health.sync);
      // Deadlines are server times; anchor them to local receipt via `as_of`.
      const serverNow = Date.parse(response.as_of);
      const receivedAt = this.clock.now();
      for (const project of response.projects) {
        const observation = observations.get(project.requested_id);
        if (!observation) continue;
        const deadlines = project.resources
          .map((entry) =>
            entry.valid_until ? receivedAt + Date.parse(entry.valid_until) - serverNow : Number.NaN,
          )
          .filter((value) => Number.isFinite(value));
        if (deadlines.length) this.validUntil.set(project.requested_id, Math.min(...deadlines));
        else this.validUntil.delete(project.requested_id);
        store.observe(project.requested_id, project.resources, observation);
        sync.set(project.requested_id, project.sync);
      }
      for (const id of response.unavailable) {
        sync.delete(id);
        this.validUntil.delete(id);
        store.dropProject(id);
        this.options.onLostAccess(id);
      }
      this.setHealth({ failing: false, sync });
    } catch (error) {
      if (this.inFlight !== controller) return;
      this.failures += 1;
      for (const ticket of observations.values()) store.failObservation(ticket, error);
      // A newer activation made during the request wins over the retried one,
      // and runs at once instead of waiting out the failure backoff.
      activatedDuringFailure = this.activated !== null;
      if (activated && this.activated === null) this.activated = activated;
      this.setHealth({ ...this.health, failing: true });
    } finally {
      this.clock.clearTimeout(timeout);
      if (this.inFlight === controller) {
        this.inFlight = null;
        store.pump();
        const retry = store.nextRetryAt();
        // An activation queued during this request runs at once, not next beat.
        const beatMs = this.failures
          ? activatedDuringFailure
            ? 0
            : retryBackoffMs(this.failures)
          : this.activated || this.newlyAwaiting(open)
            ? 0
            : REFRESH_BEAT_MS;
        const untilRetry = retry === null ? null : Math.max(0, retry - this.clock.now());
        if (this.retryTimer !== null) this.clock.clearTimeout(this.retryTimer);
        this.retryTimer = null;
        if (this.failures && untilRetry !== null) {
          // A failing watermark keeps its backoff; payload retries only pump.
          this.retryTimer = this.clock.setTimeout(() => {
            this.retryTimer = null;
            store.pump();
          }, untilRetry);
        }
        this.schedule(this.failures || untilRetry === null ? beatMs : Math.min(beatMs, untilRetry));
      }
    }
  }
}

function sameSync(
  a: ReadonlyMap<string, ProjectSyncHealth>,
  b: ReadonlyMap<string, ProjectSyncHealth>,
): boolean {
  if (a.size !== b.size) return false;
  for (const [id, health] of a) {
    const other = b.get(id);
    if (
      !other ||
      other.status !== health.status ||
      other.last_completed_at !== health.last_completed_at ||
      other.error !== health.error
    )
      return false;
  }
  return true;
}
