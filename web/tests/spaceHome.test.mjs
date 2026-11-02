import assert from "node:assert/strict";
import test from "node:test";
import { spaceHomeHash } from "../src/projects/spaceHome.ts";

test("a personal home defaults to Agent and restores either remembered tab", () => {
  for (const hash of ["", "#", "#/"]) {
    assert.equal(spaceHomeHash(hash, "personal", null), "#/agent");
    assert.equal(spaceHomeHash(hash, "personal", "agent"), "#/agent");
    assert.equal(spaceHomeHash(hash, "personal", "cards"), hash);
    assert.equal(spaceHomeHash(hash, "personal", "invalid"), "#/agent");
    assert.equal(spaceHomeHash(hash, "team", "agent"), hash);
  }
});

test("explicit destinations win over either remembered personal tab", () => {
  for (const hash of [
    "#/cards",
    "#/agent",
    "#/agent/thread",
    "#/projects/p?view=chats",
    "#/project-groups/g",
  ]) {
    for (const saved of [null, "cards", "agent"]) {
      assert.equal(spaceHomeHash(hash, "personal", saved), hash);
    }
  }
});
