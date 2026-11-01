import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

let server, browser, origin;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

for (const branch of ["member-fork", "episode-branch"]) {
  test(`${branch} merge releases navigation during delayed dispatch at 390px`, async (t) => {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    page.setDefaultTimeout(5000);
    t.after(() => page.close());
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    const request = Promise.withResolvers();
    const response = Promise.withResolvers();
    await page.route("**/graph-refs/*/merge", async (route) => {
      request.resolve(route.request().postDataJSON());
      await response.promise;
      await route.fulfill({ json: { operation_id: "merge-task", status: "running" } });
    });
    await page.goto(`${origin}/tests/fixtures/graphPickerMerge.html`);
    await page.getByRole("button", { name: "Branch: main" }).click();
    const merge = page.getByRole("button", { name: `Merge ${branch}`, exact: true });
    await merge.waitFor();
    assert.equal(await merge.getAttribute("title"), "Merge");
    await merge.click();
    await page.locator("dialog").getByRole("button", { name: "Merge", exact: true }).click();
    const body = await request.promise;
    assert.match(body.request_id, /^[0-9a-f-]{36}$/);
    assert.equal(await page.locator("dialog").count(), 0);
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    await page.getByRole("heading", { name: "Inbox", exact: true }).waitFor();
    response.resolve();
    await page.getByRole("status").filter({ hasText: "Merge is running" }).waitFor();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    assert.deepEqual(errors, []);
  });
}

test("picker confirms unfinished episode jobs before retrying, then releases navigation", async (t) => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.setDefaultTimeout(3000);
  t.after(() => page.close());
  const requests = [];
  const response = Promise.withResolvers();
  await page.route("**/graph-refs/*/merge", async (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    if (!body.confirm_unfinished_jobs)
      return route.fulfill({
        status: 409,
        json: {
          detail: {
            code: "unfinished_jobs_confirmation_required",
            jobs: [
              { kind: "watcher", id: "job", command: "python train.py", status: "unobservable" },
            ],
          },
        },
      });
    await response.promise;
    await route.fulfill({ json: { operation_id: "merge-task", status: "running" } });
  });
  await page.goto(`${origin}/tests/fixtures/graphPickerMerge.html`);
  await page.getByRole("button", { name: "Branch: main" }).click();
  await page.getByRole("button", { name: "Merge episode-branch", exact: true }).click();
  await page.locator("dialog").getByRole("button", { name: "Merge", exact: true }).click();
  await page.getByRole("button", { name: "Merge anyway", exact: true }).click();
  assert.equal(await page.locator("dialog").count(), 0);
  await page.getByRole("button", { name: "Inbox", exact: true }).click();
  response.resolve();
  await page.getByRole("status").filter({ hasText: "Merge is running" }).waitFor();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].confirm_unfinished_jobs, true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});
