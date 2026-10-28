import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";

import { createPaperTestServer, editorMatches, openPaper } from "./paperTestServer.mjs";

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
  assert.deepEqual(errors, []);
});
