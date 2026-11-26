import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

const serve = (html) =>
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `import sys
from rcp.artifacts import html_preview_document
from rcp.report_reader import reader_report_bytes, report_frame_addon
data = reader_report_bytes(sys.stdin.read().encode())
print(html_preview_document(data, frame_addon=report_frame_addon())[0])`,
    ],
    {
      cwd: new URL("../..", import.meta.url),
      encoding: "utf8",
      input: html,
      maxBuffer: 16 * 1024 * 1024,
    },
  );

const wellFormed = serve(`<!doctype html><html><head><title>R</title></head><body>
<article class="report">
<header><h1>Title</h1><p class="dek">Finding.</p></header>
<section class="summary"><h2>Summary</h2><ul><li>One.</li></ul></section>
<section><h2>First finding</h2><aside>A side note.</aside><p id="claim">A claim.</p>
<figure><svg viewBox="0 0 10 10"></svg><figcaption>Caption.</figcaption></figure></section>
<section><h2>Second finding</h2><p>More.</p></section>
</article></body></html>`);

const hostile = serve(`<!doctype html><html><head><title>H</title>
<style>
* { margin: 0; padding: 0; }
html, body { height: 100vh; overflow: hidden; }
body { max-width: 700px; margin: 40px auto; font-family: Arial; background: #fff; }
p { color: #111; }
.bar { fill: #c0563f; }
</style></head><body><article class="report">
<h1>Hostile styles</h1>
<section><h2>Alpha</h2><p id="text">Body text.</p>
<figure id="fig"><svg viewBox="0 0 10 10"><rect class="bar" width="5" height="5"/></svg>
<figcaption>Caption.</figcaption></figure>
<table><tr>${"<td>wide-cell-content</td>".repeat(40)}</tr></table>
${"<p>Filler paragraph to make the page scroll.</p>".repeat(40)}</section>
<section><h2>Beta</h2><p>More.</p></section>
</article></body></html>`);

const freeform = serve(`<!doctype html><html><head><title>F</title>
<style>body { display: flex; gap: 2rem; font-family: Arial; }</style></head><body>
<main><h2>One</h2><p>Text.</p><h2>Two</h2><p>Text.</p></main><aside>Agent's own sidebar.</aside>
</body></html>`);

const open = async (width, body, colorScheme = "light") => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width, height: 900 }, colorScheme });
  await page.route("http://rcp.test/report", (route) =>
    route.fulfill({ contentType: "text/html", body }),
  );
  await page.goto("http://rcp.test/report");
  const frame = page.frames()[1];
  await frame.waitForLoadState();
  await frame.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  return { browser, frame };
};

test("a well-formed wide report gets a contents rail and margin notes", async () => {
  const { browser, frame } = await open(1400, wellFormed);
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
  test(`a ${width}px well-formed report folds the contents and keeps notes in the text column`, async () => {
    const { browser, frame } = await open(width, wellFormed);
    try {
      const layout = await frame.evaluate(() => {
        const box = (selector) => document.querySelector(selector).getBoundingClientRect();
        return {
          listShown:
            getComputedStyle(document.querySelector("nav.reader-toc ol")).display !== "none",
          noteAbove: box("aside").bottom <= box("#claim").top,
          fits: document.documentElement.scrollWidth <= window.innerWidth,
        };
      });
      assert.deepEqual(layout, { listShown: false, noteAbove: true, fits: true });
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
  test(`at ${width}px the reader layout and text colours win over the report's page CSS`, async () => {
    const { browser, frame } = await open(width, hostile, "dark");
    try {
      const layout = await frame.evaluate(() => {
        const style = (selector) => getComputedStyle(document.querySelector(selector));
        return {
          rail: Boolean(document.querySelector("nav.reader-toc")),
          textColor: style("#text").color,
          bodyColor: style("body").color,
          barFill: style("#fig rect").fill,
          scrolls: document.documentElement.scrollHeight > window.innerHeight,
          fits: document.documentElement.scrollWidth <= window.innerWidth,
        };
      });
      assert.equal(layout.rail, true);
      assert.equal(layout.textColor, layout.bodyColor);
      assert.equal(layout.barFill, "rgb(192, 86, 63)");
      assert.equal(layout.scrolls, true);
      assert.equal(layout.fits, true);
    } finally {
      await browser.close();
    }
  });

test("a page without the report article is shown as written", async () => {
  const { browser, frame } = await open(1400, freeform);
  try {
    const page = await frame.evaluate(() => ({
      rail: Boolean(document.querySelector("nav.reader-toc")),
      display: getComputedStyle(document.body).display,
      notes: document.querySelectorAll(".reader-note, .note-ref").length,
    }));
    assert.deepEqual(page, { rail: false, display: "flex", notes: 0 });
  } finally {
    await browser.close();
  }
});
