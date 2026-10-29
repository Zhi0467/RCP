import assert from "node:assert/strict";
import { test } from "node:test";
import { chromium } from "playwright";
import { createPaperViteServer, pdfBytes } from "./paperTestServer.mjs";

/** Two 300x400 pages; page `textPage` carries "Hello world" with its baseline 100pt from the top. */
function textPdf(textPage) {
  const stream = "BT /F1 24 Tf 40 300 Td (Hello world) Tj ET";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] >>",
  ];
  for (let page = 1; page <= 2; page++)
    objects.push(
      page === textPage
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

const MARKDOWN = "# Title\n\nFirst paragraph\nsecond line.\n\n$$\nx^2\n$$\n\nAfter math.";

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
          window.show = (id, text = '') => root.render(<PaperPreview projectId="p" file={files.find(f => f.file_id === id)} tree={{files, saved_revision: 7}} text={text} active toolbar={bar} cursorLine={1} onDirectComment={c => window.comments.push({quote: c.quote, where: c.where, anchor: c.anchor, clear: c.clear})} />);
          window.select = (element, from, to) => {
            const range = document.createRange();
            if (from === undefined) range.selectNodeContents(element);
            else { range.setStart(element.firstChild, from); range.setEnd(element.firstChild, to); }
            getSelection().removeAllRanges();
            getSelection().addRange(range);
            return resolvePreviewSelection(getSelection(), host)?.where ?? null;
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
          ],
        },
      },
    }),
  );
  await page.route("**/compiled.pdf", (route) =>
    route.fulfill({ contentType: "application/pdf", body: textPdf(1) }),
  );
  await page.route("**/preview?*", (route) =>
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
  assert.match(box.where, /^figs\/doc\.pdf, page 2, area [0-2]-\d+% across, [0-2]-\d+% down$/);
  assert.ok(box.anchor.right > box.anchor.left);
  assert.equal(await page.locator(".paper-select-box").count(), 1);
  await page.evaluate(() => window.comments.at(-1).clear());
  assert.equal(await page.locator(".paper-select-box").count(), 0);
  // A drag that starts on text stays a native selection.
  await page.mouse.move(span.x + 2, span.y + span.height / 2);
  await page.mouse.down();
  await page.mouse.move(span.x + span.width - 2, span.y + span.height / 2, { steps: 4 });
  await page.mouse.up();
  assert.equal(await page.evaluate(() => window.comments.length), 1);
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

  // HTML: the saved revision in the inline shell, with comment mode in the panel tools.
  await page.evaluate(() => window.show("html"));
  const frame = page.locator(".paper-html-preview iframe");
  assert.match(
    await frame.getAttribute("src"),
    /\/api\/projects\/p\/paper\/files\/html\/preview\?revision=7&presentation=inline$/,
  );
  const toggle = page.locator('header button[aria-pressed="false"]');
  await toggle.click();
  await page.locator('header button[aria-pressed="true"]').waitFor();

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
