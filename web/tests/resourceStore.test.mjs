import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { ResourceStore, RESOURCE_READ_TIMEOUT_MS, resourceKeyId } = await server.ssrLoadModule(
  "/src/core/resourceStore.ts",
);
const { RefreshScheduler } = await server.ssrLoadModule("/src/core/refreshScheduler.ts");
after(() => server.close());

const key = (name, project = "p", extra = {}) => ({
  backend: "b",
  member: "m",
  project,
  name,
  ...extra,
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

function manualProfile(name, project) {
  const reads = [];
  return {
    reads,
    profile: {
      key: key(name, project),
      fetch: (signal) => {
        const read = deferred();
        signal.addEventListener("abort", () => read.reject(new Error("aborted")));
        reads.push(read);
        return read.promise;
      },
    },
  };
}

test("a closed tab's mutation cannot settle into its reopened resource", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  reads[0].resolve("original");
  await settle();
  const command = store.beginMutation("retry", new Map([[id, (value) => value]]));
  store.release("tab");
  store.register("tab", profile);
  reads[1].resolve("reopened");
  await settle();
  command.settle(new Map([[id, "old response"]]));
  assert.equal(store.view(id).value, "reopened");
  assert.equal(reads.length, 2);
});

test("an invalidation during a read requires a subsequent read", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("graph");
  const id = store.register("tab", profile);
  store.invalidate(id);
  reads[0].resolve("before invalidation");
  await settle();
  assert.equal(reads.length, 2);
  reads[1].resolve("after invalidation");
  await settle();
  assert.equal(store.view(id).value, "after invalidation");
  assert.equal(reads.length, 2);
});

test("queued hidden reads progress even while active reads keep accruing debt", async () => {
  const store = new ResourceStore();
  store.setActiveProject("active");
  const active = ["a", "b", "c", "d"].map((name) => manualProfile(name, "active"));
  const ids = active.map(({ profile }) => store.register("active", profile));
  const hidden = manualProfile("graph", "hidden");
  store.register("hidden", hidden.profile);
  for (const id of ids) store.invalidate(id);
  active[0].reads[0].resolve("done");
  await settle();
  assert.equal(hidden.reads.length, 1);
});

test("refresh waits for its own read and rejects its read failure", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("graph");
  const id = store.register("tab", profile);
  let completed = false;
  const refresh = store.refresh(id).then(() => {
    completed = true;
  });
  reads[0].resolve("earlier read");
  await settle();
  assert.equal(completed, false);
  reads[1].resolve("refreshed");
  await refresh;
  const error = new Error("offline");
  const failed = assert.rejects(store.refresh(id), (failure) => failure === error);
  reads[2].reject(error);
  await failed;
  const closed = assert.rejects(store.refresh(id));
  store.release("tab");
  await closed;
});

test("a read started before a newer read never commits", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  store.observe("p", [{ name: "tasks.status", version: "1" }]);
  store.invalidate(id);
  // The first read is in flight; a mutation fences it and a newer read follows.
  const mutation = store.beginMutation("op", new Map([[id, (value) => value]]));
  mutation.settle();
  await settle();
  assert.equal(reads.length, 2);
  reads[1].resolve("new");
  await settle();
  reads[0].resolve("old");
  await settle();
  assert.equal(store.view(id).value, "new");
});

test("only an accepted read acknowledges the observed version", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("chats");
  const id = store.register("tab", profile);
  reads[0].resolve("a");
  await settle();
  store.observe("p", [{ name: "chats", version: "2" }]);
  assert.equal(reads.length, 2);
  reads[1].reject(new Error("offline"));
  await settle();
  assert.equal(store.view(id).value, "a");
  assert.equal(store.view(id).stale, true);
  // The same token again does not erase the debt; the read retries after backoff.
  store.observe("p", [{ name: "chats", version: "2" }]);
  assert.equal(reads.length, 2);
});

test("a pending overlay shows at once and a failure removes only its own", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  reads[0].resolve(["queued"]);
  await settle();
  const first = store.beginMutation("retry", new Map([[id, (value) => [...value, "retrying"]]]));
  store.beginMutation("pause", new Map([[id, (value) => [...value, "pausing"]]]));
  assert.deepEqual(store.view(id).value, ["queued", "retrying", "pausing"]);
  first.fail();
  assert.deepEqual(store.view(id).value, ["queued", "pausing"]);
});

