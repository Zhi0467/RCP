import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
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
  handleAutoResearchDialogKeyDown,
  makeAutoResearchDialogBackgroundInert,
  restoreAutoResearchDialogFocus,
} = await server.ssrLoadModule("/src/experiments/AutoResearchDialog.tsx");

after(() => server.close());

test("no web frame is granted same-origin access", async () => {
  // Agent HTML previews stay opaque (AGENTS.md invariant 10e): an artifact frame may
  // run its scripts but never share RCP's origin.
  const files = await readdir(new URL("../src/", import.meta.url), { recursive: true });
  for (const file of files.filter((name) => /\.(ts|tsx)$/.test(name))) {
    const source = await readFile(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /allow-same-origin/, file);
  }
});
test("artifact revision review wires the proven keyboard modal lifecycle", () => {
  const focused = [];
  const first = focusTarget("first", focused);
  const last = focusTarget("last", focused);
  const dialog = {
    focus() {
      focused.push("dialog");
    },
    contains(element) {
      return element === first || element === last;
    },
    querySelectorAll() {
      return [first, last];
    },
  };
  const tab = keyEvent("Tab", false);
  assert.equal(
    handleAutoResearchDialogKeyDown(tab, dialog, last, false, () => {}),
    true,
  );
  assert.equal(tab.prevented, true);
  assert.deepEqual(focused, ["first"]);

  let closed = false;
  const busyEscape = keyEvent("Escape", false);
  assert.equal(
    handleAutoResearchDialogKeyDown(busyEscape, dialog, first, true, () => {
      closed = true;
    }),
    false,
  );
  assert.equal(closed, false);
  const escape = keyEvent("Escape", false);
  assert.equal(
    handleAutoResearchDialogKeyDown(escape, dialog, first, false, () => {
      closed = true;
    }),
    true,
  );
  assert.equal(closed, true);
});

test("artifact revision modal inerts background and restores its trigger", () => {
  const background = treeElement(false);
  const dialog = treeElement(false);
  const body = treeElement(false, [background, dialog]);
  dialog.parentElement = body;
  const restore = makeAutoResearchDialogBackgroundInert(dialog);
  assert.equal(background.inert, true);
  restore();
  assert.equal(background.inert, false);

  let restored = false;
  restoreAutoResearchDialogFocus({
    isConnected: true,
    focus() {
      restored = true;
    },
  });
  assert.equal(restored, true);
});

function keyEvent(key, shiftKey) {
  return {
    key,
    shiftKey,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
}

function focusTarget(name, focused) {
  return {
    tabIndex: 0,
    focus() {
      focused.push(name);
    },
    getAttribute() {
      return null;
    },
    hasAttribute() {
      return false;
    },
  };
}

function treeElement(inert, children = []) {
  const element = { inert, children, parentElement: null };
  for (const child of children) child.parentElement = element;
  return element;
}
