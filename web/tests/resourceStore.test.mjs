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
const { ResourceStore, resourceKeyId } = await server.ssrLoadModule("/src/core/resourceStore.ts");
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