test("a new epoch drops in-flight reads and rereads everything", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("graph");
  const id = store.register("tab", profile);
  store.observeEpoch("e1");
  reads[0].resolve("before");
  await settle();
  store.observe("p", [{ name: "graph", version: "7" }]);
  store.observeEpoch("e2");
  reads[1].resolve("stale");
  await settle();
  assert.equal(reads.length, 3);
  reads[2].resolve("after");
  await settle();
  assert.equal(store.view(id).value, "after");
});

test("a new epoch ends a resource's read backoff", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("graph");
  store.register("tab", profile);
  store.observeEpoch("e1");
  reads[0].reject(new Error("offline"));
  await settle();
  store.observeEpoch("e2");
  assert.equal(reads.length, 2);
});

test("a recovered backend ends a failed read's backoff", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("graph");
  store.register("tab", profile);
  reads[0].reject(new Error("offline"));
  await settle();
  store.pump();
  assert.equal(reads.length, 1);
  store.endBackoff();
  assert.equal(reads.length, 2);
});

test("hidden projects use one background slot", async () => {
  const store = new ResourceStore();
  store.setActiveProject("active");
  const hidden = ["a", "b", "c"].map((name) => manualProfile(name, "hidden"));
  for (const entry of hidden) store.register("tab", entry.profile);
  assert.equal(hidden.filter((entry) => entry.reads.length).length, 1);
  const active = manualProfile("tasks.status", "active");
  store.register("tab", active.profile);
  assert.equal(active.reads.length, 1);
});

test("one owner's second registration survives the first release", () => {
  const store = new ResourceStore();
  const { profile } = manualProfile("questions");
  const id = store.register("tab", profile);
  store.register("tab", profile);
  store.release("tab", [id]);
  assert.equal(store.requests("p").length, 1);
});

test("a mutation response does not acknowledge a token that moved meanwhile", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  reads[0].resolve("v0");
  await settle();
  store.observe("p", [{ name: "tasks.status", version: "1" }]);
  reads[1].resolve("v1");
  await settle();
  const mutation = store.beginMutation("op", new Map([[id, (value) => value]]));
  store.observe("p", [{ name: "tasks.status", version: "2" }]);
  const before = reads.length;
  mutation.settle(new Map([[id, "response"]]));
  await settle();
  assert.equal(store.view(id).value, "response");
  assert.equal(reads.length, before + 1);
  reads.at(-1).resolve("fresh");
  await settle();
  assert.equal(store.view(id).value, "fresh");
});

test("an older command's late response never replaces a newer one's", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  reads[0].resolve("v0");
  await settle();
  const older = store.beginMutation("pause", new Map([[id, (value) => value]]));
  const newer = store.beginMutation("resume", new Map([[id, (value) => value]]));
  newer.settle(new Map([[id, "resumed"]]));
  older.settle(new Map([[id, "paused"]]));
  assert.equal(store.view(id).value, "resumed");
});

test("a read accepted during a command outranks its late response", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  reads[0].resolve("v0");
  await settle();
  const command = store.beginMutation("retry", new Map([[id, (value) => value]]));
  store.observe("p", [{ name: "tasks.status", version: "2" }]);
  reads.at(-1).resolve("v2");
  await settle();
  command.settle(new Map([[id, "response"]]));
  await settle();
  assert.equal(store.view(id).value, "v2");
});

test("a newer command settling without values still fences an older response", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  reads[0].resolve("v0");
  await settle();
  const older = store.beginMutation("pause", new Map([[id, (value) => value]]));
  const newer = store.beginMutation("resume", new Map([[id, (value) => value]]));
  newer.settle();
  older.settle(new Map([[id, "paused"]]));
  assert.equal(store.view(id).value, "v0");
});

