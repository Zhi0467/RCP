import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";

import {
  PaperTestRoom as Room,
  appendText,
  createPaperTestServer,
  editorMatches,
  openPaper,
  waitForSaved,
} from "./paperTestServer.mjs";

let server;
let browser;
before(async () => {
  server = await createPaperTestServer();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

test("a keystroke shown after a backend restart is detected survives into the next document", async (t) => {
  let instance = "instance";
  const { page, room, errors } = await openPaper(t, {
    browser,
    origin: server.origin,
    configure(fixture) {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) =>
        path === "/api/health"
          ? { json: { instance_id: instance, data_dir_id: "data", version: "test" } }
          : respond(path, ...rest);
    },
  });
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.insertText(" before");
  await page.waitForFunction(
    () => document.querySelector('[data-testid="paper-saved"]')?.dataset.saved === "true",
  );
  instance = "restarted";
  // The editor is still mounted when the new identity arrives; type before React unmounts it.
  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("rcp:backend-identity", {
        detail: {
          ok: false,
          health: { instance_id: "restarted", data_dir_id: "data", version: "test" },
          message: null,
          reason: "test",
        },
      }),
    );
    document.execCommand("insertText", false, " after");
  });
  await page.waitForFunction(editorMatches, "draft before after", { timeout: 10_000 });
  await page.waitForFunction(
    () => document.querySelector('[data-testid="paper-saved"]')?.dataset.saved === "true",
  );
  assert.equal(room.doc.getText("text").toString(), "draft before after");
  // The rebuilt editor keeps the focus and the cursor, so typing simply continues.
  await page.keyboard.insertText(" more");
  await page.waitForFunction(editorMatches, "draft before after more");
  assert.deepEqual(errors, []);
});

/** Answers health with the current instance, so a test can restart the backend. */
function restartable() {
  const backend = { instance: "instance" };
  return {
    backend,
    configure(fixture) {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) =>
        path === "/api/health"
          ? { json: { instance_id: backend.instance, data_dir_id: "data", version: "test" } }
          : respond(path, ...rest);
    },
    restart(page) {
      backend.instance = `${backend.instance}+`;
      return page.evaluate((instance_id) => {
        window.dispatchEvent(
          new CustomEvent("rcp:backend-identity", {
            detail: {
              ok: true,
              health: { instance_id, data_dir_id: "data", version: "test" },
              message: null,
              reason: "test",
            },
          }),
        );
      }, backend.instance);
    },
  };
}

test("typing through a backend restart keeps the same editor and every keystroke", async (t) => {
  const backend = restartable();
  const { page, context, room, errors } = await openPaper(t, {
    browser,
    origin: server.origin,
    configure: backend.configure,
  });
  await appendText(page, " ");
  await waitForSaved(page, true);
  await page.locator(".cm-editor").evaluate((editor) => {
    editor.dataset.kept = "yes";
  });
  // The identity check after a restart takes a while; typing continues through it.
  await context.route("**/api/identity", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 800));
    await route.fallback();
  });
  await backend.restart(page);
  await page.keyboard.type("abcdefghij", { delay: 100 });
  await page.waitForFunction(editorMatches, "draft abcdefghij");
  await waitForSaved(page, true);
  assert.equal(room.doc.getText("text").toString(), "draft abcdefghij");
  assert.equal(await page.locator('.cm-editor[data-kept="yes"]').count(), 1);
  assert.deepEqual(errors, []);
});

test("a backend restart sends a hidden file's unsent text without reopening it", async (t) => {
  const backend = restartable();
  const { page, context } = await openPaper(t, {
    browser,
    origin: server.origin,
    configure(fixture) {
      backend.configure(fixture);
      fixture.tree.files.push({ ...fixture.tree.files[0], file_id: "f2", path: "second.md" });
    },
  });
  const other = new Room();
  await context.routeWebSocket(/\/files\/f2\/live$/, (socket) => other.connect(socket));
  await page.getByRole("button", { name: "second.md", exact: true }).click();
  await waitForSaved(page, true);
  other.drop = true;
  await appendText(page, " unsent");
  await waitForSaved(page, false);
  await page.getByRole("button", { name: "intro.md", exact: true }).click();
  await page.waitForFunction(editorMatches, "draft");
  other.drop = false;
  await backend.restart(page);
  await other.waitForText("draft unsent");
});
