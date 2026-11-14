import assert from "node:assert/strict";
import test from "node:test";
import { resolve } from "node:path";
import { build } from "vite";
import { chromium } from "playwright";

function pdfBytes(pages = 6) {
  const stream = "BT /F1 24 Tf 40 240 Td (Text worth selecting) Tj ET";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Kids [${Array.from({ length: pages }, (_, index) => `${index + 5} 0 R`).join(" ")}] /Count ${pages} >>`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    ...Array.from(
      { length: pages },
      () =>
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
    ),
  ];
  let pdf = "%PDF-1.7\n";
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(pdf.length);
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const start = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)
    .join("")}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(pdf);
}

test("PDF fixture extracts its text and renders visible glyphs", async () => {
  const { getDocument } = await import("pdfjs-dist/legacy/build/pdf.mjs");
  const task = getDocument({ data: new Uint8Array(pdfBytes()), useSystemFonts: true });
  try {
    const pdf = await task.promise;
    assert.equal(pdf.numPages, 6);
    const first = await pdf.getPage(1);
    const last = await pdf.getPage(6);
    for (const page of [first, last]) {
      const content = await page.getTextContent();
      assert.equal(content.items.map((item) => item.str ?? "").join(""), "Text worth selecting");
    }
    const surface = pdf.canvasFactory.create(400, 300);
    await first.render({ canvas: surface.canvas, viewport: first.getViewport({ scale: 1 }) })
      .promise;
    const pixels = surface.context.getImageData(35, 35, 230, 35).data;
    assert.ok(pixels.some((value, index) => index % 4 !== 3 && value < 128));
    pdf.canvasFactory.destroy(surface);
  } finally {
    await task.destroy();
  }
});

test("PDF viewer renders selectable text and page-relative area anchors", async () => {
  const result = await build({
    configFile: false,
    logLevel: "silent",
    build: {
      write: false,
      lib: {
        entry: resolve("web/src/comments/pdf.ts"),
        name: "PdfViewer",
        cssFileName: "pdf",
        formats: ["iife"],
      },
    },
  });
  const output = (Array.isArray(result) ? result[0] : result).output;
  const script = output.find((item) => item.type === "chunk").code;
  const style = output.find(
    (item) => item.type === "asset" && item.fileName.endsWith(".css"),
  ).source;
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.route("https://viewer.test/**", (route) =>
      route.request().url().endsWith("/paper.pdf")
        ? route.fulfill({ contentType: "application/pdf", body: pdfBytes() })
        : route.fulfill({
            contentType: "text/html",
            headers: {
              "content-security-policy":
                "default-src 'none'; script-src 'unsafe-inline'; worker-src blob:; connect-src 'self'; style-src 'unsafe-inline'; font-src blob: data:; img-src data: blob:",
            },
            body: `<style>${style}</style><div id="pdf" style="height:600px"></div>`,
          }),
    );
    await page.goto("https://viewer.test/");
    await page.addScriptTag({ content: script });
    await page.evaluate(async () => {
      window.anchors = [];
      window.pdf = PdfViewer.mountPdf(
        document.getElementById("pdf"),
        "/paper.pdf",
        (anchor) => anchors.push(anchor),
        (error) => {
          throw error;
        },
      );
      await pdf.ready;
    });
    const layer = page.locator('.rcp-pdf-page[data-page="1"] .textLayer');
    assert.equal(await layer.textContent(), "Text worth selecting");
    const text = layer.locator("span").first();
    await text.waitFor();
    const whitespacePosts = await text.evaluate((span) => {
      const node = span.firstChild;
      const start = node.textContent.indexOf(" ");
      const range = document.createRange();
      range.setStart(node, start);
      range.setEnd(node, start + 1);
      const selection = document.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      const before = anchors.length;
      span.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      selection.removeAllRanges();
      return anchors.length - before;
    });
    assert.equal(whitespacePosts, 0);
    const glyphs = await text.evaluate((span) => {
      const node = span.firstChild;
      const first = document.createRange();
      first.setStart(node, 0);
      first.setEnd(node, 1);
      const last = document.createRange();
      last.setStart(node, node.textContent.length - 1);
      last.setEnd(node, node.textContent.length);
      return {
        first: first.getBoundingClientRect().toJSON(),
        last: last.getBoundingClientRect().toJSON(),
      };
    });
    assert.ok(glyphs.first.width > 0 && glyphs.first.height > 0);
    assert.ok(glyphs.last.width > 0 && glyphs.last.height > 0);
    await page.mouse.move(glyphs.first.x + 0.5, glyphs.first.y + glyphs.first.height / 2);
    await page.mouse.down();
    await page.mouse.move(glyphs.last.right - 0.5, glyphs.last.y + glyphs.last.height / 2, {
      steps: 12,
    });
    await page.mouse.up();
    assert.equal(await page.evaluate(() => anchors.at(-1)?.kind), "pdf");
    assert.equal(await page.evaluate(() => anchors.at(-1)?.page), 1);
    assert.match(await page.evaluate(() => anchors.at(-1)?.text), /Text worth selecting/);
    await page.getByRole("button", { name: "Select area" }).click();
    const bounds = await page.locator(".rcp-pdf-page").first().boundingBox();
    await page.mouse.move(bounds.x + 20, bounds.y + 100);
    await page.mouse.down();
    await page.mouse.move(bounds.x + 120, bounds.y + 180, { steps: 5 });
    await page.mouse.up();
    const anchor = await page.evaluate(() => anchors.at(-1));
    assert.equal(anchor.kind, "pdf");
    assert.equal(anchor.text, "");
    assert.ok(anchor.rect.width > 0 && anchor.rect.width < 1);
    assert.equal(
      await page.evaluate(async () => {
        await pdf.prepare(1);
        return pdf.context().textRect(0, 4, 1)?.width > 0;
      }),
      true,
    );
    assert.equal(
      await page.evaluate(async () => {
        const root = document.getElementById("pdf");
        const preparing = pdf.prepare(6);
        root.scrollTop = root.scrollHeight;
        await new Promise(requestAnimationFrame);
        root.scrollTop = 0;
        await new Promise(requestAnimationFrame);
        await preparing;
        return pdf.context().textRect(0, 4, 6)?.width > 0;
      }),
      true,
    );
    await page.evaluate(() => pdf.destroy());
  } finally {
    await browser.close();
  }
});
