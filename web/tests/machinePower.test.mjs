import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "vite";
import { showMachinePowerCard } from "../src/machinePower.ts";
import { loadMachinePower, updateMachinePower } from "../src/api.ts";

const status = {
  supported: true,
  idle_hold: { enabled: true, active: true },
  demand_reasons: ["episode"],
};

test("the card requires personal space and a supported status", () => {
  assert.equal(showMachinePowerCard("personal", status), true);
  assert.equal(showMachinePowerCard("personal", { ...status, supported: false }), false);
  assert.equal(showMachinePowerCard("team", status), false);
  assert.equal(showMachinePowerCard(undefined, status), false);
  assert.equal(showMachinePowerCard("personal", null), false);
});

test("machine power requests use the contract routes and return backend status", async (t) => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (path, init) => {
    requests.push([path, init?.method ?? "GET", init?.body ? JSON.parse(init.body) : null]);
    return Response.json(status);
  });
  assert.deepEqual(await loadMachinePower(), status);
  assert.deepEqual(await updateMachinePower({ idle_hold: false }), status);
  assert.deepEqual(requests, [
    ["/api/machine-power", "GET", null],
    ["/api/machine-power", "PUT", { idle_hold: false }],
  ]);
});

test("Settings polling follows the hold and stops on cleanup", async (t) => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    logLevel: "silent",
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true },
  });
  let scheduled = null;
  let data = status;
  const received = [];
  const errors = [];
  t.mock.method(globalThis, "fetch", async () => Response.json(data));
  const previousWindow = globalThis.window;
  globalThis.window = {
    setTimeout(callback, delay) {
      scheduled = { callback, delay };
      return 1;
    },
    clearTimeout() {
      scheduled = null;
    },
  };
  try {
    const { startMachinePowerPolling } = await server.ssrLoadModule(
      "/src/hooks/useMachinePower.ts",
    );
    const { EXPERIMENT_BOARD_POLL_DELAY_MS } = await server.ssrLoadModule(
      "/src/hooks/useProjectTabs.ts",
    );
    const stop = startMachinePowerPolling(
      (next) => received.push(next),
      (error) => errors.push(error),
    );
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(scheduled.delay, EXPERIMENT_BOARD_POLL_DELAY_MS);
    assert.equal(received.at(-1).idle_hold.active, true);
    data = { ...status, idle_hold: { enabled: true, active: false }, demand_reasons: [] };
    scheduled.callback();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(received.at(-1).idle_hold.active, false);
    // An in-flight read must not update state or reschedule after unmount.
    scheduled.callback();
    stop();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(scheduled, null);
    assert.equal(received.length, 2);
    assert.deepEqual(errors, []);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    await server.close();
  }
});
