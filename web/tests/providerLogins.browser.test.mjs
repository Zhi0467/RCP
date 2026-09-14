import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { chromium } from "playwright";

test("a pending sign-in keeps polling past a transient status failure", async () => {
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0, hmr: false },
  });
  let browser;
  try {
    await liveServer.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("503"))
        errors.push(message.text());
    });
    let polls = 0;
    await page.route("**/api/providers/codex/logins/sign-in/login-1", (route) => {
      polls += 1;
      assert.equal(route.request().method(), "GET");
      return route.fulfill(
        polls === 1
          ? { status: 503, json: { detail: "Sign-in status unavailable" } }
          : {
              json: {
                login_id: "login-1",
                provider: "codex",
                host: "",
                state: "succeeded",
                user_code: null,
                verification_url: null,
                detail: null,
                started_at: "2026-09-14T00:00:00Z",
              },
            },
      );
    });
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/providerLoginRow.html`,
    );
    const signIn = page.getByRole("button", { name: "Sign in with device code" });
    assert.equal(await signIn.isDisabled(), true);
    await page.getByText("Sign-in status unavailable", { exact: true }).waitFor();
    // The failed poll disables nothing for good: the next poll lands and frees the row.
    await page.waitForFunction(() => window.loginChanges === 1);
    await page.getByRole("button", { name: "Sign in with device code" }).waitFor();
    assert.equal(await signIn.isDisabled(), false);
    assert.equal(polls, 2);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});