test("a command response from before an epoch change is not accepted", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("tasks.status");
  const id = store.register("tab", profile);
  store.observeEpoch("e1");
  reads[0].resolve("v0");
  await settle();
  const command = store.beginMutation("retry", new Map([[id, (value) => value]]));
  store.observeEpoch("e2");
  command.settle(new Map([[id, "old epoch"]]));
  assert.equal(store.view(id).value, "v0");
});

test("a direct write is confirmed by one read", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("episodes");
  const id = store.register("tab", profile);
  reads[0].resolve("v0");
  await settle();
  store.write(id, "late response");
  assert.equal(store.view(id).value, "late response");
  assert.equal(reads.length, 2);
  reads[1].resolve("current");
  await settle();
  assert.equal(store.view(id).value, "current");
});

test("releasing the last owner forgets the resource", () => {
  const store = new ResourceStore();
  const { profile } = manualProfile("questions");
  const id = store.register("one", profile);
  store.register("two", profile);
  store.release("one");
  assert.equal(store.view(id).loading, true);
  assert.equal(resourceKeyId(profile.key), id);
  store.release("two");
  assert.deepEqual(store.requests("p"), []);
});

function fakeClock() {
  let now = 0;
  const timers = new Map();
  let next = 1;
  return {
    clock: {
      now: () => now,
      setTimeout: (callback, ms) => {
        timers.set(next, { at: now + ms, callback });
        return next++;
      },
      clearTimeout: (handle) => timers.delete(handle),
    },
    async advance(ms) {
      const until = now + ms;
      for (;;) {
        const due = [...timers.entries()]
          .filter(([, timer]) => timer.at <= until)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].callback();
        await settle();
      }
      now = until;
    },
  };
}

test("hidden projects are due every third beat and failures back off", async () => {
  const time = fakeClock();
  const requests = [];
  let fail = false;
  const scheduler = new RefreshScheduler({
    store: new ResourceStore(() => time.clock.now()),
    clientId: "c",
    openProjects: () => [
      { projectId: "active", currentRef: "main" },
      { projectId: "hidden", currentRef: "main" },
    ],
    activeProjectId: () => "active",
    send: async (request) => {
      requests.push({ at: time.clock.now(), due: request.due });
      if (fail) throw new Error("down");
      return { epoch: "e", as_of: "", projects: [], unavailable: [] };
    },
    onLostAccess: () => {},
    clock: time.clock,
    visibility: { isHidden: () => false, listen: () => () => {} },
  });
  scheduler.start();
  await time.advance(3500);
  assert.deepEqual(
    requests.map((request) => request.due.length),
    [2, 1, 1, 2],
  );
  fail = true;
  const before = requests.length;
  await time.advance(1000 + 3000 + 6000);
  assert.deepEqual(
    requests.slice(before).map((request) => request.at - requests[before - 1].at),
    [1000, 4000, 10_000],
  );
  scheduler.stop();
});

test("health is published only when it changes, not on every beat", async () => {
  const time = fakeClock();
  let sync = { last_completed_at: null, status: "ok", error: null };
  let fail = false;
  const scheduler = new RefreshScheduler({
    store: new ResourceStore(() => time.clock.now()),
    clientId: "c",
    openProjects: () => [{ projectId: "p", currentRef: "main" }],
    activeProjectId: () => "p",
    send: async () => {
      if (fail) throw new Error("down");
      return {
        epoch: "e",
        as_of: "",
        projects: [{ requested_id: "p", resources: [], sync: { ...sync } }],
        unavailable: [],
      };
    },
    onLostAccess: () => {},
    clock: time.clock,
    visibility: { isHidden: () => false, listen: () => () => {} },
  });
  let published = 0;
  scheduler.subscribe(() => published++);
  scheduler.start();
  await time.advance(5500);
  // The first answer is news; five identical beats after it are not.
  assert.equal(published, 1);
  sync = { ...sync, status: "syncing" };
  await time.advance(1000);
  assert.equal(published, 2);
  fail = true;
  await time.advance(1000);
  assert.equal(published, 3);
  assert.equal(scheduler.getHealth().failing, true);
  scheduler.stop();
});

