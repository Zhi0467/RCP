import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { chromium } from "playwright";
import { createPaperViteServer, pdfBytes } from "./paperTestServer.mjs";

/** Two 300x400 pages; each of `textPages` carries "Hello world" with its baseline 100pt from
 * the top, and "second line" under it. */
function textPdf(...textPages) {
  const stream = "BT /F1 24 Tf 40 300 Td (Hello world) Tj 0 -30 Td (second line) Tj ET";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] >>",
  ];
  for (let page = 1; page <= 2; page++)
    objects.push(
      textPages.includes(page)
        ? "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << /Font << /F1 6 0 R >> >> /Contents 5 0 R >>"
        : "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> >>",
    );
  objects.push(`<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`);
  objects.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");
  let body = "%PDF-1.4\n";
  const offsets = [];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(body));
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const start = Buffer.byteLength(body);
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  body += offsets.map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("");
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(body);
}

const MARKDOWN = "# Title\n\nFirst paragraph\nsecond line.\n\n$$\nx^2\n$$\n\nAfter $y^2$ math.";

const htmlPreview = JSON.parse(
  execFileSync(
    "uv",
    [
      "run",
      "--no-sync",
      "python",
      "-c",
      `import json
from dataclasses import replace
from rcp.artifact_comments import selection_frame_addon
from rcp.artifact_views import artifact_content, inline_viewer_document
source = b'''<!doctype html><html><body style="margin:0;font:20px Georgia;background:white;color:black">
<p id="words" style="margin:24px"><span id="word">Select</span> these preview words.</p>
<svg id="plot" aria-label="Saved plot" width="220" height="180" viewBox="0 0 220 180" style="margin:0 24px"><rect width="220" height="180" fill="teal"/></svg>
<button id="counter" onclick="this.textContent=String(Number(this.textContent)+1)">0</button>
<div style="height:1800px"></div></body></html>'''
shell, shell_csp = inline_viewer_document('figure.html', 'html', content_url='/html-preview-content', live=None, selectable=True)
selection = selection_frame_addon()
selection = replace(selection, frame_script=selection.frame_script + "\\nlisten(privatePort,'message',event=>{if(event.data?.kind==='rcp-artifact-selection-enable') document.documentElement.dataset.selectionReady='true';});")
content, _, content_csp = artifact_content('figure.html', 'text/html', source, frame_addon=selection)
print(json.dumps(dict(shell=shell,shell_csp=shell_csp,content=content,content_csp=content_csp)))`,
    ],
    {
      cwd: new URL("../..", import.meta.url),
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: "src", UV_NO_SYNC: "1" },
    },
  ).trim(),
);

