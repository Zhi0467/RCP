import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";

import {
  PaperTestRoom as Room,
  paperTestCard as card,
  createPaperFixture,
  createPaperTestServer,
  editorMatches,
  openPaper as openPaperPage,
  pdfBytes,
} from "./paperTestServer.mjs";

let server;
let browser;
let origin;
before(async () => {
  server = await createPaperTestServer();
  origin = server.origin;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

const openPaper = (t, options = {}) => openPaperPage(t, { browser, origin, ...options });

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

test("rewrite comparison reads the complete proposed file", async (t) => {
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
    cards: [
      {
        ...card,
        file_id: "f2",
        lines: { start: 3, end: 3 },
        current: { start: 8, end: 13, revision: 1, text: "draft" },
      },
    ],
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

test("insertion refreshes use current anchors without downloading base files", async (t) => {
  const insertion = {
    ...card,
    current: { start: 5, end: 5, revision: 1, text: "" },
    preview: { removed: [], added: ["suggested"], more: 0 },
  };
  let currentTree;
  const { page, requests } = await openPaper(t, {
    cards: [insertion],
    configure: ({ tree }) => {
      currentTree = tree;
    },
  });
  await page.locator(".paper-inline-suggestion").waitFor();
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+Home");
  const refreshed = page.waitForResponse((response) => response.url().endsWith("/paper/changes"));
  await page.keyboard.insertText("prefix ");
  insertion.current = { start: 12, end: 12, revision: 2, text: "" };
  currentTree.revision = 2;
  currentTree.saved_revision = 2;
  await refreshed;
  await page.locator(".paper-inline-suggestion").waitFor();
  assert.equal(await page.locator(".paper-inline-suggestion").count(), 1);
  assert.deepEqual(
    requests.filter((url) => /\/raw\?revision=/.test(url)),
    [],
  );
});

test("uploading an existing name replaces it, and a failed upload keeps the file actions", async (t) => {
  const { page, context } = await openPaper(t, {
    width: 390,
    configure: ({ tree }) =>
      tree.files.push({
        file_id: "img",
        path: "figure.png",
        kind: "binary",
        format: "image",
        size: 5,
        sha256: "a",
      }),
  });
  await page.getByRole("button", { name: "Show files", exact: true }).click();
  let attempts = 0;
  await context.route("**/paper/files/img/content", (route) => {
    attempts += 1;
    assert.equal(route.request().method(), "PUT");
    return route.fulfill({ status: attempts === 1 ? 500 : 200, json: {} });
  });
  await page.locator('input[type="file"]').setInputFiles({
    name: "figure.png",
    mimeType: "image/png",
    buffer: Buffer.from("image"),
  });
  const footer = page.getByTestId("paper-files").locator("footer");
  await footer.getByRole("button", { name: "Retry file operation" }).waitFor();
  for (const name of ["Rename file", "Download file", "Delete file"]) {
    const box = await footer
      .getByRole(name === "Download file" ? "link" : "button", { name })
      .boundingBox();
    assert.ok(box && box.x >= 0 && box.x + box.width <= 390);
  }
  // A clean New file form does not take over the upload's failure.
  await page.getByRole("button", { name: "New file", exact: true }).click();
  assert.equal(await page.locator(".paper-path-form.paper-failed").count(), 0);
  await page.getByRole("button", { name: "Cancel file path" }).click();
  const retried = page.waitForResponse(
    (response) => response.url().endsWith("/files/img/content") && response.status() === 200,
  );
  await footer.getByRole("button", { name: "Retry file operation" }).click();
  await retried;
  assert.equal(attempts, 2);
});

test("Reconnect discards a rejected oversized update and its persisted copy only for that file", async (t) => {
  const { page, context, room } = await openPaper(t, {
    configure: ({ tree }) =>
      tree.files.push({ ...tree.files[0], file_id: "f2", path: "second.md" }),
  });
  const other = new Room();
  await context.routeWebSocket(/\/files\/f2\/live$/, (socket) => other.connect(socket));
  await page.getByRole("button", { name: "second.md", exact: true }).click();
  await saved(page, true);
  other.hold = true;
  await append(page, " unsaved");
  await saved(page, false);
  await page.getByRole("button", { name: "intro.md", exact: true }).click();
  await saved(page, true);
  // Reconnect the first file through a room that rejects oversized frames before applying them.
  await context.routeWebSocket(/\/files\/f1\/live$/, (socket) =>
    room.connect({
      send: (message) => socket.send(message),
      onClose: (callback) => socket.onClose(callback),
      onMessage: (callback) =>
        socket.onMessage((message) => {
          if (message.byteLength > 4 * 1024 * 1024) socket.close({ code: 4413 });
          else callback(message);
        }),
    }),
  );
  for (const client of [...room.clients]) client.close({ code: 4413 });
  await page.getByRole("button", { name: "Reconnect Paper" }).click();
  await saved(page, true);
  // A real paste is one editor transaction; typing 5 MiB through the driver times out.
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.evaluate(
    (size) => {
      const data = new DataTransfer();
      data.setData("text/plain", "x".repeat(size));
      document
        .querySelector(".cm-content")
        .dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true }));
    },
    5 * 1024 * 1024,
  );
  await page.getByRole("button", { name: "Reconnect Paper" }).waitFor();
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+Z");
  await page.getByRole("button", { name: "Reconnect Paper" }).click();
  await page.waitForFunction(editorMatches, "draft");
  await saved(page, true);
  await page.getByRole("button", { name: "second.md", exact: true }).click();
  await page.waitForFunction(editorMatches, "draft unsaved");
  await saved(page, false);
  await page.getByRole("button", { name: "intro.md", exact: true }).click();
  await page.reload();
  await page.waitForFunction(editorMatches, "draft");
  await saved(page, true);
  assert.equal(room.doc.getText("text").toString(), "draft");
});

test("Markdown preview waits for idle but updates during continuous typing", async (t) => {
  const { page } = await openPaper(t);
  await page.waitForFunction(
    () => document.querySelector(".paper-markdown")?.textContent === "draft",
  );
  await page.clock.install();
  await append(page, " idle");
  assert.equal(await page.locator(".paper-markdown").textContent(), "draft");
  await page.clock.fastForward(250);
  await page.waitForFunction(
    () => document.querySelector(".paper-markdown")?.textContent === "draft idle",
  );
  for (let i = 0; i < 5; i++) {
    await append(page, " word");
    await page.clock.fastForward(200);
  }
  await page.waitForFunction(
    () =>
      document.querySelector(".paper-markdown")?.textContent ===
      "draft idle word word word word word",
  );
});

test("compile diagnostics open a readable phone dialog with retry", async (t) => {
  const { page, context } = await openPaper(t, {
    width: 390,
    configure: ({ tree }) =>
      tree.files.push({ ...tree.files[0], file_id: "f2", path: "paper.tex", format: "latex" }),
  });
  await context.route("**/paper/compile", (route) =>
    route.fulfill({
      json: { status: "error", log_tail: "Undefined control sequence at line 4", pdf_url: null },
    }),
  );
  await page.getByRole("button", { name: "Show files", exact: true }).click();
  await page.getByRole("button", { name: "paper.tex", exact: true }).click();
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  const opener = page.getByRole("button", { name: "View compile log" });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Compile log" });
  assert.match(await dialog.locator("pre").textContent(), /Undefined control sequence/);
  assert.equal(await dialog.locator("textarea, input, [contenteditable=true]").count(), 0);
  assert.ok(await dialog.getByRole("button", { name: "Retry preview" }).isVisible());
  await page.keyboard.press("Escape");
  assert.ok(await opener.evaluate((element) => document.activeElement === element));
});

test("repeated same lines paint and locate the verified current range, then hide on local replacement", async (t) => {
  const room = new Room();
  room.doc.getText("text").delete(0, 5);
  room.doc.getText("text").insert(0, "same\nsame\nsame");
  const { page } = await openPaper(t, {
    room,
    initialText: "same\nsame\nsame",
    cards: [
      {
        ...card,
        lines: { start: 1, end: 1 },
        current: { start: 5, end: 9, revision: 1, text: "same" },
        preview: { removed: ["same"], added: ["new"], more: 0 },
      },
    ],
  });
  await page.locator(".paper-deletion").waitFor();
  assert.deepEqual(
    await page
      .locator(".cm-line")
      .evaluateAll((lines) => lines.map((line) => !!line.querySelector(".paper-deletion"))),
    [false, true, false],
  );
  await page.getByRole("button", { name: "Coach", exact: true }).last().click();
  await page.getByRole("button", { name: "Locate change", exact: true }).click();
  await page.waitForFunction(() =>
    document.querySelector(".paper-position")?.textContent.includes("Ln 2,"),
  );
  await page.keyboard.press("Shift+End");
  await page.keyboard.insertText("gone");
  await page.waitForFunction(() => !document.querySelector(".paper-inline-suggestion"));
  assert.equal(await page.locator(".paper-deletion").count(), 0);
});

const secondFile = ({ tree }) =>
  tree.files.push({ ...tree.files[0], file_id: "f2", path: "second.md" });

async function insideViewport(locator, width = 390) {
  const box = await locator.boundingBox();
  return box && box.x >= 0 && box.x + box.width <= width;
}

test("switching files keeps each document's undo history, selection and scroll", async (t) => {
  const long = Array.from({ length: 60 }, (_, index) => `line ${index + 1}`).join("\n");
  const room = new Room();
  room.doc.getText("text").delete(0, 5);
  room.doc.getText("text").insert(0, long);
  const { page, context } = await openPaper(t, {
    room,
    initialText: long,
    configure: secondFile,
  });
  await context.routeWebSocket(/\/files\/f2\/live$/, (socket) => new Room().connect(socket));
  await append(page, " edited");
  const scroller = page.locator(".cm-scroller");
  const position = await page.locator(".paper-position").textContent();
  const scroll = await scroller.evaluate((element) => element.scrollTop);
  assert.ok(scroll > 0);
  await page.getByRole("button", { name: "second.md", exact: true }).click();
  await page.waitForFunction(editorMatches, "draft");
  // The restored scroll renders only the lines near the bottom.
  const lastLine = (text) =>
    page.waitForFunction(
      (expected) => [...document.querySelectorAll(".cm-line")].at(-1)?.textContent === expected,
      text,
    );
  await page.getByRole("button", { name: "intro.md", exact: true }).click();
  await lastLine("line 60 edited");
  assert.equal(await page.locator(".paper-position").textContent(), position);
  // Scroll restores to the top visible line.
  assert.ok(Math.abs((await scroller.evaluate((element) => element.scrollTop)) - scroll) < 40);
  await page.locator(".cm-content").focus();
  await page.keyboard.press("ControlOrMeta+Z");
  await lastLine("line 60");
});

test("a hidden phone preview keeps its PDF and reading position and compiles only when shown", async (t) => {
  let currentTree;
  const { page, context } = await openPaper(t, {
    width: 390,
    configure: ({ tree }) => {
      currentTree = tree;
      tree.files.push({ ...tree.files[0], file_id: "f2", path: "paper.tex", format: "latex" });
    },
  });
  let compiles = 0;
  let pdfs = 0;
  await context.route("**/paper/compile", (route) => {
    compiles += 1;
    return route.fulfill({
      json: { status: "ok", pdf_url: "/api/projects/p/paper/compile/one/pdf" },
    });
  });
  await context.route("**/compile/one/pdf", (route) => {
    pdfs += 1;
    return route.fulfill({ contentType: "application/pdf", body: pdfBytes(20) });
  });
  await page.getByRole("button", { name: "Show files", exact: true }).click();
  await page.getByRole("button", { name: "paper.tex", exact: true }).click();
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  await page.locator('.paper-pdf-page[data-page="1"] canvas').waitFor();
  const viewer = page.locator(".paper-pdf");
  await viewer.evaluate((element) => {
    element.scrollTop = 2000;
  });
  await page.waitForFunction(
    () => !document.querySelector('.paper-pdf-page[data-page="1"] canvas'),
  );
  const scroll = await viewer.evaluate((element) => element.scrollTop);
  const write = page.getByRole("button", { name: "Write", exact: true });
  const preview = page.getByRole("button", { name: "Preview", exact: true }).first();
  await write.click();
  await preview.click();
  assert.equal(await viewer.evaluate((element) => element.scrollTop), scroll);
  await page.waitForTimeout(1000);
  assert.equal(compiles, 1);
  await write.click();
  currentTree.saved_revision += 1;
  const treeRead = page.waitForResponse((response) => response.url().endsWith("/paper/tree"));
  await append(page, " hidden");
  await treeRead;
  await page.waitForTimeout(1000);
  assert.equal(compiles, 1);
  const recompiled = page.waitForResponse((response) => response.url().endsWith("/paper/compile"));
  await preview.click();
  await recompiled;
  assert.equal(compiles, 2);
  assert.equal(pdfs, 1);
});

test("a deleted file keeps its unsent text read-only and refreshes the tree", async (t) => {
  const { page, context, fixture } = await openPaper(t, { configure: secondFile });
  const other = new Room();
  await context.routeWebSocket(/\/files\/f2\/live$/, (socket) => other.connect(socket));
  // Opening a saved file refreshes the tree once; let that read finish first.
  const opened = page.waitForResponse((response) => response.url().endsWith("/paper/tree"));
  await page.getByRole("button", { name: "second.md", exact: true }).click();
  await saved(page, true);
  await opened;
  other.hold = true;
  await append(page, " unsent");
  await saved(page, false);
  fixture.tree.files.pop();
  const treeRead = page.waitForResponse((response) => response.url().endsWith("/paper/tree"));
  for (const client of [...other.clients]) client.close({ code: 4403 });
  await treeRead;
  await page.getByRole("button", { name: "second.md", exact: true }).waitFor({ state: "detached" });
  assert.equal(await page.locator(".paper-path").textContent(), "second.md");
  await page.getByRole("button", { name: "Reconnect Paper" }).waitFor();
  await page.locator(".cm-content").click();
  await page.keyboard.insertText(" typed");
  assert.ok(await page.evaluate(editorMatches, "draft unsent"));
});

test("a refused card decision refreshes the cards and keeps their actions; only server failures retry", async (t) => {
  const { page, context } = await openPaper(t, { width: 390, cards: [{ ...card }] });
  let status = 409;
  await context.route("**/changes/g1/accept", (route) =>
    route.fulfill({ status, json: { detail: "Paper change is outdated" } }),
  );
  await page.getByRole("button", { name: "Coach", exact: true }).first().click();
  const change = page.locator('[data-change-id="c1"]');
  const reloaded = page.waitForResponse((response) => response.url().endsWith("/paper/changes"));
  await change.getByRole("button", { name: "Accept change" }).click();
  await reloaded;
  await page.locator('[data-change-id="c1"].paper-failure').waitFor();
  assert.equal(await change.getByRole("button", { name: "Retry change" }).count(), 0);
  status = 503;
  await change.getByRole("button", { name: "Accept change" }).click();
  await change.getByRole("button", { name: "Retry change" }).waitFor();
  for (const name of ["Retry change", "Accept change", "Reject change"])
    assert.ok(await insideViewport(change.getByRole("button", { name })));
});

test("a failed engine install shows as the compile error with its log and retry", async (t) => {
  const { page, context } = await openPaper(t, {
    width: 390,
    configure: ({ tree }) =>
      tree.files.push({ ...tree.files[0], file_id: "f2", path: "paper.tex", format: "latex" }),
  });
  await context.route("**/paper/engines", (route) =>
    route.fulfill({
      json: {
        latex: "unavailable",
        typst: "ready",
        errors: { latex: "engine download failed", typst: null },
      },
    }),
  );
  await page.getByRole("button", { name: "Show files", exact: true }).click();
  await page.getByRole("button", { name: "paper.tex", exact: true }).click();
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  await page.locator(".paper-compiled-preview.paper-failed").waitFor();
  await page.getByRole("button", { name: "View compile log" }).click();
  const dialog = page.getByRole("dialog", { name: "Compile log" });
  assert.equal(await dialog.locator("pre").textContent(), "engine download failed");
  const install = page.waitForRequest(
    (request) => request.url().endsWith("/engines/latex/install") && request.method() === "POST",
  );
  await dialog.getByRole("button", { name: "Retry preview" }).click();
  await install;
});

test("named versions can be deleted from history; automatic versions cannot", async (t) => {
  const { page, fixture } = await openPaper(t, {
    width: 390,
    configure: ({ versions }) =>
      versions.push({ ...versions[0], version_id: "auto", name: null, kind: "auto" }),
  });
  await page.getByRole("button", { name: "Paper history", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Paper history" });
  const remove = dialog.getByRole("button", { name: "Delete version" });
  await remove.waitFor();
  assert.equal(await remove.count(), 1);
  assert.ok(await insideViewport(remove));
  await remove.click();
  await remove.waitFor({ state: "detached" });
  assert.deepEqual(
    fixture.versions.map((version) => version.version_id),
    ["auto"],
  );
});