test("an activation during a failing watermark runs at once, not after the backoff", async () => {
  const time = fakeClock();
  const requests = [];
  const pending = [];
  const scheduler = new RefreshScheduler({
    store: new ResourceStore(() => time.clock.now()),
    clientId: "c",
    openProjects: () => [{ projectId: "p", currentRef: "main" }],
    activeProjectId: () => "p",
    send: (request) => {
      requests.push({ at: time.clock.now(), activated: request.activated });
      const reply = deferred();
      pending.push(reply);
      return reply.promise;
    },
    onLostAccess: () => {},
    clock: time.clock,
    visibility: { isHidden: () => false, listen: () => () => {} },
  });
  scheduler.start();
  await time.advance(0);
  scheduler.activate("p");
  pending[0].reject(new Error("down"));
  await settle();
  await time.advance(0);
  assert.deepEqual(requests.at(-1), { at: 0, activated: "p" });
  // Failing again with nothing new queued backs off as before.
  pending[1].reject(new Error("down"));
  await settle();
  await time.advance(2999);
  assert.equal(requests.length, 2);
  scheduler.stop();
});

test("payload read retries do not shorten the watermark backoff", async () => {
  const time = fakeClock();
  const store = new ResourceStore(() => time.clock.now());
  const requests = [];
  let reads = 0;
  let fail = false;
  const scheduler = new RefreshScheduler({
    store,
    clientId: "c",
    openProjects: () => [{ projectId: "active", currentRef: "main" }],
    activeProjectId: () => "active",
    send: async (request) => {
      requests.push(time.clock.now());
      if (fail) throw new Error("down");
      const resources = request.open[0].resources.map((resource) => ({
        ...resource,
        version: "1",
        valid_until: null,
      }));
      return {
        epoch: "e",
        as_of: "",
        projects: [{ requested_id: "active", project_id: "active", resources, sync: null }],
        unavailable: [],
      };
    },
    onLostAccess: () => {},
    clock: time.clock,
    visibility: { isHidden: () => false, listen: () => () => {} },
  });
  store.register("tab", {
    key: { project: "active", name: "questions" },
    fetch: async () => {
      reads += 1;
      throw new Error("read failed");
    },
  });
  scheduler.start();
  await time.advance(10);
  assert.ok(reads >= 1);
  fail = true;
  const before = requests.length;
  await time.advance(1000 + 3000 + 6000);
  assert.deepEqual(
    requests.slice(before).map((at) => at - requests[before - 1]),
    [1000, 4000, 10_000],
  );
  assert.ok(reads > 2, "read retries still run while the watermark backs off");
  scheduler.stop();
});

test("valid_until is anchored to the server's as_of, not the browser clock", async () => {
  const time = fakeClock();
  const requests = [];
  const scheduler = new RefreshScheduler({
    store: new ResourceStore(() => time.clock.now()),
    clientId: "c",
    openProjects: () => [
      { projectId: "active", currentRef: "main" },
      { projectId: "hidden", currentRef: "main" },
    ],
    activeProjectId: () => "active",
    send: async (request) => {
      requests.push(request.due);
      // The server clock runs an hour ahead; the deadline is 1.5 s after its now.
      return {
        epoch: "e",
        as_of: new Date(Date.parse("2026-01-01T01:00:00.000Z") + time.clock.now()).toISOString(),
        projects: [
          {
            requested_id: "hidden",
            project_id: "hidden",
            resources: [
              { name: "questions", version: "1", valid_until: "2026-01-01T01:00:01.500Z" },
            ],
            sync: { last_completed_at: null, status: "local", error: null },
          },
        ],
        unavailable: [],
      };
    },
    onLostAccess: () => {},
    clock: time.clock,
    visibility: { isHidden: () => false, listen: () => () => {} },
  });
  scheduler.start();
  await time.advance(2500);
  scheduler.stop();
  // Beats at 0 (all), 1000 (active), 2000 (hidden due: deadline 1500 passed).
  assert.deepEqual(requests[2], ["active", "hidden"]);
});

