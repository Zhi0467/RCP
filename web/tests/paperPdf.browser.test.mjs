import assert from "node:assert/strict";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";
import { pdfBytes } from "./paperTestServer.mjs";

test("PDF keeps its reading position and old canvases during replacement, renders nearby pages, and resizes without fetching", async (t) => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
    plugins: [
      {
        name: "paper-pdf-test",
        resolveId(id) {
          if (id === "/paper-pdf-test.tsx") return id;
        },
        load(id) {
          if (id !== "/paper-pdf-test.tsx") return;
          return `import React from 'react';
          import { createRoot } from 'react-dom/client';
          import PaperPdf from '/src/paper/PaperPdf.tsx';
          import '/src/paper/paper-preview.css';
          const root = createRoot(document.getElementById('root'));
          window.preview = url => root.render(<PaperPdf url={url} />);
          window.preview('/first.pdf');`;
        },
      },
    ],
  });
  await server.listen();
  t.after(() => server.close());
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 600, height: 700 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/paper-pdf-test.html", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await server.transformIndexHtml(
        "/paper-pdf-test.html",
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root" style="display:flex;height:600px;width:100%"></div><script type="module" src="/paper-pdf-test.tsx"></script></body></html>',
      ),
    }),
  );
  let fetches = 0;
  let release;
  const replacement = new Promise((resolve) => {
    release = resolve;
  });
  await page.route("**/*.pdf", async (route) => {
    fetches += 1;
    if (route.request().url().endsWith("/second.pdf")) await replacement;
    await route.fulfill({ contentType: "application/pdf", body: pdfBytes(30) });
  });
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/paper-pdf-test.html`);
  await page.locator('.paper-pdf-page[data-page="1"] canvas').waitFor();
  assert.ok((await page.locator("canvas").count()) < 10);
  await page.locator(".paper-pdf").evaluate((viewer) => {
    viewer.scrollTop = 6000;
  });
  await page.waitForFunction(
    () => !document.querySelector('.paper-pdf-page[data-page="1"] canvas'),
  );
  await page.waitForFunction(() => document.querySelectorAll("canvas").length > 0);
  await page
    .locator("canvas")
    .first()
    .evaluate((canvas) => {
      canvas.dataset.previous = "true";
    });
  const requested = page.waitForRequest("**/second.pdf");
  await page.evaluate(() => window.preview("/second.pdf"));
  await requested;
  assert.equal(await page.locator('canvas[data-previous="true"]').count(), 1);
  await page.locator(".paper-pdf").evaluate((viewer) => {
    viewer.scrollTop = 11000;
  });
  await page.waitForFunction(() => {
    const bounds = document.querySelector(".paper-pdf").getBoundingClientRect();
    return [...document.querySelectorAll(".paper-pdf-page")].some((slot) => {
      const rect = slot.getBoundingClientRect();
      return rect.top < bounds.bottom && rect.bottom > bounds.top && slot.querySelector("canvas");
    });
  });
  const scroll = await page.locator(".paper-pdf").evaluate((viewer) => viewer.scrollTop);
  await page
    .locator("canvas")
    .first()
    .evaluate((canvas) => {
      canvas.dataset.previous = "true";
    });
  release();
  await page.getByLabel("Rendering PDF").waitFor({ state: "hidden" });
  await page.waitForFunction(() => !document.querySelector('canvas[data-previous="true"]'));
  assert.equal(await page.locator(".paper-pdf").evaluate((viewer) => viewer.scrollTop), scroll);
  await page.setViewportSize({ width: 390, height: 700 });
  await page.waitForFunction(() => {
    const canvas = document.querySelector("canvas");
    return canvas && canvas.width < 400;
  });
  assert.equal(fetches, 2);
  assert.ok((await page.locator("canvas").count()) < 10);
  assert.deepEqual(errors, []);
});

test("preview returns from Markdown, retains root scroll, and keeps failed actions on one row", async (t) => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
    plugins: [
      {
        name: "preview-lifecycle-test",
        resolveId(id) {
          if (id === "/preview-test.tsx") return id;
        },
        load(id) {
          if (id !== "/preview-test.tsx") return;
          return `import React from 'react';
          import { createRoot } from 'react-dom/client';
          import { PaperPreview } from '/src/paper/PaperPreview.tsx';
          const root = createRoot(document.getElementById('root'));
          const files = [
            {file_id:'main',path:'main.typ',format:'typst',kind:'text',sha256:'a'},
            {file_id:'part',path:'part.typ',format:'typst',kind:'text',sha256:'b'},
            {file_id:'md',path:'sections/main.md',format:'markdown',kind:'text',sha256:'c'},
            {file_id:'methods',path:'sections/methods.md',format:'markdown',kind:'text',sha256:'d'}
          ];
          window.show = (id, broken=false) => root.render(<PaperPreview projectId="p" file={files.find(f=>f.file_id===id)} tree={{files,saved_revision:1}} text={id==='md' ? '[Methods](methods.md) [External](https://example.org) [Bad](mailto:test@example.org)' : broken ? '#bad' : id==='main' ? '#include "part.typ"' : 'Part'} active onOpenFile={id=>window.opened=id} />);
          window.show('main');`;
        },
      },
    ],
  });
  await server.listen();
  t.after(() => server.close());
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 700 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(
    ({ pdf }) => {
      window.RealWorker = window.Worker;
      window.Worker = class {
        constructor(url) {
          if (!String(url).includes("typst.worker"))
            return new window.RealWorker(url, { type: "module" });
        }
        postMessage(request) {
          const broken = request.sources.some((s) =>
            new TextDecoder().decode(s.bytes).includes("#bad"),
          );
          setTimeout(
            () =>
              this.onmessage({
                data: {
                  id: request.id,
                  ...(broken
                    ? { error: "main.typ:1: Unknown variable" }
                    : { pdf: new Uint8Array(pdf) }),
                },
              }),
            10,
          );
        }
        terminate() {}
      };
    },
    { pdf: [...pdfBytes(30)] },
  );
  // Keep PDF.js's actual worker; only the compiler is stubbed.
  await page.route("**/preview-test.html", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await server.transformIndexHtml(
        "/preview-test.html",
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root" style="display:flex;height:600px"></div><script type="module" src="/preview-test.tsx"></script></body></html>',
      ),
    }),
  );
  await page.route("**/paper/engines", (route) =>
    route.fulfill({ json: { typst: "ready", latex: "ready", errors: {} } }),
  );
  await page.route("**/raw?revision=1", (route) =>
    route.fulfill({
      body: route.request().url().includes("/main/") ? '#include "part.typ"' : "Part",
    }),
  );
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/preview-test.html`);
  await page.locator("canvas").first().waitFor();
  await page.locator(".paper-pdf").evaluate((el) => {
    el.scrollTop = 3000;
  });
  await page.waitForFunction(() => document.querySelector(".paper-pdf").scrollTop === 3000);
  await page.evaluate(() => window.show("md"));
  await page.getByRole("link", { name: "Methods", exact: true }).click();
  assert.equal(await page.evaluate(() => window.opened), "methods");
  assert.equal(await page.getByRole("link", { name: "External" }).getAttribute("target"), "_blank");
  assert.match(await page.getByRole("link", { name: "External" }).getAttribute("rel"), /noopener/);
  assert.equal(await page.getByText("Bad", { exact: true }).getAttribute("href"), null);
  await page.evaluate(() => window.show("main"));
  await page.locator("canvas").first().waitFor();
  assert.equal(await page.locator(".paper-pdf").evaluate((el) => el.scrollTop), 3000);
  await page.evaluate(() => window.show("part"));
  await page.locator("canvas").first().waitFor();
  await page.waitForFunction(() => document.querySelector(".paper-pdf")?.scrollTop === 3000);
  await page.locator(".paper-pdf").evaluate((el) => {
    el.scrollTop = 0;
  });
  const before = await page.locator(".paper-pdf-pages").boundingBox();
  await page.evaluate(() => window.show("part", true));
  await page.getByLabel("View compile log").waitFor();
  const after = await page.locator(".paper-pdf-pages").boundingBox();
  assert.equal(after.y, before.y);
  const log = await page.getByLabel("View compile log").boundingBox();
  const download = await page.getByLabel("Download PDF").boundingBox();
  assert.equal(log.y, download.y);
  assert.ok(log.height >= 44);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
});
