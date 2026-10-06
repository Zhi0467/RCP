import assert from "node:assert/strict";
import test from "node:test";
import { dependencyView } from "../src/projects/machineDependenciesModel.ts";

const program = (name, required) => ({
  name,
  purpose: "p",
  required,
  feature: required ? "" : "f",
  fallback: required ? "" : "b",
});

const base = {
  outcome: "ready",
  platform: "linux",
  distribution: "ubuntu",
  tested: true,
  missing: [],
  install_command: null,
  install_notes: [],
  reason: null,
  checked_at: "2026-10-05T00:00:00Z",
};

test("missing programs split into required and optional", () => {
  const view = dependencyView({
    ...base,
    outcome: "missing",
    missing: [program("rsync", true), program("tmux", false), program("git", true)],
    install_command: "sudo apt-get install -y rsync tmux git",
  });
  assert.deepEqual(
    view.required.map((entry) => entry.name),
    ["rsync", "git"],
  );
  assert.deepEqual(
    view.optional.map((entry) => entry.name),
    ["tmux"],
  );
  assert.equal(view.command, "sudo apt-get install -y rsync tmux git");
  assert.deepEqual(view.notes, []);
});

test("an untested distribution is flagged, and notes replace a missing command", () => {
  const view = dependencyView({
    ...base,
    distribution: "rocky",
    tested: false,
    missing: [program("tmux", false)],
    install_notes: ["step one", "step two"],
  });
  assert.equal(view.untested, true);
  assert.equal(view.required.length, 0);
  assert.equal(view.command, null);
  assert.equal(view.notes.length, 2);
});

test("a ready machine offers no install, and only unsupported or not checked carry a reason", () => {
  const ready = dependencyView(base);
  assert.equal(ready.untested, false);
  assert.equal(ready.command, null);
  assert.equal(ready.reason, null);
  for (const outcome of ["unsupported", "not_checked"]) {
    const view = dependencyView({
      ...base,
      outcome,
      platform: null,
      distribution: null,
      reason: "r",
    });
    assert.equal(view.reason, "r");
    assert.equal(view.system, null);
  }
});
