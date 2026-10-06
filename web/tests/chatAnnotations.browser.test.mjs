import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("a wide annotation composer stays interactive inside a keyboard-shrunken visual viewport", async () => {
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
    await page.addInitScript(() => {
      class TestVisualViewport extends EventTarget {
        width = window.innerWidth;
        height = window.innerHeight;
        offsetLeft = 0;
        offsetTop = 0;

        shrink(height, offsetTop) {
          this.height = height;
          this.offsetTop = offsetTop;
          this.dispatchEvent(new Event("resize"));
        }
      }
      const viewport = new TestVisualViewport();
      Object.defineProperty(window, "visualViewport", {
        configurable: true,
        value: viewport,
      });
      window.shrinkTestVisualViewport = (height, offsetTop) => viewport.shrink(height, offsetTop);
    });

    await page.route("**/api/projects/*/chats/*/questions", (route) => route.fulfill({ json: [] }));
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/chatAnnotationViewport.html`);
    const comment = page.getByRole("button", { name: "Comment on this answer" });
    await comment.waitFor({ state: "visible" });
    assert.equal(await page.evaluate(() => window.innerWidth), 844);
    await comment.click();
    const composer = page.getByRole("form", { name: "Select answer text" });
    await composer.waitFor({ state: "visible" });

    await page.evaluate(() => window.shrinkTestVisualViewport(196, 48));
    await page.waitForFunction(() => {
      const element = document.querySelector(".chat-annotation-composer");
      return element instanceof HTMLElement && element.getBoundingClientRect().bottom <= 232;
    });

    const layout = await composer.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
        overflowY: style.overflowY,
      };
    });
    assert.ok(layout.top >= 60, `composer top ${layout.top} escaped the visual viewport`);
    assert.ok(layout.bottom <= 232, `composer bottom ${layout.bottom} escaped the visual viewport`);
    assert.equal(layout.width, 320);
    assert.equal(layout.overflowY, "auto");
    assert.ok(layout.scrollHeight > layout.clientHeight, "constrained composer should scroll");

    const source = page.getByRole("textbox", { name: "Select answer text" });
    const originalValue = await source.inputValue();
    await source.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
    const selection = await source.evaluate((element) => ({
      readOnly: element.readOnly,
      selectionStart: element.selectionStart,
      selectionEnd: element.selectionEnd,
      value: element.value,
    }));
    assert.deepEqual(selection, {
      readOnly: true,
      selectionStart: 0,
      selectionEnd: originalValue.length,
      value: originalValue,
    });

    await page.getByRole("button", { name: "Comment on selection" }).click();
    const commentBox = page.getByRole("textbox", { name: "Comment" });
    await commentBox.fill("Show the comparison.");
    // Enter submits the comment, as the Add comment button does.
    await commentBox.press("Enter");
    const annotationCount = page.getByRole("button", { name: "1 annotation" });
    await annotationCount.waitFor({ state: "visible" });
    await annotationCount.click();
    const stagedComment = page.getByRole("textbox", { name: "Comment for annotation 1" });
    await stagedComment.waitFor({ state: "visible" });
    await page.getByRole("textbox", { name: "Message" }).fill("Send the staged annotation.");
    await page.getByRole("button", { name: "Start Discuss turn" }).click();

    assert.equal(await comment.isDisabled(), true);
    assert.equal(await stagedComment.isDisabled(), true);
    assert.equal(
      await page.getByRole("button", { name: "Remove annotation 1" }).isDisabled(),
      true,
    );
    assert.equal(await stagedComment.inputValue(), "Show the comparison.");

    await page.evaluate(() => window.resolveViewportTask());
    await annotationCount.waitFor({ state: "detached" });
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("a pointer selection offers Comment, keeps the selection copyable, and opens the composer only on request", async () => {
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
    const offer = page.getByRole("button", { name: "Comment", exact: true });
    await offer.waitFor({ state: "visible", timeout: 2000 });
    const composer = page.getByRole("form", { name: "Add annotation" });
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
    // The sweep overshot into the Comment button; the staged text stops at the answer's end.
    assert.equal(
      (await composer.locator("blockquote").innerText()).trim(),
      "he reported improvement needs a stronger comparison and a variance estimate.",
    );

    // Dismissing the composer does not bring the offer straight back.
    await page.getByRole("textbox", { name: "Comment", exact: true }).waitFor();
    assert.equal(
      await page.evaluate(
        () => document.activeElement?.closest(".chat-annotation-composer") !== null,
      ),
      true,
      "Choosing Comment puts focus in the composer",
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
      return box.right <= window.innerWidth && Math.abs(box.bottom + 14 - first.top) < 2;
    });
  } finally {
    await browser?.close();
    await server.close();
  }
});
