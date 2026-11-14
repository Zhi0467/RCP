import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { chromium } from "playwright";

const script = await readFile(
  new URL("../../src/rcp/artifact_selection.js", import.meta.url),
  "utf8",
);

test("HTML comment mode picks elements and preserves text selection", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.setContent(
      '<div id="chart" aria-label="Results" style="width:300px;height:200px;background:tan"></div><p id="quote">Text worth selecting</p>',
    );
    await page.addScriptTag({ content: script });
    await page.evaluate(() => {
      window.picks = [];
      installArtifactSelection(document, (value) => picks.push(value));
    });
    await page.locator("#chart").hover();
    assert.equal(await page.locator('[data-rcp-selection="hover"]').count(), 1);
    await page.locator("#chart").click();
    assert.equal(await page.evaluate(() => picks.at(-1)?.kind), "element");
    assert.equal(await page.evaluate(() => picks.at(-1)?.path), "div#chart");
    await page.locator("#quote").click({ clickCount: 3 });
    assert.equal(await page.evaluate(() => picks.at(-1)?.kind), "text");
  } finally {
    await browser.close();
  }
});

test("phone tap picks an element and long press draws a box", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    });
    await page.setContent(
      '<meta name="viewport" content="width=device-width"><div id="chart" style="width:300px;height:300px;background:tan"></div>',
    );
    await page.addScriptTag({ content: script });
    await page.evaluate(() => {
      window.picks = [];
      installArtifactSelection(document, (value) => picks.push(value));
    });
    await page.tap("#chart");
    assert.equal(await page.evaluate(() => picks.at(-1)?.kind), "element");
    const session = await page.context().newCDPSession(page);
    await session.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [{ x: 30, y: 30 }],
    });
    await page.waitForTimeout(550);
    await session.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ x: 160, y: 160 }],
    });
    await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    assert.equal(await page.evaluate(() => picks.at(-1)?.kind), "box");
  } finally {
    await browser.close();
  }
});