test("with a scheduler, a new key reads once, after its first watermark", async () => {
  const time = fakeClock();
  const store = new ResourceStore(() => time.clock.now());
  const scheduler = new RefreshScheduler({
    store,
    clientId: "c",
    openProjects: () => [{ projectId: "p", currentRef: "main" }],
    activeProjectId: () => "p",
    send: async () => ({
      epoch: "e",
      as_of: new Date(0).toISOString(),
      projects: [
        {
          requested_id: "p",
          project_id: "p",
          resources: [{ name: "chats", version: "7", valid_until: null }],
          sync: { last_completed_at: null, status: "local", error: null },
        },
      ],
      unavailable: [],
    }),
    onLostAccess: () => {},
    clock: time.clock,
    visibility: { isHidden: () => false, listen: () => () => {} },
  });
  const { reads, profile } = manualProfile("chats");
  store.register("tab", profile);
  assert.equal(reads.length, 0);
  scheduler.start();
  await time.advance(1);
  assert.equal(reads.length, 1);
  reads[0].resolve("v7");
  await time.advance(2500);
  scheduler.stop();
  assert.equal(reads.length, 1);
});

test("an unavailable project loses its data and its tab", async () => {
  const time = fakeClock();
  const store = new ResourceStore(() => time.clock.now());
  const { profile } = manualProfile("chats", "gone");
  const id = store.register("tab", profile);
  const lost = [];
  const { closeProjectTab } = await server.ssrLoadModule("/src/projects/projectTabs.ts");
  let tabs = [{ id: "gone", name: "Gone" }];
  let active = "gone";
  const viewports = new Map([["gone", {}]]);
  const scheduler = new RefreshScheduler({
    store,
    clientId: "c",
    openProjects: () => [{ projectId: "gone", currentRef: "main" }],
    activeProjectId: () => "gone",
    send: async () => ({ epoch: "e", as_of: "", projects: [], unavailable: ["gone"] }),
    onLostAccess: (projectId) => {
      lost.push(projectId);
      const closed = closeProjectTab(tabs, active, projectId);
      tabs = closed.tabs;
      active = closed.activeProjectId;
      viewports.delete(projectId);
    },
    clock: time.clock,
    visibility: { isHidden: () => false, listen: () => () => {} },
  });
  scheduler.start();
  await time.advance(1);
  scheduler.stop();
  assert.deepEqual(lost, ["gone"]);
  assert.deepEqual(tabs, []);
  assert.equal(active, null);
  assert.equal(viewports.has("gone"), false);
  assert.equal(store.view(id).loading, true);
});

test("a stalled read times out and frees its slot", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const store = new ResourceStore();
  store.setActiveProject("p");
  const stalled = ["a", "b", "c"].map((name) =>
    store.register("tab", { key: key(name), fetch: () => new Promise(() => {}) }),
  );
  const { reads, profile } = manualProfile("d");
  store.register("tab", profile);
  assert.equal(reads.length, 0);
  t.mock.timers.tick(RESOURCE_READ_TIMEOUT_MS);
  await settle();
  assert.equal(reads.length, 1);
  assert.ok(store.view(stalled[0]).stale);
});

const observed = (store, version = "1", name = "chats", ticket) =>
  store.observe("p", [{ name, version }], ticket ?? store.beginObservation("p"));

async function retainedPresentation() {
  const store = new ResourceStore();
  store.attachScheduler(() => {});
  store.observeEpoch("e1");
  const { reads, profile } = manualProfile("chats");
  const id = store.register("cache", profile);
  observed(store);
  reads[0].resolve("retained");
  await settle();
  return { store, reads, profile, id };
}

test("a returning presentation waits for a post-entry watermark and reuses unchanged cache", async () => {
  const { store, reads, id } = await retainedPresentation();
  assert.equal(store.presentationView("panel", "return").status, "loading");
  const old = store.beginObservation("p");
  store.beginPresentation("panel", "return", [id]);
  observed(store, "1", "chats", old);
  assert.equal(store.presentationView("panel", "return").status, "loading");
  assert.deepEqual(store.presentationView("panel", "return").views, []);
  observed(store);
  assert.equal(store.presentationView("panel", "return").status, "ready");
  assert.equal(store.presentationView("panel", "return").views[0].value, "retained");
  assert.equal(reads.length, 1);
});

