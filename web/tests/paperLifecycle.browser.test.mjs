import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import { chromium } from "playwright";

import {
  PaperTestRoom as Room,
  paperTestCard as card,
  appendText as append,
  completingSocket,
  createPaperTestServer,
  editorMatches,
  openPaper as openPaperPage,
  openPaperTab,
  waitForSaved as saved,
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
const secondFile = ({ tree }) =>
  tree.files.push({ ...tree.files[0], file_id: "f2", path: "second.md" });

/** Opens second.md on its own room and leaves it with unsent text. */
async function strandSecondFile(opened) {
  const other = new Room();
  await opened.context.routeWebSocket(/\/files\/f2\/live$/, (socket) => other.connect(socket));
  const read = opened.page.waitForResponse((response) => response.url().endsWith("/paper/tree"));
  await opened.page.getByRole("button", { name: "second.md", exact: true }).click();
  await saved(opened.page, true);
  await read;
  other.drop = true;
  await append(opened.page, " unsent");
  await saved(opened.page, false);
  return other;
}

function deleteSecondFile(fixture, other) {
  fixture.tree.files = fixture.tree.files.filter((file) => file.file_id !== "f2");
  fixture.tree.revision += 1;
  for (const client of [...other.clients]) client.close({ code: 4403 });
}

test("Reconnect after a refused update in one tab keeps the other tab's local storage", async (t) => {
  const room = new Room();
  room.refuseAbove = 4 * 1024 * 1024;
  const first = await openPaper(t, {
    room,
    configure: ({ tree }) => {
      tree.max_doc_bytes = 8 * 1024 * 1024;
    },
  });
  const second = await openPaperTab(t, first, origin);
  await saved(second.page, true);
  await first.page.locator(".cm-content").click();
  await first.page.keyboard.press("ControlOrMeta+End");
  await first.page.evaluate(
    (size) => {
      const data = new DataTransfer();
      data.setData("text/plain", "x".repeat(size));
      document
        .querySelector(".cm-content")
        .dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true }));
    },
    5 * 1024 * 1024,
  );
  await first.page.getByRole("button", { name: "Reconnect Paper" }).click();
  await first.page.waitForFunction(editorMatches, "draft");
  await saved(first.page, true);
  room.drop = true;
  await append(second.page, " tab");
  await saved(second.page, false);
  // The text must be in the second tab's store, not only in its memory.
  second.page.once("dialog", (dialog) => dialog.accept());
  await second.page.reload();
  await second.page.waitForFunction(editorMatches, "draft tab", { timeout: 5_000 });
  assert.deepEqual(second.errors, []);
});

test("a peer deleting a hidden file with unsent text leaves that text one action away", async (t) => {
  const opened = await openPaper(t, { configure: secondFile });
  const { page, fixture } = opened;
  const other = await strandSecondFile(opened);
  await page.getByRole("button", { name: "intro.md", exact: true }).click();
  await saved(page, true);
  deleteSecondFile(fixture, other);
  // Typing in the open file is what next reads the tree here.
  const read = page.waitForResponse((response) => response.url().endsWith("/paper/tree"));
  await append(page, " x");
  await read;
  await page.getByRole("button", { name: "Open unsent file" }).click({ timeout: 5_000 });
  await page.waitForFunction(editorMatches, "draft unsent");
  assert.equal(await page.locator(".paper-path").textContent(), "second.md");
  await page.getByRole("button", { name: "Recreate file" }).click();
  await saved(page, true);
  const restored = fixture.tree.files.find((file) => file.path === "second.md");
  assert.equal(fixture.rooms.get(restored.file_id).doc.getText("text").toString(), "draft unsent");
});

