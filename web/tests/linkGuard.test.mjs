import assert from "node:assert/strict";
import { test } from "node:test";

import { leavesApp } from "../src/core/linkGuard.ts";

test("only hash routes on the app page stay in place", () => {
  const here = new URL("https://rcp.example:8421/#/projects/a?view=paper");
  assert.equal(leavesApp("#/projects/b", here), false);
  assert.equal(leavesApp("/#/projects/b", here), false);
  assert.equal(leavesApp("sections/methods.md", here), true);
  assert.equal(leavesApp("/api/projects/a/paper/files/f/raw", here), true);
  assert.equal(leavesApp("https://example.org/paper", here), true);
});
