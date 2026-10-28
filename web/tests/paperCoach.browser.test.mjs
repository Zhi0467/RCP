import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import {
  createPaperTestServer,
  openPaper,
  paperTestCard as card,
  PaperTestRoom,
} from "./paperTestServer.mjs";

let server, browser;
before(async () => {
  server = await createPaperTestServer();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
const open = (t, options = {}) =>
  openPaper(t, { browser, origin: server.origin, width: 390, ...options });
async function coach(page) {
  await page.getByRole("button", { name: "Coach", exact: true }).first().click();
}
async function redEdge(locator) {
  assert.ok(
    await locator.evaluate((element) => {
      const probe = document.createElement("span");
      probe.style.color = "var(--coral)";
      element.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return getComputedStyle(element).boxShadow.includes(color);
    }),
  );
}
async function fits(page) {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
}
async function target(locator, minimum = 44) {
  const box = await locator.boundingBox();
  assert.ok(box && box.height >= minimum && box.width >= minimum, JSON.stringify(box));
}

test("card refusals have a visible edge and dismiss; transient failures retry", async (t) => {
  const { page, context } = await open(t, { cards: [{ ...card, state: "accepted" }] });
  await coach(page);
  const change = page.locator('[data-change-id="c1"]');
  for (const [state, action] of [
    ["accepted", "Undo change"],
    ["outdated", "Refresh change"],
    ["pending", "Accept change"],
  ]) {
    await context.route("**/paper/changes", (route) =>
      route.fulfill({
        json: { groups: [{ changes: [{ ...card, state }] }] },
      }),
    );
    await page.reload();
    await coach(page);
    let status = 409;
    await context.route("**/changes/g1/*", (route) => route.fulfill({ status, json: {} }));
    await change.getByRole("button", { name: action, exact: true }).click();
    await page.waitForFunction(() =>
      document.querySelector('[data-change-id="c1"]').classList.contains("paper-failure"),
    );
    await redEdge(change);
    await fits(page);
    // A pending or outdated card's own actions are its way forward; only a settled one dismisses.
    const dismiss = change.getByRole("button", { name: "Dismiss change failure" });
    assert.equal(await dismiss.count(), state === "accepted" ? 1 : 0);
    if (state === "accepted") {
      await target(dismiss);
      await dismiss.click();
      assert.equal(await change.evaluate((e) => e.classList.contains("paper-failure")), false);
    }
    status = 503;
    await change.getByRole("button", { name: action, exact: true }).click();
    await change.getByRole("button", { name: "Retry change", exact: true }).waitFor();
    await redEdge(change);
    await context.unroute("**/changes/g1/*");
    await context.unroute("**/paper/changes");
  }
});

test("summary refusals expose one dismiss action and a red edge", async (t) => {
  const { page, context } = await open(t, { cards: [{ ...card, state: "applied" }] });
  await context.route("**/changes/undo-all", (route) => route.fulfill({ status: 409, json: {} }));
  await coach(page);
  await page.getByRole("button", { name: "Undo all", exact: true }).click();
  const summary = page.locator(".paper-change-summary");
  const dismiss = summary.getByRole("button", { name: "Dismiss changes failure" });
  await dismiss.waitFor();
  await redEdge(summary);
  await dismiss.click();
  assert.equal(await summary.evaluate((e) => e.classList.contains("paper-failure")), false);
});

test("outdated cards can be rejected", async (t) => {
  const { page, posts } = await open(t, { cards: [{ ...card, state: "outdated" }] });
  await coach(page);
  await page.getByRole("button", { name: "Reject change", exact: true }).click();
  await page.locator('[data-change-id="c1"][data-state="rejected"]').waitFor();
  assert.ok(posts.some((post) => post.path.endsWith("/g1/reject")));
});

test("accepted cards offer task-level undo", async (t) => {
  const { page, posts } = await open(t, { cards: [{ ...card, state: "accepted" }] });
  await coach(page);
  const response = page.waitForResponse((response) => response.url().endsWith("/undo-all"));
  await page.getByRole("button", { name: "Undo all", exact: true }).click();
  await response;
  assert.deepEqual(posts.find((post) => post.path.endsWith("/undo-all"))?.body, {
    task_id: card.task_id,
  });
});

test("create cards never locate a nonexistent file", async (t) => {
  const { page } = await open(t, {
    cards: [{ ...card, kind: "create", file_id: "new", path: "new.md" }],
  });
  await coach(page);
  const change = page.locator('[data-change-id="c1"]');
  await change.waitFor();
  assert.equal(await change.getByRole("button", { name: "Locate change" }).count(), 0);
});

test("card line coordinates shift with the live anchor", async (t) => {
  const { page } = await open(t, {
    cards: [
      {
        ...card,
        lines: { start: 8, end: 10 },
        current: { start: 0, end: 5, revision: 1, text: "draft", line: 12 },
      },
    ],
  });
  await coach(page);
  // Compare coordinates, not the surrounding UI wording.
  const label = await page.locator(".paper-change-name > span").textContent();
  assert.deepEqual(label.match(/\d+/g).map(Number), [12, 14]);
});

test("inline suggestions precede trailing newlines and have usable desktop and phone actions", async (t) => {
  for (const width of [1280, 390]) {
    const room = new PaperTestRoom();
    room.doc.getText("text").insert(5, "\nnext paragraph");
    const { page, errors, posts } = await open(t, {
      width,
      room,
      initialText: "draft\nnext paragraph",
      cards: [
        {
          ...card,
          current: { start: 0, end: 6, revision: 1, text: "draft\n", line: 1 },
        },
      ],
    });
    const widget = page.locator(".paper-inline-suggestion");
    await widget.waitFor();
    assert.equal(
      await widget.evaluate((e) => e.closest(".cm-line").nextElementSibling.textContent),
      "next paragraph",
    );
    assert.ok(await widget.evaluate((e) => parseFloat(getComputedStyle(e).marginRight) > 0));
    for (const action of ["accept", "reject"])
      await target(
        widget.getByRole("button", { name: action + " suggestion" }),
        width === 390 ? 44 : 28,
      );
    await fits(page);
    await widget.getByRole("button", { name: "reject suggestion" }).click();
    await widget.waitFor({ state: "detached" });
    assert.ok(posts.some((post) => post.path.endsWith("/g1/reject")));
    assert.deepEqual(errors, []);
  }
});

test("oversized UTF-8 edits leave the document editable and synchronized", async (t) => {
  const { page, room, errors } = await open(t);
  // Mount the editor with its explicit limit contract, independently of Workspace wiring.
  await page.evaluate(async () => {
    const { React, createRoot } = await import("/tests/fixtures/paperReact.ts");
    const { PaperEditor } = await import("/src/paper/PaperEditor.tsx");
    const { acquirePaperDocument } = await import("/src/paper/paperDocuments.ts");
    const owner = acquirePaperDocument(
      { backend: "limit-test", member: "member", project: "p", file: "f1" },
      "/api/projects/p",
      "Member",
    );
    document.querySelector("#root").style.display = "none";
    const host = document.body.appendChild(document.createElement("div"));
    host.id = "limited";
    function Limited() {
      const snapshot = React.useSyncExternalStore(owner.subscribe, owner.getSnapshot);
      return snapshot.ready
        ? React.createElement(PaperEditor, {
            owner,
            maxBytes: 9,
            format: "markdown",
            changes: [],
            focusChange: null,
            onDecide: () => {},
          })
        : null;
    }
    createRoot(host).render(React.createElement(Limited));
  });
  const editor = page.locator("#limited .cm-content");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content")?.textContent === "draft",
  );
  await editor.click();
  await page.keyboard.press("ControlOrMeta+End");
  const updates = room.updatesReceived;
  await page.keyboard.insertText("😀😀");
  assert.equal(await editor.textContent(), "draft");
  assert.equal(room.doc.getText("text").toString(), "draft");
  assert.equal(room.updatesReceived, updates);
  assert.ok(
    await page.locator("#limited .paper-code").evaluate((e) => e.getAnimations().length > 0),
  );
  await page.keyboard.insertText("😀");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content").textContent === "draft😀",
  );
  await room.waitForUpdates(updates + 1);
  assert.equal(room.doc.getText("text").toString(), "draft😀");
  assert.equal(await editor.getAttribute("contenteditable"), "true");
  // Undo restoring deleted text past the limit after a peer filled the room is refused too.
  // Wait out Y.UndoManager's 500 ms capture window so the deletion is its own undo step.
  await page.waitForTimeout(600);
  await page.keyboard.press("Backspace");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content").textContent === "draft",
  );
  room.doc.getText("text").insert(0, "abcd");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content").textContent === "abcddraft",
  );
  const beforeUndo = room.updatesReceived;
  await page.keyboard.press("ControlOrMeta+z");
  assert.equal(await editor.textContent(), "abcddraft");
  assert.equal(room.doc.getText("text").toString(), "abcddraft");
  assert.equal(room.updatesReceived, beforeUndo);
  assert.deepEqual(errors, []);
});

