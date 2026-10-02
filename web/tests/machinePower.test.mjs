import assert from "node:assert/strict";
import { test } from "node:test";
import { machinePowerWarnings, showMachinePowerCard } from "../src/machinePower.ts";
import {
  installMachinePower,
  loadMachinePower,
  uninstallMachinePower,
  updateMachinePower,
} from "../src/api.ts";

const status = {
  platform: "macos",
  supported: true,
  installed: true,
  install_problem: null,
  idle_hold: { enabled: true, active: true },
  lid_mode: { enabled: true, active: false },
  demand: true,
  demand_reasons: ["episode"],
  latched: null,
  last_release: { cause: "battery_floor", at: "2026-10-01T03:12:00Z" },
  cleanup_failure: null,
  external_owner: false,
};

test("the card requires personal space and a supported status", () => {
  assert.equal(showMachinePowerCard("personal", status), true);
  assert.equal(showMachinePowerCard("personal", { ...status, supported: false }), false);
  assert.equal(showMachinePowerCard("team", status), false);
  assert.equal(showMachinePowerCard(undefined, status), false);
  assert.equal(showMachinePowerCard("personal", null), false);
});

test("home warnings project latches and cleanup independently, preserving the command", () => {
  assert.deepEqual(machinePowerWarnings(null), { latch: null, cleanup: null });
  assert.deepEqual(machinePowerWarnings(status), { latch: null, cleanup: null });
  for (const latched of [null, "thermal", "cleanup_failure"]) {
    for (const kind of [null, "clear_failed", "sleep_failed"]) {
      const cleanup = kind ? { kind, command: "sudo pmset -a disablesleep 0" } : null;
      assert.deepEqual(machinePowerWarnings({ ...status, latched, cleanup_failure: cleanup }), {
        latch: latched,
        cleanup,
      });
    }
  }
});

test("machine power requests use the contract routes and return backend status", async (t) => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (path, init) => {
    requests.push([path, init?.method ?? "GET", init?.body ? JSON.parse(init.body) : null]);
    return Response.json(status);
  });
  assert.deepEqual(await loadMachinePower(), status);
  assert.deepEqual(await updateMachinePower({ idle_hold: false }), status);
  assert.deepEqual(await updateMachinePower({ lid_mode: true }), status);
  assert.deepEqual(await installMachinePower(), status);
  assert.deepEqual(await uninstallMachinePower(), status);
  assert.deepEqual(requests, [
    ["/api/machine-power", "GET", null],
    ["/api/machine-power", "PUT", { idle_hold: false }],
    ["/api/machine-power", "PUT", { lid_mode: true }],
    ["/api/machine-power/install", "POST", null],
    ["/api/machine-power/uninstall", "POST", null],
  ]);
});
