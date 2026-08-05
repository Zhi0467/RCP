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
const { experimentWatcherStatusTransitioned, terminalTaskNeedsAuthoritativeProjectReload } =
  await server.ssrLoadModule("/src/App.tsx");

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

test("an Experiment watcher status transition requires fresh control state", () => {
  const watcher = {
    watcher_id: "watcher-1",
    status: "active",
    continuation: { patch_kind: "experiment_loop" },
  };
  const ordinaryWatcher = {
    watcher_id: "watcher-2",
    status: "active",
    continuation: { patch_kind: "work" },
  };

  assert.equal(experimentWatcherStatusTransitioned([watcher], [watcher]), false);
  assert.equal(
    experimentWatcherStatusTransitioned([watcher], [{ ...watcher, status: "completed" }]),
    true,
  );
  assert.equal(
    experimentWatcherStatusTransitioned(
      [ordinaryWatcher],
      [{ ...ordinaryWatcher, status: "completed" }],
    ),
    false,
  );
});