async function openHtmlPreview(t, width) {
  const { server, origin, close } = await createPaperViteServer([
    {
      name: "paper-html-zoom-test",
      resolveId: (id) => (id === "/html-zoom-test.tsx" ? id : null),
      load(id) {
        if (id !== "/html-zoom-test.tsx") return;
        return `import React from 'react';
          import { createRoot } from 'react-dom/client';
          import { PaperPreview } from '/src/paper/PaperPreview.tsx';
          import '/src/styles.css';
          const toolbar = document.querySelector('header');
          const root = createRoot(document.getElementById('root'));
          window.comments = []; window.cancelled = [];
          window.show = (revision=1, active=true, id='html') => root.render(<PaperPreview projectId="p" file={{file_id:id,path:id+'.html',kind:'text',format:'html',sha256:'html-bytes'}} tree={{files:[],revision,saved_revision:revision}} text="" active={active} toolbar={active ? toolbar : null} cursorLine={1} onDirectComment={comment=>window.comments.push(comment)} onDirectCancel={clear=>window.cancelled.push(clear)} />);`;
      },
    },
  ]);
  t.after(close);
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width, height: 760 } });
  page.setDefaultTimeout(5000);
  page.setDefaultNavigationTimeout(30_000);
  await page.routeWebSocket(/^ws:\/\/127\.0\.0\.1:\d+\/\?token=/, (socket) =>
    socket.send(JSON.stringify({ type: "connected" })),
  );
  const errors = [];
  const states = [];
  const loads = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      message.text() !== "Unrecognized Content-Security-Policy directive 'navigate-to'."
    )
      errors.push(message.text());
  });
  let state = { revision: 1, key: "html-and-image-1" };
  let failed = false;
  let release;
  let hold = new Promise((resolve) => (release = resolve));
  t.after(() => release());
  await page.route("**/html-zoom-test.html", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await server.transformIndexHtml(
        "/html-zoom-test.html",
        '<!doctype html><html data-theme="aqua"><body style="margin:0"><header style="display:flex;height:56px;align-items:center"></header><div id="root" style="display:flex;height:680px;max-height:calc(100vh - 56px)"></div><script type="module" src="/html-zoom-test.tsx"></script></body></html>',
      ),
    }),
  );
  await page.route("**/files/*/preview/state", async (route) => {
    const response = { ...state };
    const failure = failed;
    states.push(route.request().url());
    await hold;
    await route
      .fulfill(
        failure ? { status: 503, json: { detail: "Preview unavailable" } } : { json: response },
      )
      .catch(() => {});
  });
  await page.route(/\/files\/[^/]+\/preview(?:\?.*)?$/, (route) => {
    loads.push(route.request().url());
    return route.fulfill({
      contentType: "text/html",
      body: htmlPreview.shell,
      headers: { "Content-Security-Policy": htmlPreview.shell_csp },
    });
  });
  await page.route("**/html-preview-content?*", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: htmlPreview.content,
      headers: { "Content-Security-Policy": htmlPreview.content_csp },
    }),
  );
  await page.goto(`${origin}/html-zoom-test.html`);
  await page.waitForFunction(() => window.show);
  return {
    page,
    errors,
    states,
    loads,
    release: () => {
      release();
      hold = Promise.resolve();
    },
    hold: () => (hold = new Promise((resolve) => (release = resolve))),
    update: (next) => (state = next),
    fail: (value) => (failed = value),
  };
}

