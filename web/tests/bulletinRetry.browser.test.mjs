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

test("retrying a failed edit keeps a revision made after the failure", async () => {
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
    const saved = [];
    let refreshedAfterRetry;
    const retrySettled = new Promise((resolve) => (refreshedAfterRetry = resolve));
    const post = {
      post_id: "p1",
      author: { kind: "member", user_id: "member", display_name: "Member" },
      text: "Original",
      refs: [],
      reply_to: null,
      created_at: new Date().toISOString(),
      reply_count: 0,
    };
    await page.route("**/api/**", (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith("/bulletin/channels")) return route.fulfill({ json: [] });
      if (request.method() === "PATCH") {
        saved.push(JSON.parse(request.postData() ?? "{}"));
        if (saved.length === 1) return route.fulfill({ status: 409, json: { detail: "busy" } });
        return route.fulfill({ json: { post } });
      }
      if (url.pathname.endsWith("/bulletin")) {
        // A successful save reloads the page after updating the edit box.
        if (saved.length === 2) refreshedAfterRetry();
        return route.fulfill({
          json: {
            roster: [],
            can_post: true,
            posts: [post],
            newest_post_id: "p1",
            members: [],
            next_cursor: null,
            total: 1,
          },
        });
      }
      return route.fulfill({ json: {} });
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/bulletinRetry.html`);
    await page.getByRole("button", { name: "Edit post", exact: true }).click();
    const box = page.getByRole("textbox", { name: "Edit post", exact: true });
    // Opening an edit focuses the box with the caret after the existing text.
    assert.deepEqual(
      await box.evaluate((element) => [
        document.activeElement === element,
        element.selectionStart,
        element.selectionEnd,
      ]),
      [true, post.text.length, post.text.length],
    );
    await box.fill("First edit");
    await box.press("Enter");
    const retry = page.getByRole("button", { name: "Retry", exact: true });
    await retry.waitFor();

    await box.fill("Revised edit");
    await retry.click();
    await retrySettled;
    await page.locator('textarea[aria-label="Edit post"]:not([disabled])').waitFor();
    // The retry saves the original request and leaves the newer revision open.
    assert.equal(saved.length, 2);
    assert.equal(saved[1].text, "First edit");
    assert.equal(saved[1].key, saved[0].key);
    assert.equal(await box.inputValue(), "Revised edit");
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("an agent from another channel moves only the Bulletin there, keeping the draft", async () => {
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
    const agent = (id, target_key, name) => ({
      id,
      chat_id: id.split(":")[1],
      target_key,
      name,
      kind: "human_chat_work",
      seat_node_id: null,
      started_at: new Date().toISOString(),
      state: "working",
      last_post_at: null,
    });
    await page.route("**/api/**", (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith("/bulletin/channels"))
        return route.fulfill({
          json: [
            { target_key: "main", branch_id: null, name: "main", last_post_at: null, unread: 0 },
            { target_key: "branch:x", branch_id: "x", name: "x", last_post_at: null, unread: 0 },
          ],
        });
      if (request.method() === "POST" && url.pathname.endsWith("/bulletin")) {
        posted.push({
          branch: url.searchParams.get("branch_id"),
          ...JSON.parse(request.postData()),
        });
        return route.fulfill({ json: {} });
      }
      if (url.pathname.endsWith("/bulletin"))
        return route.fulfill({
          json: {
            roster: [agent("chat:a", "main", "helper"), agent("chat:b", "branch:x", "running-the")],
            can_post: true,
            posts: [],
            newest_post_id: null,
            members: [],
            next_cursor: null,
            total: 0,
          },
        });
      return route.fulfill({ json: {} });
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/bulletinRetry.html#start`);
    const composer = page.getByRole("textbox", { name: "Bulletin post", exact: true });
    await composer.waitFor();
    // The roster count covers only the shown channel's agents.
    assert.equal(await page.locator(".bulletin-members").textContent(), "1");

    // Under the Aqua theme the field's frame shows focus, not a ring around the textarea.
    await page.evaluate(() => (document.documentElement.dataset.theme = "aqua"));
    await composer.click();
    assert.equal(
      await composer.evaluate((element) => getComputedStyle(element).outlineStyle),
      "none",
    );

    await composer.fill("ping @runn");
    const options = page.getByRole("option");
    assert.equal(await options.count(), 1);
    await composer.press("Enter");
    await page.locator(".bulletin-members").filter({ hasText: /^1$/ }).waitFor();
    assert.equal(await composer.inputValue(), "ping @running-the ");
    assert.equal(await page.evaluate(() => window.location.hash), "#start");

    await composer.press("Enter");
    await page.waitForFunction(() => !document.querySelector(".bulletin-composer textarea")?.value);
    assert.equal(posted.length, 1);
    assert.equal(posted[0].branch, "x");
    assert.equal(posted[0].text, "ping <@agent:chat:b>");
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