test("changed entry data waits for its matching accepted read while active owners stay visible", async () => {
  const { store, reads, id } = await retainedPresentation();
  const active = store.beginPresentation("active", "first", [id]);
  observed(store);
  assert.equal(store.presentationView("active", "first").status, "ready");
  const returned = store.beginPresentation("returning", "second", [id]);
  observed(store, "2");
  assert.equal(store.presentationView("returning", "second").status, "loading");
  assert.equal(store.presentationView("active", "first").views[0].value, "retained");
  store.endPresentation(active);
  reads[1].resolve("current");
  await settle();
  assert.equal(store.presentationView("returning", "second").views[0].value, "current");
  store.endPresentation(returned);
});

test("ticket cleanup cannot retire a replacement entry and a new epoch hides admitted values", async () => {
  const { store, reads, id } = await retainedPresentation();
  const old = store.beginPresentation("panel", "old", [id]);
  store.beginPresentation("panel", "new", [id]);
  store.endPresentation(old);
  observed(store);
  assert.equal(store.presentationView("panel", "new").status, "ready");
  store.observeEpoch("e2");
  assert.equal(store.presentationView("panel", "new").status, "loading");
  observed(store);
  reads.at(-1).resolve("new epoch");
  await settle();
  assert.equal(store.presentationView("panel", "new").views[0].value, "new epoch");
});

test("failed entry validation exposes an error without retained payload and recovers", async () => {
  const { store, reads, id } = await retainedPresentation();
  store.beginPresentation("panel", "return", [id]);
  const request = store.beginObservation("p");
  const offline = new Error("offline");
  store.failObservation(request, offline);
  assert.equal(store.presentationView("panel", "return").error, offline);
  assert.deepEqual(store.presentationView("panel", "return").views, []);
  observed(store, "2");
  reads[1].reject(offline);
  await settle();
  assert.equal(store.presentationView("panel", "return").status, "error");
  store.invalidate(id);
  reads[2].resolve("recovered");
  await settle();
  assert.equal(store.presentationView("panel", "return").views[0].value, "recovered");
});

test("dormant return reuses matching cache and preserves another owner's admission", async () => {
  const { store, reads, profile, id } = await retainedPresentation();
  store.beginPresentation("active", "first", [id]);
  observed(store);
  store.suspend("cache", [id]);
  store.register("cache", profile);
  store.beginPresentation("panel", "return", [id]);
  observed(store);
  assert.equal(store.presentationView("panel", "return").status, "ready");
  assert.equal(store.presentationView("active", "first").views[0].value, "retained");
  assert.equal(reads.length, 1);
});

test("without a scheduler an accepted pre-entry read cannot validate a new presentation", async () => {
  const store = new ResourceStore();
  const { reads, profile } = manualProfile("chats");
  const id = store.register("cache", profile);
  store.beginPresentation("panel", "return", [id]);
  reads[0].resolve("old request");
  await settle();
  assert.equal(store.presentationView("panel", "return").status, "loading");
  reads[1].resolve("current request");
  await settle();
  assert.equal(store.presentationView("panel", "return").views[0].value, "current request");
});

test("admitted subscribers retain their value during polling and pending mutations", async () => {
  const { store, reads, id } = await retainedPresentation();
  store.beginPresentation("panel", "active", [id]);
  observed(store);
  const presentations = [];
  const unsubscribe = store.subscribe(() =>
    presentations.push(store.presentationView("panel", "active")),
  );
  const initial = store.presentationView("panel", "active");
  observed(store);
  assert.equal(store.presentationView("panel", "active"), initial);
  const mutation = store.beginMutation("intent", new Map([[id, () => "pending intent"]]));
  assert.deepEqual(store.presentationView("panel", "active").views[0].pending, ["intent"]);
  assert.equal(store.presentationView("panel", "active").views[0].value, "pending intent");
  mutation.fail();
  observed(store, "2");
  assert.equal(store.presentationView("panel", "active").views[0].value, "retained");
  reads.at(-1).resolve("current");
  await settle();
  assert.ok(presentations.length > 0);
  assert.ok(
    presentations.every((view) => view.status === "ready" && view.views[0].value !== undefined),
  );
  unsubscribe();
});