for (const width of [1280, 390])
  test(`HTML preview waits for current state and zooms with its comments at ${width}px`, async (t) => {
    const fixture = await openHtmlPreview(t, width);
    const { page, states, loads, errors } = fixture;
    const firstCheck = page.waitForRequest((request) => request.url().endsWith("/preview/state"), {
      timeout: 3000,
    });
    await page.evaluate(() => window.show());
    await firstCheck;
    assert.equal(await page.locator(".paper-html-preview iframe").count(), 0);
    await page.getByLabel("Loading preview").waitFor();
    fixture.release();
    const frame = page.locator(".paper-html-preview iframe");
    await frame.waitFor();
    assert.match(await frame.getAttribute("src"), /\/preview\?revision=1$/);
    const shell = () => page.frame({ url: /\/files\/html\/preview\?revision=/ });
    const artifact = () => shell()?.childFrames()[0]?.childFrames()[0];
    await page.waitForFunction(() => window.frames[0]?.frames[0]?.frames.length === 1);
    await artifact().locator("#counter").click();
    const initial = await frame.boundingBox();
    const plus = page.getByRole("button", { name: "Zoom in", exact: true });
    const minus = page.getByRole("button", { name: "Zoom out", exact: true });
    const reset = page.getByRole("button", { name: "Reset preview zoom", exact: true });
    await plus.click();
    assert.equal(await reset.innerText(), "125%");
    const physical = await frame.boundingBox();
    assert.ok(Math.abs(physical.width - initial.width) < 1);
    assert.ok(Math.abs(physical.height - initial.height) < 1);
    assert.equal(await artifact().locator("#counter").innerText(), "1");
    await minus.click();
    await minus.click();
    assert.equal(await reset.innerText(), "75%");
    await page.getByRole("button", { name: "Comment on the page" }).click();
    await artifact().waitForFunction(
      () => document.documentElement.dataset.selectionReady === "true",
    );
    for (const scale of [0.75, 1.5]) {
      if (scale === 1.5) {
        await reset.click();
        await plus.click();
        await plus.click();
      }
      const before = await page.evaluate(() => window.comments.length);
      const plot = await artifact().locator("#plot").boundingBox();
      const start = { x: plot.x + 4, y: plot.y + 4 };
      const end = { x: plot.x + plot.width - 4, y: plot.y + plot.height - 4 };
      await page.mouse.move(start.x, start.y);
      await page.mouse.down();
      await page.mouse.move(end.x, end.y, { steps: 6 });
      await page.mouse.up();
      await page.waitForFunction((count) => window.comments.length > count, before);
      const box = await page.evaluate(() => window.comments.at(-1));
      assert.match(box.quote, /Saved plot/);
      assert.ok(Math.abs(box.anchor.left - start.x) < 2);
      assert.ok(Math.abs(box.anchor.right - end.x) < 2);
      assert.ok(Math.abs(box.anchor.top - start.y) < 2);
      assert.equal(await artifact().locator("[data-rcp-selection]").count(), 1);
      const cancelled = await page.evaluate(() => window.cancelled.length);
      await plus.click();
      await page.waitForFunction((count) => window.cancelled.length > count, cancelled);
      await artifact().locator("[data-rcp-selection]").waitFor({ state: "detached" });
      const textBefore = await page.evaluate(() => window.comments.length);
      await artifact().locator("#word").dblclick();
      await page.waitForFunction((count) => window.comments.length > count, textBefore);
      assert.match(await page.evaluate(() => window.comments.at(-1).quote), /Select/);
    }
    await reset.click();
    for (let i = 0; i < 2; i++) await minus.click();
    assert.equal(await minus.isDisabled(), true);
    for (let i = 0; i < 5; i++) await plus.click();
    assert.equal(await plus.isDisabled(), true);
    await reset.click();
    await artifact().evaluate(() => window.scrollTo(0, 300));
    assert.ok(await artifact().evaluate(() => window.scrollY > 0));
    await plus.click();
    const count = states.length;
    fixture.update({ revision: 2, key: "html-and-image-1" });
    const rechecked = page.waitForResponse((response) => response.url().endsWith("/preview/state"));
    await page.evaluate(() => window.show(2));
    await rechecked;
    assert.equal(states.length, count + 1);
    assert.equal(loads.length, 1);
    assert.equal(await artifact().locator("#counter").innerText(), "1");
    fixture.update({ revision: 3, key: "html-and-image-2" });
    const replaced = page.waitForResponse((response) =>
      /\/preview\?revision=3$/.test(response.url()),
    );
    await page.evaluate(() => window.show(3));
    await replaced;
    assert.equal(loads.length, 2);
    assert.equal(await reset.innerText(), "125%");
    await page.evaluate(() => window.show(3, true, "other"));
    await page.waitForFunction(() => document.querySelector("iframe")?.title === "other.html");
    assert.equal(await reset.innerText(), "100%");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
  });

test("HTML preview rechecks hidden entry, aborts old reads, and retries a visible refusal", async (t) => {
  const fixture = await openHtmlPreview(t, 390);
  const { page, states, errors } = fixture;
  fixture.release();
  await page.evaluate(() => window.show());
  const frame = page.locator(".paper-html-preview iframe");
  await frame.waitFor();
  await page.waitForFunction(() => window.frames[0]?.frames[0]?.frames.length === 1);
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  const count = states.length;
  await page.evaluate(() => window.show(2, false));
  await frame.waitFor({ state: "hidden" });
  assert.equal(states.length, count);
  fixture.hold();
  fixture.update({ revision: 2, key: "updated-image" });
  const resuming = page.waitForRequest((request) => request.url().endsWith("/html/preview/state"));
  await page.evaluate(() => window.show(2));
  await resuming;
  assert.equal(await frame.isVisible(), false);
  fixture.update({ revision: 3, key: "other-file" });
  const switching = page.waitForRequest((request) =>
    request.url().endsWith("/other/preview/state"),
  );
  await page.evaluate(() => window.show(3, true, "other"));
  await switching;
  assert.equal(await frame.count(), 0);
  fixture.release();
  await frame.waitFor();
  assert.equal(await frame.getAttribute("title"), "other.html");
  assert.match(await frame.getAttribute("src"), /revision=3$/);
  assert.equal(await page.getByRole("button", { name: "Reset preview zoom" }).innerText(), "100%");
  fixture.fail(true);
  const refusal = page.waitForResponse((response) => response.url().endsWith("/preview/state"));
  await page.evaluate(() => window.show(4, true, "other"));
  await refusal;
  const retry = page.getByRole("button", { name: "Retry HTML preview" });
  await retry.waitFor();
  assert.equal(await frame.isVisible(), false);
  fixture.fail(false);
  fixture.update({ revision: 4, key: "other-file-updated" });
  await retry.click();
  await frame.waitFor();
  await page.waitForFunction(() => document.querySelector("iframe")?.src.endsWith("revision=4"));
  assert.equal(await frame.isVisible(), true);
  assert.deepEqual(
    errors.filter((message) => !message.includes("503")),
    [],
  );
});

