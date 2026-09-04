import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("reopening an indexed Experiment restores selection when its exact hash is unchanged", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const address = server.httpServer?.address();
    assert.ok(address && typeof address === "object");
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const exactHash =
      "#/projects/project-one?view=runs&experiment=experiment%2Fbranch-child&episode=child-experiment-episode&target=branch&branch=auto-research-parent&parent=auto-research-parent";
    await page.goto(
      `http://127.0.0.1:${address.port}/tests/fixtures/indexedExperimentReopen.html${exactHash}`,
    );

    await page.getByText("Selected child transcript").waitFor();
    await page
      .getByRole("button", { name: "Collapse Experiment loop episode Reproduce the baseline" })
      .click();
    await page
      .getByRole("button", { name: "Expand Experiment loop episode Reproduce the baseline" })
      .waitFor();
    assert.equal(await page.getByText("Selected child transcript").count(), 0);

    await page.evaluate(() => {
      window.hashChangesAfterCollapse = 0;
      window.addEventListener("hashchange", () => {
        window.hashChangesAfterCollapse += 1;
      });
    });
    await page
      .getByRole("button", { name: "Expand Experiment loop episode Reproduce the baseline" })
      .click();

    await page.getByText("Selected child transcript").waitFor();
    assert.equal(await page.evaluate(() => window.location.hash), exactHash);
    assert.equal(await page.evaluate(() => window.hashChangesAfterCollapse), 0);

    await page
      .getByRole("button", { name: "Collapse Experiment loop episode Reproduce the baseline" })
      .click();
    assert.equal(await page.getByText("Selected child transcript").count(), 0);

    await page
      .getByRole("region", { name: "Episode turns" })
      .getByRole("link", { name: /Reproduce the baseline/ })
      .click();

    await page.getByText("Selected child transcript").waitFor();
    assert.equal(await page.evaluate(() => window.location.hash), exactHash);
    assert.equal(await page.evaluate(() => window.hashChangesAfterCollapse), 0);
  } finally {
    await browser?.close();
    await server.close();
  }
});