test("a deleted file's unsent text is still one action away after a reload", async (t) => {
  const opened = await openPaper(t, { configure: secondFile });
  const { page, fixture, context } = opened;
  const other = await strandSecondFile(opened);
  await page.getByRole("button", { name: "intro.md", exact: true }).click();
  await saved(page, true);
  deleteSecondFile(fixture, other);
  // The server now refuses the deleted file's room, as it does after a reload.
  await context.routeWebSocket(/\/files\/f2\/live$/, (socket) => socket.close({ code: 4403 }));
  await page.reload();
  await page.getByRole("button", { name: "Open unsent file" }).click({ timeout: 5_000 });
  await page.waitForFunction(editorMatches, "draft unsent");
  await page.getByRole("button", { name: "Recreate file" }).click();
  await saved(page, true);
  // Recreated text lives in the new file; a later reload offers nothing to recover.
  const read = page.waitForResponse((response) => response.url().endsWith("/paper/tree"));
  await page.reload();
  await read;
  await page.waitForFunction(editorMatches, "draft unsent");
  assert.equal(await page.getByRole("button", { name: "Open unsent file" }).count(), 0);
  assert.deepEqual(opened.errors, []);
});

test("a connection refused before any server message keeps unsent text on Reconnect", async (t) => {
  const { page, room, errors } = await openPaper(t);
  await saved(page, true);
  room.drop = true;
  await append(page, " unsent");
  await saved(page, false);
  room.refuseConnect = 4413;
  for (const client of [...room.clients]) client.close({ code: 1011 });
  const reconnect = page.getByRole("button", { name: "Reconnect Paper" });
  await reconnect.waitFor();
  room.refuseConnect = null;
  room.drop = false;
  await reconnect.click();
  await saved(page, true);
  assert.ok(await page.evaluate(editorMatches, "draft unsent"));
  assert.equal(room.doc.getText("text").toString(), "draft unsent");
  assert.deepEqual(errors, []);
});

test("losing project access keeps unsent text visible with a download", async (t) => {
  let lost = false;
  const { page, room } = await openPaper(t, {
    configure(fixture) {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) =>
        lost && path.startsWith("/api/projects/")
          ? { status: 403, json: { detail: "Not a project member" } }
          : respond(path, ...rest);
    },
  });
  await saved(page, true);
  room.drop = true;
  await append(page, " unsent");
  await saved(page, false);
  await page.locator(".cm-editor").evaluate((editor) => {
    editor.dataset.kept = "yes";
  });
  lost = true;
  room.refuseConnect = 4403;
  for (const client of [...room.clients]) client.close({ code: 4403 });
  await page.evaluate(() => window.dispatchEvent(new Event("rcp:read-access-lost")));
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download unsent text" }).click({ timeout: 5_000 });
  assert.equal(await readFile(await (await download).path(), "utf8"), "draft unsent");
  assert.equal(await page.locator('.cm-editor[data-kept="yes"]').count(), 1);
  assert.equal(await page.locator(".cm-content").getAttribute("aria-readonly"), "true");
});

test("Locate to a file missing from the current tree opens a listed file", async (t) => {
  const { page } = await openPaper(t, { cards: [{ ...card, file_id: "f9" }] });
  await page.getByRole("button", { name: "Coach", exact: true }).last().click();
  await page.getByRole("button", { name: "Locate change", exact: true }).click();
  await page.waitForTimeout(300);
  assert.equal(await page.locator(".paper-path").textContent(), "intro.md");
  assert.ok(await page.evaluate(editorMatches, "draft"));
});

test("a created file stays selected while a peer's older save is read first", async (t) => {
  const { page } = await openPaper(t, {
    configure(fixture) {
      const respond = fixture.respond;
      let stale = null;
      fixture.respond = (path, method, body) => {
        if (method === "POST" && path.endsWith("/files")) {
          // A peer's save commits first; the next tree read predates the new file.
          fixture.tree.revision += 1;
          stale = structuredClone(fixture.tree);
        } else if (path.endsWith("/tree") && stale) {
          const json = stale;
          stale = null;
          return { json };
        }
        return respond(path, method, body);
      };
    },
  });
  await page.evaluate(() => {
    window.paperPaths = [];
    new MutationObserver(() =>
      window.paperPaths.push(document.querySelector(".paper-path")?.textContent),
    ).observe(document.body, { subtree: true, childList: true, characterData: true });
  });
  await page.getByRole("button", { name: "File tree actions" }).click();
  await page.getByRole("menuitem", { name: "New file" }).click();
  const path = page.getByRole("textbox", { name: "File path" });
  await path.fill("new.md");
  await path.press("Enter");
  await page.waitForFunction(() => document.querySelector(".paper-path")?.textContent === "new.md");
  await page.waitForFunction(editorMatches, "");
  // Once the editor leaves intro.md for the new file, it never returns to it.
  const paths = await page.evaluate(() => window.paperPaths);
  assert.ok(!paths.slice(paths.findIndex((path) => path !== "intro.md")).includes("intro.md"));
});

