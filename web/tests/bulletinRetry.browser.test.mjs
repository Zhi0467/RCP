import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

// The server answers an accepted post with the stored record.
const accepted = (body, extra = {}) => ({
  post: {
    post_id: `post-${body.key}`,
    author: { kind: "member", user_id: "member", display_name: "Member" },
    text: body.text,
    refs: [],
    reply_to: body.reply_to ?? null,
    created_at: new Date().toISOString(),
    mentions: [],
    reply_count: 0,
    ...extra,
  },
});

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
        return route.fulfill({ json: accepted(posted[1]) });
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

test("an agent from another channel is mentioned from the shown channel", async () => {
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
    const held = Promise.withResolvers();
    await page.route("**/api/**", async (route) => {
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
        const body = JSON.parse(request.postData());
        posted.push({ branch: url.searchParams.get("branch_id"), ...body });
        return route.fulfill({ json: accepted(body) });
      }
      if (url.pathname.endsWith("/bulletin")) {
        // After a post, the confirming read waits, so only the accepted reply can show it.
        if (posted.length) await held.promise;
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
      }
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

    // A pinned phone bar over the room below the composer sends the list above it.
    await page.evaluate(() => {
      const form = document.querySelector(".bulletin-composer").getBoundingClientRect();
      const bar = document.createElement("div");
      bar.className = "phone-tab-bar";
      bar.style.cssText = `position:fixed;left:0;right:0;bottom:0;top:${form.bottom + 4}px`;
      document.body.append(bar);
    });
    await composer.fill("ping @runn");
    const options = page.getByRole("option");
    assert.equal(await options.count(), 1);
    const [list, form] = await page.evaluate(() =>
      [".bulletin-suggest", ".bulletin-composer"].map((selector) => {
        const box = document.querySelector(selector).getBoundingClientRect();
        return { top: box.top, bottom: box.bottom };
      }),
    );
    assert.ok(list.bottom <= form.top, JSON.stringify({ list, form }));
    await composer.press("Enter");
    assert.equal(await composer.inputValue(), "ping @running-the ");
    assert.equal(await page.evaluate(() => window.location.hash), "#start");

    await composer.press("Enter");
    await page.waitForFunction(() => !document.querySelector(".bulletin-composer textarea")?.value);
    assert.equal(posted.length, 1);
    // The post stays in main and still names the branch agent, which reads mentions everywhere.
    assert.equal(posted[0].branch, null);
    assert.equal(posted[0].text, "ping <@agent:chat:b>");
    await page.locator(".bulletin-feed .bulletin-message").waitFor();
    held.resolve();
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("an agent's name opens its chat and another channel's node opens on that branch", async () => {
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
    const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const worker = {
      id: "chat:b",
      chat_id: "b",
      target_key: "branch:x",
      name: "worker",
      kind: "human_chat_work",
      seat_node_id: null,
      started_at: new Date().toISOString(),
      state: "working",
      last_post_at: null,
    };
    const { post } = accepted(
      { key: "note", text: "Done (#exp/run-a): see it." },
      { author: { kind: "agent", actor_id: "chat:b", actor_label: "worker" }, refs: ["exp/run-a"] },
    );
    await page.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/bulletin/channels"))
        return route.fulfill({
          json: [
            { target_key: "main", branch_id: null, name: "main", last_post_at: null, unread: 0 },
            { target_key: "branch:x", branch_id: "x", name: "x", last_post_at: null, unread: 0 },
          ],
        });
      if (url.pathname.endsWith("/bulletin"))
        return route.fulfill({
          json: {
            roster: [worker],
            can_post: true,
            posts: url.searchParams.get("branch_id") === "x" ? [post] : [],
            newest_post_id: null,
            members: [],
            next_cursor: null,
            total: 1,
          },
        });
      return route.fulfill({ json: {} });
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/bulletinRetry.html#start`);
    // The workspace shows main; the post lives in channel x.
    await page.locator(".bulletin-channel").filter({ hasText: /^x$/ }).click();
    const node = page.locator(".bulletin-node");
    await node.waitFor();
    await node.click();
    assert.equal(
      await page.evaluate(() => window.location.hash),
      "#/projects/project/targets/x/node/exp%2Frun-a",
    );
    await page.locator("button.bulletin-author").click();
    const hash = await page.evaluate(() => window.location.hash);
    const params = new URLSearchParams(hash.split("?")[1]);
    assert.deepEqual(
      [params.get("view"), params.get("chat"), params.get("branch_id")],
      ["chats", "b", "x"],
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("artifact picker inserts a token and the post chip opens the shared viewer at phone and desktop widths", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    for (const width of [390, 1200]) {
      const page = await browser.newPage({ viewport: { width, height: 800 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      const requests = [];
      const posts = [];
      const artifact = {
        artifact_id: "art-1",
        source_name: "Results from another branch.html",
        media_type: "text/html",
      };
      await page.route("**/api/**", (route) => {
        const request = route.request();
        const url = new URL(request.url());
        requests.push(url.pathname);
        if (url.pathname.endsWith("/bulletin/channels")) return route.fulfill({ json: [] });
        if (url.pathname.endsWith("/bulletin") && request.method() === "POST") {
          const body = JSON.parse(request.postData());
          posts.push(accepted(body, { artifacts: [artifact] }).post);
          return route.fulfill({ json: { post: posts[0] } });
        }
        if (url.pathname.endsWith("/bulletin"))
          return route.fulfill({
            json: {
              roster: [],
              members: [],
              can_post: true,
              posts,
              newest_post_id: posts[0]?.post_id ?? null,
              next_cursor: null,
              total: posts.length,
            },
          });
        if (url.pathname.endsWith("/artifacts"))
          return route.fulfill({
            json: [
              {
                artifact_id: artifact.artifact_id,
                name: artifact.source_name,
                kind: "artifact",
                available: true,
              },
              { artifact_id: "expired", name: "Expired", kind: "artifact", available: false },
            ],
          });
        if (url.pathname.endsWith("/art-1/state"))
          return route.fulfill({
            json: {
              ...artifact,
              name: artifact.source_name,
              view: "html",
              current_version: "v1",
              version_number: 1,
              pinned_branch_ids: [],
              supplier: "turn",
              live: null,
              editing_operation_id: null,
              viewer_url: "/api/projects/project/artifacts/art-1/viewer",
              thread_href: null,
            },
          });
        if (url.pathname.endsWith("/art-1/viewer"))
          return route.fulfill({
            contentType: "text/html",
            body: "<p>Shared artifact bytes</p>",
          });
        if (url.pathname.endsWith("/refs")) return route.fulfill({ json: [] });
        return route.fulfill({ json: {} });
      });
      await page.goto(
        `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/bulletinRetry.html`,
      );
      const composer = page.getByRole("textbox", { name: "Bulletin post", exact: true });
      await composer.fill("See");
      await page.getByRole("button", { name: "Attach artifact", exact: true }).click();
      const picker = page.getByRole("region", { name: "From RCP" });
      await picker.getByRole("button", { name: /^Artifacts/ }).click();
      assert.equal(await picker.getByRole("button", { name: "Expired", exact: true }).count(), 0);
      assert.equal(await picker.getByRole("button", { name: /^Nodes/ }).count(), 0);
      const bounds = await picker.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width);
      await picker.getByRole("button", { name: artifact.source_name, exact: true }).click();
      assert.equal(await composer.inputValue(), "See <artifact:art-1> ");
      await page.getByRole("button", { name: "Post", exact: true }).click();
      const chip = page.locator(".bulletin-artifact");
      await chip.waitFor();
      assert.equal(await chip.textContent(), artifact.source_name);
      assert.equal(await chip.locator("svg").count(), 1);
      assert.equal(posts[0].text, "See <artifact:art-1>");
      assert.equal(
        requests.some((path) => path.endsWith("/paper")),
        false,
      );
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
      );
      await chip.click();
      await page
        .frameLocator(".artifact-viewer iframe")
        .getByText("Shared artifact bytes")
        .waitFor();
      assert.ok(requests.includes("/api/projects/project/artifacts/art-1/state"));
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser?.close();
    await server.close();
  }
});
