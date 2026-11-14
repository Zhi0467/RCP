import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("a pointer selection on a reply offers Comment, keeps the selection copyable, and opens the shared composer only on request", async () => {
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
    const page = await browser.newPage({ viewport: { width: 844, height: 520 } });
    await page.route("**/api/projects/*/chats/*/questions", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/projects/*/comment-threads?*", (route) =>
      route.fulfill({ json: { threads: [] } }),
    );
    await page.route("**/api/projects/*/members", (route) => route.fulfill({ json: [] }));
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/chatAnnotationViewport.html`);
    const answer = page.locator(".chat-annotatable-answer");
    await answer.waitFor({ state: "visible" });
    const box = await answer.boundingBox();
    assert.ok(box);

    // Drag from inside the answer text and release well below the answer element,
    // as a reader does when sweeping a selection downward.
    await page.mouse.move(box.x + 12, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 160, box.y + box.height / 2, { steps: 4 });
    await page.mouse.move(box.x + 160, box.y + box.height + 60, { steps: 4 });
    await page.mouse.up();

    // Releasing offers Comment without opening the composer or moving focus, so
    // the platform's Copy still acts on the reader's selection.
    const offer = page.locator(".chat-selection-comment");
    await offer.waitFor({ state: "visible", timeout: 2000 });
    const composer = page.getByRole("dialog", { name: "Comment" });
    assert.equal(await composer.count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement === document.body), true);
    const offerBox = await offer.boundingBox();
    const selectionTop = await page.evaluate(
      () => window.getSelection().getRangeAt(0).getClientRects()[0].top,
    );
    assert.ok(
      offerBox && offerBox.y + offerBox.height <= selectionTop,
      "Comment sits on top of the selection without covering it",
    );

    await offer.click();
    await composer.waitFor({ state: "visible", timeout: 2000 });
    assert.equal(await offer.count(), 0, "The offer gives way to the composer");
    // The sweep overshot past the answer; the quote stops at the answer's end.
    assert.equal(
      (await composer.locator("blockquote").innerText()).trim(),
      "he reported improvement needs a stronger comparison and a variance estimate.",
    );

    // Dismissing the composer does not bring the offer straight back.
    await page.getByRole("textbox", { name: "Comment", exact: true }).waitFor();
    await page.waitForFunction(
      () => document.activeElement?.closest(".rcp-comment-composer") != null,
      null,
      { timeout: 2000 },
    );
    await page.keyboard.press("Escape");
    await composer.waitFor({ state: "detached" });
    assert.equal(await offer.count(), 0);

    // Collapsing the selection withdraws the offer.
    await page.mouse.move(box.x + 12, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.up();
    await page.mouse.move(box.x + 12, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 120, box.y + box.height / 2, { steps: 3 });
    await page.mouse.up();
    await offer.waitFor();
    await page.evaluate(() => window.getSelection()?.removeAllRanges());
    await offer.waitFor({ state: "detached" });

    // A selection that survives a resize keeps its offer beside it, on screen.
    await page.evaluate(() => {
      const text = document.querySelector(".chat-annotatable-answer p").firstChild;
      const range = document.createRange();
      range.setStart(text, text.length - 20);
      range.setEnd(text, text.length - 1);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
    });
    await offer.waitFor();
    await page.setViewportSize({ width: 360, height: 520 });
    await page.waitForFunction(() => {
      const button = document.querySelector(".chat-selection-comment");
      const first = window.getSelection().getRangeAt(0).getClientRects()[0];
      if (!button || !first) return false;
      const box = button.getBoundingClientRect();
      return box.right <= window.innerWidth && Math.abs(box.bottom + 6 - first.top) < 2;
    });
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("a reply thread pins at its quote, opens from the pin and the header count, and sends through the composer", async () => {
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
    const page = await browser.newPage({ viewport: { width: 390, height: 760 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const thread = {
      thread_id: "c1",
      project_id: "project",
      target_kind: "reply",
      target_id: "answer-task",
      graph_target: "main",
      drawn_version: null,
      anchor: {
        kind: "text",
        text: "stronger comparison",
        surrounding_text: "needs a stronger comparison and",
        comment: "",
      },
      state: "open",
      created_by: "u1",
      created_at: "2026-11-13T10:00:00Z",
      closed_by: null,
      closed_at: null,
      sent_operation_id: null,
      can_send: true,
      posts: [
        {
          post_id: "p1",
          thread_id: "c1",
          author: { user_id: "u1", display_name: "Ada" },
          text: "Which baseline?",
          created_at: "2026-11-13T10:00:00Z",
        },
      ],
    };
    await page.route("**/api/projects/*/chats/*/questions", (route) => route.fulfill({ json: [] }));
    const listed = [];
    await page.route("**/api/projects/*/comment-threads?*", (route) => {
      listed.push(new URL(route.request().url()).search);
      return route.fulfill({ json: { threads: [thread] } });
    });
    await page.route("**/api/projects/*/members", (route) => route.fulfill({ json: [] }));
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/chatAnnotationViewport.html`);
    const pin = page.getByRole("button", { name: "Comment 1" });
    await pin.waitFor();
    const quoteEnd = await page.evaluate(() => {
      const answer = document.querySelector(".chat-annotatable-answer");
      const text = answer.textContent;
      const at = text.indexOf("stronger comparison") + "stronger comparison".length;
      const walker = document.createTreeWalker(answer, NodeFilter.SHOW_TEXT);
      let offset = 0;
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (at <= offset + node.length) {
          const range = document.createRange();
          range.setStart(node, at - offset - 1);
          range.setEnd(node, at - offset);
          return range.getBoundingClientRect().right;
        }
        offset += node.length;
      }
      return null;
    });
    const pinBox = await pin.boundingBox();
    assert.ok(Math.abs(pinBox.x + pinBox.width / 2 - quoteEnd) < 2);
    assert.deepEqual(listed, ["?target_kind=reply&chat_id=viewport-regression"]);
    await pin.click();
    const popover = page.getByRole("dialog", { name: "Comment" });
    await popover.getByText("Which baseline?").waitFor();
    await page.keyboard.press("Escape");
    await popover.waitFor({ state: "detached" });
    await page.getByRole("button", { name: "1 open comment" }).click();
    const list = page.getByRole("dialog", { name: "Comments" });
    await list.getByRole("button", { name: "Send thread to agent" }).click();
    await page.waitForFunction(() => window.started?.length === 1);
    const [started] = await page.evaluate(() => window.started);
    assert.equal(started.kind, "project_chat");
    assert.deepEqual(started.request.comment_thread_ids, ["c1"]);
    assert.equal(started.request.chat_id, "viewport-regression");
    assert.match(started.requestId, /^[0-9a-f-]{36}$/);
    const listBox = await list.boundingBox();
    assert.ok(listBox.x >= 0 && listBox.x + listBox.width <= 390, JSON.stringify(listBox));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
