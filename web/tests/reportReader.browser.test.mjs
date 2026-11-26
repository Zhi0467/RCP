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

const serve = (html) =>
  execFileSync(
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
      input: html,
      maxBuffer: 16 * 1024 * 1024,
    },
  );
const served = serve(report);

const messy = serve(`<!doctype html><title>M</title><body>
<div id="stray">Written outside the article.</div>
<article class="report"><aside>A note with no paragraph before it.</aside>
<h1>No sections at all</h1>
<p id="url">https://example.com/${"a".repeat(300)}</p>
<table><tr>${"<td>wide-cell-content</td>".repeat(40)}</tr></table>
</article>
<article class="report" id="second"><p>A second article.</p></article>
</body>`);
const open = async (width, body = served) => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  await page.route("http://rcp.test/report", (route) =>
    route.fulfill({ contentType: "text/html", body }),
  );
  await page.goto("http://rcp.test/report");
  const frame = page.frames()[1];
  await frame.waitForSelector("article.report");
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

for (const width of [1260, 390])
  test(`a ${width}px report folds the contents and keeps notes inline`, async () => {
    const { browser, frame } = await open(width);
    try {
      const layout = await frame.evaluate(() => {
        const box = (selector) => document.querySelector(selector).getBoundingClientRect();
        return {
          listShown:
            getComputedStyle(document.querySelector("nav.reader-toc ol")).display !== "none",
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

for (const width of [1400, 390])
  test(`a ${width}px report with stray content stays in one readable column`, async () => {
    const { browser, frame } = await open(width, messy);
    try {
      const layout = await frame.evaluate(() => {
        const box = (selector) => document.querySelector(selector).getBoundingClientRect();
        const main = document.querySelector("article.report").getBoundingClientRect();
        const within = (selector) =>
          box(selector).left >= main.left - 1 && box(selector).right <= main.right + 1;
        return {
          fits: document.documentElement.scrollWidth <= window.innerWidth,
          stray: within("#stray"),
          second: within("#second") && box("#second").top >= main.bottom - 1,
          url: within("#url"),
          rail: Boolean(document.querySelector("nav.reader-toc")),
        };
      });
      assert.deepEqual(layout, { fits: true, stray: true, second: true, url: true, rail: false });
    } finally {
      await browser.close();
    }
  });
