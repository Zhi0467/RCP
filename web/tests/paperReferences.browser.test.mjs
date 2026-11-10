import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createPaperTestServer, openPaper, PaperTestRoom } from "./paperTestServer.mjs";

let server, browser;
before(async () => {
  server = await createPaperTestServer();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
const bib =
  "@article{one, title={A Reference}, author={An Author}, year=2024, doi={10.123/example}, abstract={An abstract}}";
async function open(t, width = 1280) {
  return openPaper(t, {
    browser,
    origin: server.origin,
    width,
    room: new PaperTestRoom("See [@one]"),
    initialText: "See [@one]",
    configure(fixture) {
      fixture.tree.files[0].sha256 = "source";
      fixture.tree.files.push({
        file_id: "bib",
        path: "references.bib",
        kind: "text",
        format: "bibtex",
        sha256: "bib1",
        size: bib.length,
      });
      fixture.rooms.set("bib", new PaperTestRoom(bib));
      const respond = fixture.respond.bind(fixture);
      fixture.respond = (path, method, body) =>
        path.endsWith("/bib/raw")
          ? { body: bib, contentType: "text/plain" }
          : respond(path, method, body);
    },
  });
}
test("reference list inserts at the cursor, hover opens a reference and source navigation locates its entry", async (t) => {
  const { page, room, errors } = await open(t);
  await page.locator(".paper-reference-fold").click({ timeout: 5000 });
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.locator(".paper-reference-entry").click();
  await room.waitForText("See [@one][@one]");
  const line = page.locator(".cm-line").first();
  const point = await line.evaluate((element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const at = node.textContent.indexOf("one");
      if (at < 0) continue;
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + 3);
      const rect = range.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    }
  });
  await page.mouse.move(point.x, point.y);
  await page.locator(".paper-cite-tooltip").waitFor();
  assert.equal(await page.locator(".paper-cite-tooltip h3").textContent(), "A Reference");
  await page.mouse.click(point.x, point.y);
  await page.locator(".paper-reference-page").waitFor();
  assert.equal(
    await page.locator(".paper-reference-page a").getAttribute("href"),
    "https://doi.org/10.123/example",
  );
  await page.locator(".paper-reference-source").click();
  await page.waitForFunction(() =>
    document.querySelector(".cm-content")?.textContent.includes("@article"),
  );
  assert.deepEqual(errors, []);
});
test("390px Refs lists entries, opens a page and returns without overflow", async (t) => {
  const { page, errors } = await open(t, 390);
  await page.getByRole("button", { name: "Refs", exact: true }).click({ timeout: 5000 });
  await page.locator(".paper-reference-entry:visible").click();
  await page.locator(".paper-reference-page:visible").waitFor();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
  await page.getByRole("button", { name: "Back to references" }).click();
  await page.locator(".paper-reference-entry:visible").waitFor();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
  assert.deepEqual(errors, []);
});

for (const width of [1280, 390]) {
  test(`export uses the open file and shows a structured refusal at ${width}px`, async (t) => {
    const { page, context } = await openPaper(t, {
      browser,
      origin: server.origin,
      width,
      configure(fixture) {
        fixture.tree.files[0].format = "latex";
        fixture.tree.files[0].path = "main.tex";
        const respond = fixture.respond.bind(fixture);
        fixture.respond = (path, method, body) => {
          if (path.endsWith("/engines"))
            return { json: { latex: "unavailable", typst: "ready", errors: {} } };
          if (path.endsWith("/export/arxiv"))
            return {
              status: 409,
              json: { reason: "not_root", log_tail: "fixture compile diagnostic" },
            };
          return respond(path, method, body);
        };
      },
    });
    if (width === 390) await page.getByRole("button", { name: "Show files" }).click();
    const exportButton = page.getByRole("button", { name: "Export for arXiv", exact: true });
    const request = page.waitForRequest((request) => request.url().includes("/export/arxiv"));
    await exportButton.click();
    assert.equal(new URL((await request).url()).searchParams.get("file_id"), "f1");
    const dialog = page.getByRole("dialog", { name: "Export for arXiv", exact: true });
    await dialog.waitFor();
    assert.equal(await dialog.locator("pre").textContent(), "fixture compile diagnostic");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    await context.route("**/paper/export/arxiv?*", (route) =>
      route.fulfill({ body: "zip fixture", contentType: "application/zip" }),
    );
    const download = page.waitForEvent("download");
    await exportButton.click();
    assert.equal((await download).suggestedFilename(), "main-arxiv.zip");
  });
}

test("export is disabled for a non-LaTeX file", async (t) => {
  const { page } = await open(t);
  assert.ok(await page.getByRole("button", { name: "Export for arXiv" }).isDisabled());
});
