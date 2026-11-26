import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

const report = `<!doctype html><html><head><title>R</title></head><body>
<article class="report">
<header><h1>Title</h1><p class="dek">Finding.</p></header>
<section class="summary"><h2>Summary</h2><ul><li>One.</li></ul></section>
<section><h2>First finding</h2><p id="claim">A claim.</p><aside>A side note.</aside>
<figure><svg viewBox="0 0 10 10"></svg><figcaption>Caption.</figcaption></figure></section>
<section><h2>Second finding</h2><p>More.</p></section>
</article></body></html>`;

const served = execFileSync(
  "uv",
  [
    "run",
    "python",
    "-c",
    `import sys
from rcp.artifacts import html_preview_document
from rcp.report_reader import report_frame_addon
print(html_preview_document(sys.stdin.read().encode(), frame_addon=report_frame_addon())[0])`,
  ],
  {
    cwd: new URL("../..", import.meta.url),
    encoding: "utf8",
    input: report,
    maxBuffer: 16 * 1024 * 1024,
  },
);

const open = async (width) => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  await page.route("http://rcp.test/report", (route) =>
    route.fulfill({ contentType: "text/html", body: served }),
  );
  await page.goto("http://rcp.test/report");
  const frame = page.frames()[1];
  await frame.waitForSelector("nav.reader-toc");
  return { browser, frame };
};

test("a wide report gets a contents rail and margin notes beside the text", async () => {
  const { browser, frame } = await open(1400);
  try {
    const layout = await frame.evaluate(() => {
      const box = (selector) => document.querySelector(selector).getBoundingClientRect();
      return {
        entries: [...document.querySelectorAll(".reader-toc-entry")].map((a) => a.textContent),
        headingIds: [...document.querySelectorAll("h2")].filter((h) => h.id).length,
        railExcluded: document.querySelector("nav.reader-toc").hasAttribute("data-rcp-selection"),
        navRight: box("nav.reader-toc").right,
        textLeft: box("#claim").left,
        textRight: box("#claim").right,
        noteLeft: box("aside").left,
        noteTop: box("aside").top,
        claimTop: box("#claim").top,
        marker: document.querySelector("#claim .note-ref")?.textContent,
      };
    });
    assert.deepEqual(layout.entries, ["First finding", "Second finding"]);
    assert.equal(layout.headingIds, 0);
    assert.equal(layout.railExcluded, true);
    assert.ok(layout.navRight < layout.textLeft);
    assert.ok(layout.noteLeft > layout.textRight);
    assert.ok(Math.abs(layout.noteTop - layout.claimTop) < 20);
    assert.equal(layout.marker, "1");
    await frame.click(".reader-toc-toggle");
    await frame.waitForFunction(
      () => document.querySelector(".reader-toc-toggle").getAttribute("aria-expanded") === "false",
    );
  } finally {
    await browser.close();
  }
});

test("a phone-width report folds the contents and keeps notes inline", async () => {
  const { browser, frame } = await open(390);
  try {
    const layout = await frame.evaluate(() => {
      const box = (selector) => document.querySelector(selector).getBoundingClientRect();
      return {
        listShown: getComputedStyle(document.querySelector("nav.reader-toc ol")).display !== "none",
        noteBelow: box("aside").top > box("#claim").bottom,
        fits: document.documentElement.scrollWidth <= window.innerWidth,
      };
    });
    assert.deepEqual(layout, { listShown: false, noteBelow: true, fits: true });
    await frame.click(".reader-toc-toggle");
    assert.notEqual(
      await frame.evaluate(
        () => getComputedStyle(document.querySelector("nav.reader-toc ol")).display,
      ),
      "none",
    );
  } finally {
    await browser.close();
  }
});
