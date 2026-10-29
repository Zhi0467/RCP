import assert from "node:assert/strict";
import test from "node:test";

import { rememberSubview, tabEntryView, topTab } from "../src/core/viewTabs.ts";

test("each top tab reopens the sub-view last open there, per project", () => {
  const values = new Map();
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  try {
    assert.equal(tabEntryView("a", "artifacts"), "artifacts");
    for (const view of ["paper", "chats", "dag", "overview"]) rememberSubview("a", view);
    assert.equal(tabEntryView("a", "artifacts"), "paper");
    assert.equal(tabEntryView("a", "scientific"), "dag");
    assert.equal(tabEntryView("b", "artifacts"), "artifacts");
    assert.deepEqual(
      [...values.keys()].map((key) => key.split(":").at(-1)),
      ["artifacts", "scientific"],
    );
    rememberSubview("a", "artifacts");
    assert.equal(tabEntryView("a", "artifacts"), "artifacts");
    assert.equal(topTab("paper"), "artifacts");

    globalThis.localStorage.getItem = () => {
      throw new DOMException("denied", "SecurityError");
    };
    assert.equal(tabEntryView("a", "scientific"), "scientific");
  } finally {
    delete globalThis.localStorage;
  }
});
