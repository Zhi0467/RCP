import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("feedback opens inside the phone project menu and continues to a prefilled issue", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.route("**/api/health", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ version: "0.4.54", build: 7, space_kind: "team" }),
      }),
    );
    const base = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures`;
    await page.goto(`${base}/feedbackMenu.html`);
    await page.getByRole("button", { name: "Project menu" }).click();
    await page.getByRole("button", { name: "Send feedback" }).click();

    const composer = page.getByRole("dialog", { name: "Send feedback" });
    await composer.waitFor();
    const box = await composer.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390);
    assert.equal(await page.locator("#project-menu").isHidden(), false);

    const send = page.getByRole("link", { name: "Continue on GitHub" });
    assert.equal(await send.getAttribute("aria-disabled"), "true");
    await page.getByRole("textbox", { name: "Feedback" }).fill("Figures vanish in dark mode");
    await page.waitForFunction(() =>
      document.querySelector(".feedback-continue")?.getAttribute("href")?.includes("build+7"),
    );
    const href = new URL(await send.getAttribute("href"));
    assert.equal(href.pathname, "/Zhi0467/RCP/issues/new");
    assert.equal(href.searchParams.get("title"), "Figures vanish in dark mode");

    await page
      .context()
      .route("https://github.com/**", (route) =>
        route.fulfill({ contentType: "text/html", body: "" }),
      );
    const [issue] = await Promise.all([page.context().waitForEvent("page"), send.click()]);
    await issue.waitForLoadState();
    assert.equal(new URL(issue.url()).searchParams.get("title"), "Figures vanish in dark mode");
  } finally {
    await browser?.close();
    await server.close();
  }
});
