import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";

import {
  PaperTestRoom as Room,
  paperTestCard as card,
  createPaperFixture,
  createPaperTestServer,
} from "./paperTestServer.mjs";

let server;
let browser;
let origin;
before(async () => {
  server = await createPaperTestServer({ port: 0 });
  origin = server.origin;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

function editorMatches(expected) {
  const source = document.querySelector(".cm-content")?.cloneNode(true);
  source
    ?.querySelectorAll(".paper-inline-suggestion, .cm-ySelectionCaret")
    .forEach((widget) => widget.remove());
  return source?.textContent === expected;
}

async function openPaper(
  t,
  { room = new Room(), width = 1280, cards = [], proposedTexts = {}, configure = () => {} } = {},
) {
  const context = await browser.newContext({ viewport: { width, height: 844 } });
  t.after(async () => {
    await context.close();
  });
  const page = await context.newPage();
  const errors = [];
  const fixture = createPaperFixture({ room, cards, proposedTexts });
  configure(fixture);
  const posts = fixture.posts;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await context.route("**/api/**", async (route) => {
    const request = route.request();
    return route.fulfill(
      fixture.respond(new URL(request.url()).pathname, request.method(), request.postDataJSON()),
    );
  });
  await context.routeWebSocket(/\/paper\/files\/[^/]+\/live(?:\?.*)?$/, (socket) =>
    room.connect(socket),
  );
  await page.goto(`${origin}/tests/fixtures/paperResource.html`);
  await page.locator(".cm-content").waitFor();
  await page.waitForFunction(editorMatches, "draft");
  return { page, context, room, errors, posts };
}

async function append(page, text) {
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.insertText(text);
}

async function saved(page, value) {
  await page.waitForFunction(
    (expected) =>
      document.querySelector('[data-testid="paper-saved"]')?.getAttribute("data-saved") ===
      String(expected),
    value,
  );
}

test("Paper file-tree folding persists across reload", async (t) => {
  const { page, errors } = await openPaper(t);
  await page.getByRole("button", { name: "Fold files", exact: true }).click();
  assert.equal(await page.getByTestId("paper-files").isVisible(), false);
  await page.reload();
  await page.locator(".cm-content").waitFor();
  assert.equal(await page.getByTestId("paper-files").isVisible(), false);
  await page.getByRole("button", { name: "Show files", exact: true }).click();
  assert.equal(await page.getByTestId("paper-files").isVisible(), true);
  assert.deepEqual(errors, []);
});

test("Paper clients converge after concurrent edits through the websocket room", async (t) => {
  const room = new Room();
  const first = await openPaper(t, { room });
  const second = await openPaper(t, { room });
  await saved(first.page, true);
  await saved(second.page, true);
  room.hold = true;
  const updates = room.updatesReceived;
  await Promise.all([append(first.page, " A"), append(second.page, " B")]);
  await room.waitForUpdates(updates + 2);
  room.flush();
  const expected = room.doc.getText("text").toString();
  assert.ok(expected.includes(" A") && expected.includes(" B"));
  for (const client of [first, second]) {
    await client.page.waitForFunction(editorMatches, expected);
    await saved(client.page, true);
    assert.deepEqual(client.errors, []);
  }
});

test("Paper Saved waits for a covering durable acknowledgement", async (t) => {
  const { page, room, errors } = await openPaper(t);
  await saved(page, true);
  room.acknowledge = false;
  const updates = room.updatesReceived;
  await append(page, " pending");
  await room.waitForUpdates(updates + 1);
  await saved(page, false);
  assert.equal(room.doc.getText("text").toString(), "draft pending");
  room.ack();
  await saved(page, true);
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("Backspace");
  // Deletion carries a separate causal marker because a delete set alone has
  // no state-vector clock. Wait for that clock before acknowledging it.
  await room.waitForUpdates(updates + 3);
  await saved(page, false);
  room.ack();
  await saved(page, true);
  assert.deepEqual(errors, []);
});

test("Paper pending local edits survive reload before the server receives them", async (t) => {
  const { page, room, errors } = await openPaper(t);
  await saved(page, true);
  room.hold = true;
  room.acknowledge = false;
  await append(page, " offline");
  await saved(page, false);
  // Wait for IndexedDB's transaction queue, without depending on a debounce timer.
  await page.evaluate(async () => {
    for (const { name } of await indexedDB.databases()) {
      if (!name?.includes("paper")) continue;
      await new Promise((resolve, reject) => {
        const request = indexedDB.open(name);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result;
          if (!db.objectStoreNames.length) {
            db.close();
            resolve();
            return;
          }
          const transaction = db.transaction([...db.objectStoreNames], "readwrite");
          transaction.oncomplete = () => {
            db.close();
            resolve();
          };
          transaction.onerror = () => reject(transaction.error);
        };
      });
    }
  });
  assert.equal(room.doc.getText("text").toString(), "draft");
  await page.reload();
  await page.waitForFunction(editorMatches, "draft offline");
  await saved(page, false);
  assert.deepEqual(errors, []);
});

test("Paper change acceptance posts to the group endpoint", async (t) => {
  const { page, posts, errors } = await openPaper(t, { cards: [{ ...card }] });
  await page.getByRole("button", { name: "Coach", exact: true }).last().click();
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/changes/g1/accept") && response.request().method() === "POST",
  );
  await page
    .locator('[data-change-id="c1"]')
    .getByRole("button", { name: "Accept change", exact: true })
    .click();
  await accepted;
  assert.equal(posts[0]?.path, "/api/projects/p/paper/changes/g1/accept");
  assert.deepEqual(errors, []);
});