test("phone coach agent and branch pickers are 44px targets", async (t) => {
  const { page } = await open(t);
  await coach(page);
  await target(page.locator(".paper-composer-actions .agent-config-chip"));
  // The shared fixture mounts the workspace without the project-shell branch picker.
  await page.evaluate(async () => {
    const { React, createRoot } = await import("/tests/fixtures/paperReact.ts");
    const { GraphPicker } = await import("/src/graph/GraphPicker.tsx");
    const host = document.createElement("div");
    document.body.prepend(host);
    createRoot(host).render(
      React.createElement(GraphPicker, {
        refs: [],
        activeRef: { kind: "main" },
        projectId: "p",
        compact: true,
        onSelect: () => {},
        onRefUpdated: () => {},
        onCreate: async () => {},
      }),
    );
  });
  await page.locator(".branch-picker-trigger").waitFor();
  await target(page.locator(".branch-picker-trigger"));
  await fits(page);
});

test("on a touch phone Return writes a new line and a tap outside closes chat history", async (t) => {
  const { page } = await open(t, { touch: true });
  await coach(page);
  const message = page.getByRole("textbox", { name: "Message" });
  await message.tap();
  await page.keyboard.type("one");
  await page.keyboard.press("Enter");
  await page.keyboard.type("two");
  assert.equal(await message.inputValue(), "one\ntwo");
  await page.getByRole("button", { name: "Coach chat history" }).tap();
  await page.locator(".paper-session-list").waitFor();
  await page.locator(".paper-coach-transcript").tap();
  await page.locator(".paper-session-list").waitFor({ state: "detached" });
});