test("preview selections name their source, and boxes on PDF pages open comments", async (t) => {
  const { server, close } = await createPaperViteServer([
    {
      name: "paper-preview-comments-test",
      resolveId(id) {
        if (id === "/preview-comments-test.tsx") return id;
      },
      load(id) {
        if (id !== "/preview-comments-test.tsx") return;
        return `import React from 'react';
          import { createRoot } from 'react-dom/client';
          import { PaperPreview } from '/src/paper/PaperPreview.tsx';
          import { resolvePreviewSelection } from '/src/paper/previewComments.ts';
          const bar = document.createElement('header');
          document.body.prepend(bar);
          const host = document.getElementById('root');
          const root = createRoot(host);
          const files = [
            {file_id:'md',path:'notes.md',format:'markdown',kind:'text',sha256:'a'},
            {file_id:'txt',path:'data.txt',format:'other',kind:'text',sha256:'b'},
            {file_id:'pdf',path:'figs/doc.pdf',format:'pdf',kind:'binary',sha256:'c'},
            {file_id:'tex',path:'sections/a.tex',format:'latex',kind:'text',sha256:'d'},
            {file_id:'html',path:'page.html',format:'html',kind:'text',sha256:'e'}
          ];
          window.comments = [];
          window.show = (id, text = '', saved = 7, htmlSha = 'e') => { const shown = files.map(f => f.file_id === 'html' ? {...f, sha256: htmlSha} : f); root.render(<PaperPreview projectId="p" file={shown.find(f => f.file_id === id)} tree={{files: shown, revision: saved, saved_revision: saved}} text={text} active toolbar={bar} cursorLine={1} onDirectComment={c => window.comments.push({quote: c.quote, where: c.where, anchor: c.anchor, clear: c.clear})} onDirectCancel={clear => window.cancels.push(clear)} />); };
          window.cancels = [];
          window.select = (element, from, to) => {
            const range = document.createRange();
            if (from === undefined) range.selectNodeContents(element);
            else { range.setStart(element.firstChild, from); range.setEnd(element.firstChild, to); }
            getSelection().removeAllRanges();
            getSelection().addRange(range);
            return resolvePreviewSelection(getSelection(), host)?.where ?? null;
          };
          window.quote = () => resolvePreviewSelection(getSelection(), host)?.quote ?? null;
          window.selectSpan = (first, last, end = last.firstChild.length) => {
            const range = document.createRange();
            range.setStart(first.firstChild ?? first, 0);
            range.setEnd(last.firstChild ?? last, end);
            getSelection().removeAllRanges();
            getSelection().addRange(range);
            return resolvePreviewSelection(getSelection(), host) ?? null;
          };`;
      },
    },
  ]);
  t.after(close);
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 600, height: 700 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/preview-comments-test.html", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await server.transformIndexHtml(
        "/preview-comments-test.html",
        '<!doctype html><html><body><div id="root" style="display:flex;height:600px;width:100%"></div><script type="module" src="/preview-comments-test.tsx"></script></body></html>',
      ),
    }),
  );
  await page.route("**/files/pdf/raw?*", (route) =>
    route.fulfill({ contentType: "application/pdf", body: textPdf(2) }),
  );
  await page.route("**/paper/engines", (route) =>
    route.fulfill({ json: { typst: "ready", latex: "ready", errors: {} } }),
  );
  await page.route("**/paper/compile", (route) =>
    route.fulfill({
      json: {
        status: "ok",
        root_path: "main.tex",
        log_tail: "",
        pdf_url: "/compiled.pdf",
        synctex: {
          "main.tex": [[1, 1, 20]],
          "sections/a.tex": [
            [5, 1, 70],
            [9, 1, 300],
            [12, 2, 90],
            [20, 2, 380],
          ],
        },
      },
    }),
  );
  await page.route("**/compiled.pdf", (route) =>
    route.fulfill({ contentType: "application/pdf", body: textPdf(1, 2) }),
  );
  let htmlState = { revision: 7, key: "e" };
  await page.route("**/files/*/preview/state", (route) => route.fulfill({ json: htmlState }));
  await page.route("**/files/*/preview?*", (route) =>
    route.fulfill({ contentType: "text/html", body: "" }),
  );
  await page.goto(
    `http://127.0.0.1:${server.httpServer.address().port}/preview-comments-test.html`,
  );
  await page.waitForFunction(() => window.show);

  // Markdown: the selected element's source lines; display math resolves through its block.
  await page.evaluate((text) => window.show("md", text), MARKDOWN);
  await page.locator(".katex-display").waitFor();
  assert.equal(await page.locator("h1").evaluate((el) => window.select(el)), "notes.md, line 1");
  assert.equal(
    await page
      .locator("p")
      .first()
      .evaluate((el) => window.select(el, 0, 5)),
    "notes.md, lines 3-4",
  );
  assert.equal(
    await page.locator(".katex-display").evaluate((el) => window.select(el)),
    "notes.md, lines 6-8",
  );
  // Inline math is quoted once, as shown, without KaTeX's hidden MathML.
  assert.equal(
    await page.evaluate(() => {
      window.select([...document.querySelectorAll("p")].at(-1));
      return window.quote().replace(/\s+/g, " ");
    }),
    "After y2 math.",
  );
  // A selection that ends where the next block starts (a triple-click) names its own block.
  assert.equal(
    await page.evaluate(() => {
      const [heading, paragraph] = document.querySelectorAll("h1, p");
      return window.selectSpan(heading, paragraph, 0).where;
    }),
    "notes.md, line 1",
  );

  // Plain text: lines counted from the newlines before the range.
  await page.evaluate(() => window.show("txt", "alpha\nbeta\ngamma\n"));
  const pre = page.locator("pre.paper-plain-preview");
  assert.equal(await pre.evaluate((el) => window.select(el, 6, 10)), "data.txt, line 2");
  assert.equal(await pre.evaluate((el) => window.select(el, 6, 17)), "data.txt, lines 2-3");

  // Uploaded PDF: the text layer selects natively and names the page.
  await page.evaluate(() => window.show("pdf"));
  const words = page.locator('.paper-pdf-page[data-page="2"] .textLayer span', {
    hasText: "Hello",
  });
  await words.waitFor();
  assert.equal(await words.evaluate((el) => window.select(el)), "figs/doc.pdf, page 2");
  // Text-layer lines keep a break between them in the quote.
  assert.match(
    await page.evaluate(() => {
      const spans = document.querySelectorAll('.paper-pdf-page[data-page="2"] .textLayer span');
      return window.selectSpan(spans[0], spans[spans.length - 1]).quote;
    }),
    /world\s+second/,
  );

  // A mouse box from an empty corner of the page collects the text inside it.
  await words.scrollIntoViewIfNeeded();
  const slot = await page.locator('.paper-pdf-page[data-page="2"]').boundingBox();
  const span = await words.boundingBox();
  await page.mouse.move(slot.x + 4, slot.y + 4);
  await page.mouse.down();
  await page.mouse.move(span.x + span.width + 4, span.y + span.height + 4, { steps: 4 });
  await page.mouse.up();
  const box = await page.evaluate(() => window.comments.at(-1));
  assert.match(box.quote, /Hello world/);
  // The coach gets the box's place on the page, since no pixels are sent.
  assert.match(box.where, /^figs\/doc\.pdf, page 2, area [0-2]-\d+% across and [0-2]-\d+% down$/);
  assert.ok(box.anchor.right > box.anchor.left);
  assert.equal(await page.locator(".paper-select-box").count(), 1);
  await page.evaluate(() => window.comments.at(-1).clear());
  assert.equal(await page.locator(".paper-select-box").count(), 0);
  // A box over part of a line quotes the boxed words, not the whole line or nothing.
  await page.mouse.move(slot.x + 4, slot.y + 4);
  await page.mouse.down();
  await page.mouse.move(span.x + span.width * 0.45, span.y + span.height + 4, { steps: 4 });
  await page.mouse.up();
  assert.match(await page.evaluate(() => window.comments.at(-1).quote), /^Hello\s*$/);
  await page.evaluate(() => window.comments.at(-1).clear());
  // Escape during a drag cancels it: no box, no comment.
  const before = await page.evaluate(() => window.comments.length);
  await page.mouse.move(slot.x + 4, slot.y + 4);
  await page.mouse.down();
  await page.mouse.move(slot.x + 120, slot.y + 120, { steps: 4 });
  await page.keyboard.press("Escape");
  await page.mouse.up();
  assert.equal(await page.locator(".paper-select-box").count(), 0);
  assert.equal(await page.evaluate(() => window.comments.length), before);
  // A drag that starts on text stays a native selection.
  await page.mouse.move(span.x + 2, span.y + span.height / 2);
  await page.mouse.down();
  await page.mouse.move(span.x + span.width - 2, span.y + span.height / 2, { steps: 4 });
  await page.mouse.up();
  assert.equal(await page.evaluate(() => window.comments.length), before);
  assert.match(await page.evaluate(() => getSelection().toString()), /Hello/);

  // LaTeX: the compile root's page, near the source line SyncTeX puts above the selection.
  await page.evaluate(() => window.show("tex"));
  const compiled = page.locator('.paper-pdf-page[data-page="1"] .textLayer span', {
    hasText: "Hello",
  });
  await compiled.waitFor();
  assert.equal(
    await compiled.evaluate((el) => window.select(el)),
    "main.tex, page 1, near sections/a.tex line 5",
  );
  // Across pages, the line comes from where the selection ends on its last page.
  const next = page.locator('.paper-pdf-page[data-page="2"] .textLayer span', { hasText: "Hello" });
  await next.waitFor();
  assert.equal(
    await page.evaluate(() => {
      const spans = (n) =>
        document.querySelectorAll(`.paper-pdf-page[data-page="${n}"] .textLayer span`);
      return window.selectSpan(spans(1)[0], spans(2)[0]).where;
    }),
    "main.tex, pages 1-2, near sections/a.tex line 12",
  );

  // HTML: the latest saved page in the inline shell, with comment mode in the panel tools.
  await page.evaluate(() => window.show("html"));
  const frame = page.locator(".paper-html-preview iframe");
  await frame.waitFor();
  assert.match(
    await frame.getAttribute("src"),
    /\/api\/projects\/p\/paper\/files\/html\/preview\?revision=7$/,
  );
  const toggle = page.locator('header button[aria-pressed="false"]');
  await toggle.click();
  await page.locator('header button[aria-pressed="true"]').waitFor();
  // A box keeps its area, so two boxes on one labelled image differ for the coach.
  const shell = page.frame({ url: /\/preview\?revision=7$/ });
  const post = (selection) =>
    shell.evaluate(
      (selection) =>
        parent.postMessage(
          { type: "rcp-artifact-selection", version: 1, selection, description: "" },
          location.origin,
        ),
      selection,
    );
  await post({
    kind: "box",
    rect: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
    viewport: { width: 500, height: 400 },
    elements: [{ path: "body > img", label: "Plot", text: "" }],
  });
  await page.waitForFunction(() => window.comments.at(-1)?.where?.startsWith("page.html"));
  const htmlBox = await page.evaluate(() => window.comments.at(-1));
  // Elements quote their own text before a label they inherit, and a repeat once.
  await post({
    kind: "box",
    rect: { x: 0, y: 0, width: 1, height: 1 },
    viewport: { width: 500, height: 400 },
    elements: [
      { path: "section > p:nth-child(1)", label: "Results", text: "First finding" },
      { path: "section > p:nth-child(2)", label: "Results", text: "First finding" },
      { path: "section > p:nth-child(3)", label: "Results", text: "Second finding" },
    ],
  });
  await page.waitForFunction(() => window.comments.at(-1)?.quote.includes("finding"));
  assert.equal(
    await page.evaluate(() => window.comments.at(-1).quote),
    "First finding, Second finding",
  );
  assert.equal(htmlBox.quote, "Plot");
  assert.equal(htmlBox.where, "page.html, area 10-40% across and 20-60% down");
  // A selection the frame cancels reports the clear of the comment it opened.
  await post(null);
  await page.waitForFunction(() => window.cancels.length === 1);
  assert.equal(await page.evaluate(() => window.cancels[0] === window.comments.at(-1).clear), true);
  // Leaving comment mode cancels the comment the frame opened.
  await post({ kind: "text", text: "Plot caption", surrounding_text: "" });
  await page.waitForFunction(() => window.comments.at(-1)?.quote === "Plot caption");
  await page.locator('header button[aria-pressed="true"]').click();
  await page.waitForFunction(() => window.cancels.length === 2);
  assert.equal(await page.evaluate(() => window.cancels[1] === window.comments.at(-1).clear), true);
  // Another file's save leaves the page alone; this file's save reloads it, and the
  // comment it opened stays open, since its quote and place are already taken.
  await page.locator('header button[aria-pressed="false"]').click();
  await post({ kind: "text", text: "Plot title", surrounding_text: "" });
  await page.waitForFunction(() => window.comments.at(-1)?.quote === "Plot title");
  const mark = () =>
    page.evaluate(() => (document.querySelector(".paper-html-preview iframe").dataset.seen = "1"));
  const seen = () =>
    page.evaluate(() => document.querySelector(".paper-html-preview iframe").dataset.seen);
  await mark();
  htmlState = { revision: 8, key: "e" };
  await page.evaluate(() => window.show("html", "", 8));
  await page.waitForTimeout(200);
  assert.equal(await seen(), "1");
  htmlState = { revision: 9, key: "e2" };
  await page.evaluate(() => window.show("html", "", 9, "e2"));
  await page.waitForFunction(
    () => !document.querySelector(".paper-html-preview iframe")?.dataset.seen,
  );
  await page.waitForTimeout(200);
  assert.equal(await page.evaluate(() => window.cancels.length), 2);

  await page.setViewportSize({ width: 390, height: 700 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
});

