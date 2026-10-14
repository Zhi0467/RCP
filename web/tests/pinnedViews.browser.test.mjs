import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("pins share refresh, mount only the active version, open native nodes, and unpin at phone width", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 8433, strictPort: true },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    let pins = [
      { artifact_id: "a", title: "A long artifact title ".repeat(5), position: 0 },
      { artifact_id: "b", title: "B", position: 1 },
    ];
    let version = "v1";
    await page.route("**/api/projects/project/research-views**", async (route) => {
      if (route.request().method() === "DELETE") pins = pins.filter((p) => p.artifact_id !== "a");
      if (route.request().method() === "POST")
        assert.deepEqual(route.request().postDataJSON(), { artifact_id: "a", branch_id: null });
      await route.fulfill({ json: { pins } });
    });
    await page.route("**/api/projects/project/artifacts/*/state", (route) =>
      route.fulfill({
        json: {
          artifact_id: "a",
          current_version: version,
          pin_branch_id: null,
          can_pin: true,
          pinned: false,
          name: "A",
          viewer_url: "/api/projects/project/artifacts/a/viewer",
        },
      }),
    );
    await page.route("**/api/projects/project/artifacts/*/viewer", (route) => {
      return route.fulfill({
        contentType: "text/html",
        body: '<button onclick=\'parent.postMessage({type:"rcp-open-node",version:1,artifact_id:"a",node_id:"n"},location.origin)\'>Open</button>',
      });
    });
    await page.goto("http://127.0.0.1:8433/tests/fixtures/pinnedViews.html");
    const tabs = page.locator(".pinned-view-tab > button:first-child");
    await tabs.first().waitFor();
    assert.equal(await page.locator("iframe").count(), 0);
    await tabs.first().click();
    await page.frameLocator(".pinned-view iframe").locator("button").click();
    await page.waitForFunction(() => document.body.dataset.node === "n");
    assert.equal(await page.locator("iframe").count(), 1);
    for (const next of ["v2", "v1"]) {
      await page.evaluate(
        () => (window.previousFrame = document.querySelector(".pinned-view iframe")),
      );
      version = next;
      await page.evaluate(
        async (version) =>
          (await import("/tests/fixtures/resourceRuntime.ts")).store.observe("project", [
            { name: "artifacts", version },
          ]),
        next,
      );
      await page.waitForFunction(
        () => document.querySelector(".pinned-view iframe") !== window.previousFrame,
      );
      await page.frameLocator(".pinned-view iframe").locator("button").waitFor();
    }
    const layout = await page.evaluate(() => {
      const strip = document.querySelector(".artifacts-subnav");
      return {
        page: document.documentElement.scrollWidth,
        width: innerWidth,
        scroll: strip.scrollWidth > strip.clientWidth,
        targets: [...strip.querySelectorAll("button")].every(
          (b) => b.getBoundingClientRect().height >= 44,
        ),
        nested: strip.querySelectorAll("button button").length,
      };
    });
    assert.deepEqual(layout, { page: 390, width: 390, scroll: true, targets: true, nested: 0 });
    await page.locator(".pinned-view-unpin").first().click();
    await page.waitForFunction(() => !document.querySelector(".pinned-view"));
    assert.equal(await page.locator('.artifacts-subnav button[aria-pressed="true"]').count(), 1);
    await page.evaluate(async () =>
      (await import("/src/artifacts/artifactViewerModel.ts")).openArtifact({
        projectId: "project",
        artifactId: "a",
      }),
    );
    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === "POST" && response.url().endsWith("/research-views"),
      ),
      page.locator(".pin-view-action").click(),
    ]);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
