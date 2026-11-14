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
    await page.evaluate(() => getSelection().removeAllRanges());
    const quote = await page.locator("#quote").boundingBox();
    await page.mouse.move(quote.x + 2, quote.y + quote.height / 2);
    await page.mouse.down();
    await page.mouse.move(quote.x + 100, quote.y + quote.height / 2, { steps: 8 });
    await page.mouse.up();
    assert.equal(await page.evaluate(() => picks.at(-1)?.kind), "text");
    assert.ok(await page.evaluate(() => picks.at(-1)?.text.length));
    await page.locator("#chart").click();
    assert.equal(await page.evaluate(() => picks.at(-1)?.kind), "element");
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

test("HTML anchors locate context, boxes, missing paths, and follow scrolling", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await page.setContent(
      '<p>Repeated quote</p><div style="height:900px"></div><p id="target">Context: Repeated <b>quote</b> here</p><div id="chart" style="width:200px;height:100px"></div>',
    );
    await page.addScriptTag({ content: script });
    await page.evaluate(() => {
      window.positions = [];
      window.locate = installArtifactLocator(document, (value) => positions.push(value));
      locate({
        id: "text",
        anchor: {
          kind: "text",
          text: "Repeated quote",
          surrounding_text: "Context: Repeated quote here",
        },
        scroll: true,
      });
    });
    await page.waitForFunction(() =>
      positions.some((value) => value.id === "text" && value.rect?.y < 600),
    );
    assert.equal(await page.locator('[data-rcp-selection="locate"]').count(), 1);
    await page.evaluate(() => {
      locate({
        id: "box",
        anchor: {
          kind: "box",
          elements: [{ path: "#chart", region: { x: 0.25, y: 0.25, width: 0.5, height: 0.5 } }],
        },
      });
      locate({ id: "gone", anchor: { kind: "element", path: "#gone" } });
      locate({ id: "invalid", anchor: { kind: "element", path: "[" } });
    });
    const positions = await page.evaluate(() => window.positions);
    assert.equal(positions.find((value) => value.id === "box").rect.width, 100);
    assert.equal(positions.find((value) => value.id === "gone").rect, null);
    assert.equal(positions.find((value) => value.id === "invalid").rect, null);
    const before = positions.filter((value) => value.id === "box").at(-1).rect.y;
    await page.evaluate(() => window.scrollBy(0, -80));
    await page.waitForFunction(
      (before) => positions.filter((value) => value.id === "box").at(-1).rect.y === before + 80,
      before,
    );
    await page.evaluate(() => {
      document.querySelector("#target").innerHTML = "Edited: Repeated quote remains";
      locate({
        id: "text",
        anchor: {
          kind: "text",
          text: "Repeated quote",
          surrounding_text: "Edited: Repeated quote remains",
        },
      });
    });
    assert.ok(
      await page.evaluate(() => positions.filter((value) => value.id === "text").at(-1).rect),
    );
  } finally {
    await browser.close();
  }
});

test("HTML locate finds cross-block text and scrolls the anchor region inside tall containers", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await page.setContent(
      '<div style="height:800px"></div><div id="scroll" style="height:220px;overflow:auto"><div id="tall" style="height:1800px"><p>First paragraph</p><p>Second<br>line</p></div></div><div style="height:800px"></div>',
    );
    await page.addScriptTag({ content: script });
    await page.evaluate(() => {
      window.positions = [];
      window.locate = installArtifactLocator(document, (value) => positions.push(value));
      locate({
        id: "text",
        anchor: { kind: "text", text: "First paragraph Second line", surrounding_text: "" },
        scroll: true,
      });
    });
    const text = await page.evaluate(
      () => positions.filter((value) => value.id === "text").at(-1).rect,
    );
    assert.ok(text);
    assert.ok(text.y >= 0 && text.y + text.height <= 600);
    await page.evaluate(() => {
      document.querySelector("#scroll").scrollTop = 1200;
      locate({
        id: "region",
        anchor: {
          kind: "box",
          elements: [{ path: "#tall", region: { x: 0, y: 0.02, width: 0.1, height: 0.03 } }],
        },
        scroll: true,
      });
    });
    const result = await page.evaluate(() => {
      const rect = positions.filter((value) => value.id === "region").at(-1).rect;
      const scroll = document.querySelector("#scroll").getBoundingClientRect();
      return { rect, top: scroll.top, bottom: scroll.bottom };
    });
    assert.ok(result.rect.y >= Math.max(0, result.top));
    assert.ok(result.rect.y + result.rect.height <= Math.min(600, result.bottom));
  } finally {
    await browser.close();
  }
});