test("one Backspace sends one update", async (t) => {
  const { page, room } = await openPaper(t);
  await saved(page, true);
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+End");
  const count = room.updatesReceived;
  await page.keyboard.press("Backspace");
  await room.waitForUpdates(count + 1);
  await saved(page, true);
  await page.waitForTimeout(200);
  assert.equal(room.updatesReceived, count + 1);
  assert.equal(room.doc.getText("text").toString(), "draf");
});

test("a disposed document is not kept alive by editor memory", async (t) => {
  const { page, context } = await openPaper(t, { configure: secondFile });
  await context.routeWebSocket(/\/files\/f2\/live$/, (socket) =>
    new Room().connect(completingSocket(socket)),
  );
  await page.getByRole("button", { name: "second.md", exact: true }).click();
  await saved(page, true);
  await append(page, " visited");
  await saved(page, true);
  await page.getByRole("button", { name: "intro.md", exact: true }).click();
  await page.waitForFunction(editorMatches, "draft");
  await page.evaluate(async () => {
    const { acquirePaperDocument } = await import("/src/paper/paperDocuments.ts");
    const owner = acquirePaperDocument(
      { backend: `${location.origin}:space`, member: "member", project: "p", file: "f2" },
      "/api/projects/p",
      "Member",
    );
    window.disposedDoc = new WeakRef(owner.doc);
    owner.destroy();
  });
  const session = await context.newCDPSession(page);
  let collected = false;
  // Closing the socket and local storage settles asynchronously; collect until it has.
  for (let attempt = 0; attempt < 10 && !collected; attempt++) {
    await page.waitForTimeout(200);
    await session.send("HeapProfiler.collectGarbage");
    collected = await page.evaluate(() => window.disposedDoc.deref() === undefined);
  }
  assert.ok(collected);
});

test("a deleted file goes straight to Recreate, which keeps focus and the cursor", async (t) => {
  const opened = await openPaper(t, { configure: secondFile });
  const { page, fixture } = opened;
  await page.evaluate(() => {
    window.reconnectShown = false;
    new MutationObserver(() => {
      if (document.querySelector('[aria-label="Reconnect Paper"]')) window.reconnectShown = true;
    }).observe(document.body, { subtree: true, childList: true, attributes: true });
  });
  const other = await strandSecondFile(opened);
  await page.keyboard.press("ArrowLeft");
  const position = await page.locator(".paper-position").textContent();
  deleteSecondFile(fixture, other);
  await page.getByRole("button", { name: "Recreate file" }).click();
  await saved(page, true);
  await page.waitForFunction(editorMatches, "draft unsent");
  await page.waitForFunction(() => document.activeElement?.classList.contains("cm-content"));
  await page.waitForFunction(
    (expected) => document.querySelector(".paper-position")?.textContent === expected,
    position,
  );
  assert.equal(await page.evaluate(() => window.reconnectShown), false);
});

test("an idle member's saved status holds while a peer types", async (t) => {
  const room = new Room();
  const writer = await openPaper(t, { room });
  const reader = await openPaper(t, { room });
  await saved(reader.page, true);
  await reader.page.evaluate(() => {
    window.statusChanges = 0;
    new MutationObserver(() => window.statusChanges++).observe(
      document.querySelector('[data-testid="paper-saved"]'),
      { attributes: true },
    );
  });
  await writer.page.locator(".cm-content").click();
  await writer.page.keyboard.press("ControlOrMeta+End");
  await writer.page.keyboard.type(" typed by a peer", { delay: 20 });
  await reader.page.waitForFunction(editorMatches, "draft typed by a peer");
  await reader.page.waitForTimeout(300);
  assert.equal(await reader.page.evaluate(() => window.statusChanges), 0);
});
