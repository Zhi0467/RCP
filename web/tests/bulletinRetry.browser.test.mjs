import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("retrying a failed post keeps a draft revised after the failure", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const address = server.httpServer?.address();
    assert.ok(address && typeof address === "object");
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const posted = [];
    let refreshedAfterRetry;
    const retrySettled = new Promise((resolve) => (refreshedAfterRetry = resolve));
    await page.route("**/api/**", (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith("/bulletin/channels")) return route.fulfill({ json: [] });
      if (request.method() === "POST" && url.pathname.endsWith("/bulletin")) {
        posted.push(JSON.parse(request.postData() ?? "{}"));
        if (posted.length === 1) return route.fulfill({ status: 409, json: { detail: "busy" } });
        return route.fulfill({ json: {} });
      }
      if (url.pathname.endsWith("/bulletin")) {
        // A successful post reloads the page after updating the composer.
        if (posted.length === 2) refreshedAfterRetry();
        return route.fulfill({
          json: {
            roster: [],
            roster_omitted: 0,
            can_post: true,
            posts: [],
            newest_post_id: null,
            members: [],
            next_cursor: null,
            total: 0,
          },
        });
      }
      return route.fulfill({ json: {} });
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/bulletinRetry.html`);
    const composer = page.getByRole("textbox", { name: "Bulletin post", exact: true });
    await composer.waitFor();
    await composer.fill("First draft");
    await composer.press("Enter");
    const retry = page.getByRole("button", { name: "Retry", exact: true });
    await retry.waitFor();
    assert.equal(await composer.inputValue(), "First draft");

    await composer.fill("Revised draft");
    await retry.click();
    await retrySettled;
    await page.locator('textarea[aria-label="Bulletin post"]:not([disabled])').waitFor();
    // The retry resends the original keyed request and leaves the newer text unsent.
    assert.equal(posted.length, 2);
    assert.equal(posted[1].text, "First draft");
    assert.equal(posted[1].key, posted[0].key);
    assert.equal(await composer.inputValue(), "Revised draft");
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
