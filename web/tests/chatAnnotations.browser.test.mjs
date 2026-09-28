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

test("an established selection is clipped to the answer when the pointer is released outside it", async () => {
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
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/chatAnnotationViewport.html`);
    const answer = page.locator(".chat-annotatable-answer");
    await answer.waitFor({ state: "visible" });
    const box = await answer.boundingBox();
    assert.ok(box);

    // Establish the overshooting selection independently of native drag behavior;
    // the regression is document-level pointer release and clipping to the answer.
    await page.mouse.move(box.x + 160, box.y + box.height + 60);
    await page.mouse.down();
    await answer.evaluate((element) => {
      const text = element.querySelector("p").firstChild;
      const next = element.nextElementSibling;
      const range = document.createRange();
      range.setStart(text, 1);
      range.setEndAfter(next);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    });
    await page.mouse.up();

    const composer = page.getByRole("form");
    await composer.waitFor({ state: "visible", timeout: 2000 });
    const selection = await answer.evaluate((element) => {
      const range = window.getSelection().getRangeAt(0);
      const text = element.querySelector("p").firstChild;
      return {
        nonempty: !range.collapsed,
        startsInside: element.contains(range.startContainer),
        endsInside: element.contains(range.endContainer),
        selectedLength: range.toString().trim().length,
        expectedLength: text.textContent.slice(1).length,
      };
    });
    assert.equal(selection.nonempty, true);
    assert.equal(selection.startsInside, true);
    assert.equal(selection.endsInside, true);
    assert.equal(selection.selectedLength, selection.expectedLength);
  } finally {
    await browser?.close();
    await server.close();
  }
});