test("Paper fits 390px in Write, Preview, Coach and the file panel", async (t) => {
  const { page, errors } = await openPaper(t, { width: 390, cards: [{ ...card }] });
  for (const panel of ["Write", "Preview", "Coach"]) {
    await page.getByRole("button", { name: panel, exact: true }).first().click();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  await page.getByRole("button", { name: "Show files", exact: true }).click();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.deepEqual(errors, []);
});

test("Paper refuses editing when local persistence is unavailable and exposes retry", async (t) => {
  const context = await browser.newContext();
  t.after(() => context.close());
  const fixture = createPaperFixture();
  await context.route("**/api/**", (route) =>
    route.fulfill(fixture.respond(new URL(route.request().url()).pathname)),
  );
  await context.addInitScript(() => {
    IDBFactory.prototype.open = () => {
      throw new DOMException("Storage denied", "SecurityError");
    };
  });
  const page = await context.newPage();
  await page.goto(`${origin}/tests/fixtures/paperResource.html`);
  await page.getByRole("button", { name: "Reconnect Paper", exact: true }).waitFor();
  assert.equal(await page.locator(".cm-content").count(), 0);
});

test("rewrite comparison reads the complete proposed file and fits 390px", async (t) => {
  const proposed = Array.from({ length: 100 }, (_, index) => `proposed line ${index}`).join("\n");
  const { page, errors } = await openPaper(t, {
    width: 390,
    cards: [
      {
        ...card,
        kind: "rewrite",
        preview: { removed: ["draft"], added: ["preview only"], more: 99 },
      },
    ],
    proposedTexts: { c1: proposed },
  });
  await page.getByRole("button", { name: "Coach", exact: true }).first().click();
  await page.getByRole("button", { name: "Compare", exact: true }).click();
  const texts = page.getByRole("dialog", { name: "Compare change" }).locator("pre");
  await texts.nth(1).waitFor();
  assert.deepEqual(await texts.allTextContents(), ["draft", proposed]);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.deepEqual(errors, []);
});

test("cancelling failed delete and rename removes their retry authority", async (t) => {
  const { page } = await openPaper(t);
  for (const operation of ["Delete", "Rename"]) {
    await page.getByRole("button", { name: `${operation} file`, exact: true }).click();
    if (operation === "Rename") {
      await page.getByRole("textbox", { name: "File path" }).fill("renamed.md");
      await page.getByRole("button", { name: "Save file path" }).click();
    } else await page.getByRole("button", { name: "Delete", exact: true }).click();
    await page.locator(".paper-files.paper-failed").waitFor();
    await page
      .getByRole("button", {
        name: operation === "Rename" ? "Cancel file path" : "Cancel",
        exact: true,
      })
      .click();
    assert.equal(await page.getByRole("button", { name: "Retry file operation" }).count(), 0);
    assert.equal(await page.locator(".paper-files.paper-failed").count(), 0);
  }
});

test("Locate waits for an unopened file's server text", async (t) => {
  const { page, context } = await openPaper(t, {
    cards: [{ ...card, file_id: "f2", lines: { start: 3, end: 3 } }],
    configure: ({ tree }) =>
      tree.files.push({ ...tree.files[0], file_id: "f2", path: "second.md" }),
  });
  const room = new Room();
  room.doc.getText("text").insert(0, "one\ntwo\n");
  room.hold = true;
  await context.routeWebSocket(/\/files\/f2\/live$/, (socket) => room.connect(socket));
  await page.getByRole("button", { name: "Coach", exact: true }).last().click();
  await page.getByRole("button", { name: "Locate change", exact: true }).click();
  await page.waitForFunction(() => document.querySelector(".cm-content")?.textContent === "");
  room.flush();
  await page.waitForFunction(() =>
    document.querySelector(".paper-position")?.textContent.includes("Ln 3,"),
  );
});

test("superseded LaTeX compilation keeps the existing PDF", async (t) => {
  let currentTree;
  const { page, context, room } = await openPaper(t, {
    configure: ({ tree }) => {
      currentTree = tree;
      tree.files.push({ ...tree.files[0], file_id: "f2", path: "paper.tex", format: "latex" });
    },
  });
  let compiled = 0;
  await context.route("**/paper/compile", (route) =>
    route.fulfill({
      json:
        ++compiled === 1
          ? { status: "ok", pdf_url: "/api/projects/p/paper/compile/good/pdf" }
          : { status: "superseded", pdf_url: null },
    }),
  );
  await page.getByRole("button", { name: "paper.tex", exact: true }).click();
  await page.locator(".paper-pdf").waitFor();
  const next = page.waitForResponse(
    (reply) => reply.url().endsWith("/paper/compile") && compiled === 2,
  );
  currentTree.saved_revision += 1;
  await append(page, " changed");
  room.ack();
  const response = await next;
  assert.equal((await response.json()).status, "superseded");
  await page.waitForFunction(
    () => document.querySelector(".paper-compiled-preview")?.getAttribute("aria-busy") === "false",
  );
  assert.equal(await page.locator(".paper-pdf").count(), 1);
  assert.match(
    await page.getByRole("link", { name: "Download PDF" }).getAttribute("href"),
    /compile\/good\/pdf$/,
  );
});
