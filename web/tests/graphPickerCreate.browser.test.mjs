import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("the picker forks the viewed head into a named branch, retries idempotently, and switches", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    const requests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/projects/project/graph-refs", async (route) => {
      const body = route.request().postDataJSON();
      requests.push(body);
      await route.fulfill(
        requests.length === 1
          ? { status: 409, json: { detail: { code: "branch_name_taken" } } }
          : requests.length === 2
            ? { status: 503, json: { detail: "temporary" } }
            : {
                json: {
                  kind: "branch",
                  branch_id: body.request_id,
                  name: body.name,
                  archived: false,
                  running: false,
                  needs_you: false,
                  created_at: "2026-10-21T00:00:00Z",
                },
              },
      );
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/graphPickerCreate.html`,
    );
    await page.getByRole("button", { name: "Branch: main" }).click();
    await page.getByRole("button", { name: "New branch" }).click();
    const field = page.getByRole("textbox", { name: "New branch name" });
    const popoverText = await page.getByRole("dialog").innerText();
    await field.fill("taken");
    await Promise.all([page.waitForResponse(/graph-refs$/), field.press("Enter")]);
    await page.locator('[aria-invalid="true"]').waitFor();
    assert.equal(await page.getByRole("button", { name: "Retry new branch" }).count(), 0);
    await field.fill("probe");
    assert.equal(await page.locator('[aria-invalid="true"]').count(), 0);
    await Promise.all([page.waitForResponse(/graph-refs$/), field.press("Enter")]);
    const retry = page.getByRole("button", { name: "Retry new branch" });
    await retry.waitFor();
    // Failures add no caption: the popover's text is what it showed before.
    assert.equal(await page.getByRole("dialog").innerText(), popoverText);
    assert.equal(await page.getByRole("alert").count(), 0);
    await Promise.all([page.waitForResponse(/graph-refs$/), retry.click()]);
    await page.getByRole("button", { name: "Branch: probe" }).waitFor();
    assert.deepEqual(
      requests.map(({ at, name }) => [at, name]),
      [
        ["main@7", "taken"],
        ["main@7", "probe"],
        ["main@7", "probe"],
      ],
    );
    assert.notEqual(requests[0].request_id, requests[1].request_id);
    assert.equal(requests[1].request_id, requests[2].request_id);
    assert.equal(await page.getByRole("dialog").count(), 0);
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
