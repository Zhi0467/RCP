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
const {
  failedTaskActionNeedsAuthoritativeProjectReload,
  loadExperimentWatcherPoll,
  terminalTaskNeedsAuthoritativeProjectReload,
} = await server.ssrLoadModule("/src/App.tsx");

after(() => server.close());

test("terminal Experiment work refetches control state even without a graph revision", () => {
  const completedLoop = {
    status: "succeeded",
    applied_revision: null,
    request: {
      patch_kind: "experiment_loop",
      control_node_id: "experiment/demo",
    },
  };
  const pausedLoop = {
    ...completedLoop,
    status: "paused",
  };
  const ordinaryChat = {
    status: "succeeded",
    applied_revision: null,
    request: { patch_kind: "work" },
  };

  assert.equal(terminalTaskNeedsAuthoritativeProjectReload(completedLoop), true);
  assert.equal(terminalTaskNeedsAuthoritativeProjectReload(pausedLoop), true);
  assert.equal(terminalTaskNeedsAuthoritativeProjectReload(ordinaryChat), false);
  assert.equal(
    terminalTaskNeedsAuthoritativeProjectReload({ ...ordinaryChat, applied_revision: 7 }),
    true,
  );
});

test("failed Experiment Resume and Retry refetch authoritative stop state", () => {
  const experimentLoop = {
    request: { patch_kind: "experiment_loop" },
  };
  const ordinaryWork = {
    request: { patch_kind: "work" },
  };

  assert.equal(failedTaskActionNeedsAuthoritativeProjectReload(experimentLoop, "resume"), true);
  assert.equal(failedTaskActionNeedsAuthoritativeProjectReload(experimentLoop, "retry"), true);
  assert.equal(failedTaskActionNeedsAuthoritativeProjectReload(experimentLoop, "pause"), false);
  assert.equal(failedTaskActionNeedsAuthoritativeProjectReload(ordinaryWork, "retry"), false);
});

test("pending Experiment watcher polling always refreshes control state", async () => {
  const watcher = {
    watcher_id: "watcher-1",
    status: "active",
    notified: false,
    notification_operation_id: null,
    continuation: { patch_kind: "experiment_loop" },
  };
  const requested = [];
  const base = "/api/projects/project-1";
  const result = await loadExperimentWatcherPoll(async (path) => {
    requested.push(path);
    if (path.endsWith("/watchers")) return [watcher];
    if (path.endsWith("/tasks")) return [];
    return { experiment_control: { "experiment/demo": { operational: {} } } };
  }, base);

  assert.deepEqual(requested, [`${base}/watchers`, `${base}/tasks`, base]);
  assert.deepEqual(result.watchers, [watcher]);
  assert.ok(result.project.experiment_control);
});
