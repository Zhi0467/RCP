import assert from "node:assert/strict";
import { test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

function pdfBytes(count) {
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""];
  const children = [];
  for (let index = 0; index < count; index++) {
    children.push(`${objects.length + 1} 0 R`);
    objects.push("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> >>");
  }
  objects[1] = `<< /Type /Pages /Count ${count} /Kids [${children.join(" ")}] >>`;
  let body = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(body));
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const start = Buffer.byteLength(body);
  body += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  body += offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)
    .join("");
  body += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(body);
}

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
  let scroll = await page.locator(".paper-pdf").evaluate((viewer) => viewer.scrollTop);
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
  scroll = await page.locator(".paper-pdf").evaluate((viewer) => viewer.scrollTop);
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
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.deepEqual(errors, []);
});
