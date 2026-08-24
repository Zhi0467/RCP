import assert from "node:assert/strict";
import test from "node:test";

import { repositoryPickerPresentation, stateRepositoryAfterRemoval } from "../src/projectSetup.ts";

const repositories = [
  { id: 1, alias: "research" },
  { id: 2, alias: "analysis" },
  { id: 3, alias: "paper" },
];

test("removing the canonical repository selects the first remaining alias", () => {
  assert.equal(stateRepositoryAfterRemoval(repositories, 1, "research"), "analysis");
});

test("repository removal preserves another selection and handles an empty remainder", () => {
  assert.equal(stateRepositoryAfterRemoval(repositories, 2, "research"), "research");
  assert.equal(stateRepositoryAfterRemoval([repositories[0]], 1, "research"), "");
});

test("only desktop local repositories offer the native folder picker", () => {
  assert.deepEqual(repositoryPickerPresentation("local", true), {
    showPicker: true,
    hint: null,
  });
  assert.deepEqual(repositoryPickerPresentation("local", false), {
    showPicker: false,
    hint: "Paste an absolute path. Finder selection is available in the desktop app.",
  });
  assert.deepEqual(repositoryPickerPresentation("ssh", true), {
    showPicker: false,
    hint: null,
  });
  assert.deepEqual(repositoryPickerPresentation("ssh", false), {
    showPicker: false,
    hint: null,
  });
});