test("a touch drag on a PDF page draws no box", async (t) => {
  const { server, close } = await createPaperViteServer([
    {
      name: "paper-box-touch-test",
      resolveId(id) {
        if (id === "/box-touch-test.tsx") return id;
      },
      load(id) {
        if (id !== "/box-touch-test.tsx") return;
        return `import React from 'react';
          import { createRoot } from 'react-dom/client';
          import PaperPdf from '/src/paper/PaperPdf.tsx';
          import '/src/paper/paper-preview.css';
          const bar = document.createElement('header');
          document.body.prepend(bar);
          window.boxes = 0;
          createRoot(document.getElementById('root')).render(<PaperPdf url="/doc.pdf" toolbar={bar} path="doc.pdf" onBox={() => window.boxes++} />);`;
      },
    },
  ]);
  t.after(close);
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const context = await browser.newContext({
    hasTouch: true,
    viewport: { width: 390, height: 700 },
  });
  const page = await context.newPage();
  await page.route("**/box-touch-test.html", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await server.transformIndexHtml(
        "/box-touch-test.html",
        '<!doctype html><html><body><div id="root" style="display:flex;height:600px;width:100%"></div><script type="module" src="/box-touch-test.tsx"></script></body></html>',
      ),
    }),
  );
  await page.route("**/doc.pdf", (route) =>
    route.fulfill({ contentType: "application/pdf", body: pdfBytes(3) }),
  );
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/box-touch-test.html`);
  const slot = page.locator('.paper-pdf-page[data-page="1"]');
  await slot.locator("canvas").waitFor();
  await slot.evaluate((element) => {
    const { left, top } = element.getBoundingClientRect();
    const fire = (type, x, y) =>
      element.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          pointerType: "touch",
          pointerId: 7,
          button: 0,
          clientX: left + x,
          clientY: top + y,
        }),
      );
    fire("pointerdown", 10, 10);
    fire("pointermove", 120, 120);
    fire("pointerup", 120, 120);
  });
  assert.equal(await page.locator(".paper-select-box").count(), 0);
  assert.equal(await page.evaluate(() => window.boxes), 0);
});
