import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("served viewer observes static edits and Undo, stops permanent errors, retries and aborts on close", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.clock.install();
    await page.addInitScript(() => {
      const original = window.fetch;
      window.stateAborts = 0;
      window.fetch = async (url, init) => {
        if (!String(url).endsWith("/state")) return original(url, init);
        const aborted = () => window.stateAborts++;
        init?.signal?.addEventListener("abort", aborted);
        try {
          return await original(url, init);
        } finally {
          init?.signal?.removeEventListener("abort", aborted);
        }
      };
    });
    let version = 1;
    let status = 200;
    let requests = 0;
    let release;
    await page.route("**/api/projects/project/artifacts", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/projects/project/artifacts/a/state", async (route) => {
      requests++;
      if (status === 0) {
        await new Promise((resolve) => {
          release = resolve;
        });
        await route.abort().catch(() => {});
        return;
      }
      await route.fulfill({
        status,
        json:
          status !== 200
            ? { detail: "Unavailable" }
            : {
                artifact_id: "a",
                name: "Test artifact",
                view: "html",
                media_type: "text/html",
                supplier: "turn",
                current_version: `v${version}`,
                version_number: version,
                can_undo: version > 1,
                live: null,
                editing_operation_id: null,
                viewer_url: `/api/projects/project/artifacts/a/viewer?v=${version}`,
                download_url: "/api/projects/project/artifacts/a/download",
                thread_href: null,
              },
      });
    });
    await page.route("**/api/projects/project/artifacts/a/viewer?*", (route) =>
      route.fulfill({ contentType: "text/html", body: "<p>Preview</p>" }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/artifacts.html`,
    );
    await page.evaluate(async () =>
      (await import("/src/artifacts/artifactViewer.ts")).openArtifact({
        projectId: "project",
        artifactId: "a",
      }),
    );
    await page.locator('iframe[src$="v=1"]').waitFor();
    version = 2;
    await page.clock.runFor(1600);
    await page.locator('iframe[src$="v=2"]').waitFor();
    version = 1; // Another member's Undo.
    await page.clock.runFor(1600);
    await page.locator('iframe[src$="v=1"]').waitFor();

    const title = page.getByLabel("Artifact title bar", { exact: true });
    const panel = page.locator(".artifact-viewer");
    const before = await panel.boundingBox();
    await title.focus();
    await page.keyboard.press("Enter");
    assert.equal((await panel.boundingBox()).width, page.viewportSize().width);
    await page.keyboard.press("Space");
    assert.equal((await panel.boundingBox()).width, before.width);
    const separator = page.getByRole("separator", { name: "Viewer width" });
    const width = Number(await separator.getAttribute("aria-valuenow"));
    await separator.focus();
    await page.keyboard.press("ArrowLeft");
    assert.ok(Number(await separator.getAttribute("aria-valuenow")) > width);
    await page.getByRole("button", { name: "Dock viewer", exact: true }).click();
    await page.getByRole("button", { name: "Restore Test artifact", exact: true }).click();
    await page.locator('iframe[src$="v=1"]').waitFor();

    status = 404;
    await page.clock.runFor(1600);
    await page.getByRole("alert").waitFor();
    const stoppedAt = requests;
    await page.clock.runFor(5000);
    assert.equal(requests, stoppedAt);
    status = 200;
    version = 3;
    await page.getByRole("button", { name: "Retry", exact: true }).click();
    await page.locator('iframe[src$="v=3"]').waitFor();
    assert.equal(requests, stoppedAt + 1);

    status = 0;
    const requestStarted = page.waitForRequest("**/api/projects/project/artifacts/a/state");
    await page.clock.runFor(1600);
    await requestStarted;
    const aborts = await page.evaluate(() => window.stateAborts);
    await page.getByRole("button", { name: "Close viewer", exact: true }).click();
    assert.equal(await page.evaluate(() => window.stateAborts), aborts + 1);
    release?.();
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
