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

test("a settled sign-in stops contradicting the account state the row now shows", async () => {
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
      if (message.type() === "error") errors.push(message.text());
    });
    await page.route("**/api/providers/codex/logins/sign-in/login-1/cancel", (route) =>
      route.fulfill({
        json: {
          login_id: "login-1",
          provider: "codex",
          host: "",
          state: "failed",
          user_code: null,
          verification_url: null,
          detail: "The sign-in was canceled before it completed.",
          started_at: "2026-09-14T00:00:00Z",
        },
      }),
    );
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/providerLoginRow.html`,
    );
    await page.getByText("ABCD-1234", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Cancel sign-in" }).click();
    // Cancelling refreshes the account, so the row speaks for itself immediately.
    await page.waitForFunction(() => window.loginChanges >= 1);
    await page.getByText("Sign-in failed: The sign-in was canceled before it completed.").waitFor();
    // The account is signed in again; a settled sign-in must not survive to contradict it.
    await page.evaluate(() =>
      window.setLoginAccount({
        state: "signed_in",
        generation: 1,
        detail: "Authenticated request succeeded.",
        sign_in: null,
      }),
    );
    await page
      .getByText("Sign-in failed: The sign-in was canceled before it completed.")
      .waitFor({ state: "detached" });

    assert.equal(await page.locator(".provider-login-sign-in").count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});

test("a pasted token cannot be submitted through the recheck button", async () => {
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
      if (message.type() === "error") errors.push(message.text());
    });
    let verifies = 0;
    await page.route("**/api/providers/codex/logins/verify", (route) => {
      verifies += 1;
      return route.fulfill({ json: { state: { state: "signed_in" }, resumed: { checked: 0 } } });
    });
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/providerLoginRow.html`,
    );
    // Settle the fixture's running sign-in first; a pending one disables the row.
    await page.evaluate(() =>
      window.setLoginAccount({
        sign_in: {
          login_id: "login-2",
          provider: "codex",
          host: "",
          state: "failed",
          user_code: null,
          verification_url: null,
          detail: "ended",
          started_at: "2026-09-14T00:00:00Z",
        },
      }),
    );
    await page.evaluate(() =>
      window.setLoginAccount({
        sign_in: null,
        sign_in_methods: ["token_entry"],
        token_instructions: "Run the provider's token command, then paste it here.",
        token: {
          pasted_at: "2026-09-14T10:00:00Z",
          pasted_by: "member",
          verified_at: "2026-09-14T10:00:05Z",
          estimated_expiry_at: "2027-08-15T10:00:00Z",
        },
      }),
    );
    const recheck = page.getByRole("button", { name: "Verify sign-in" });
    await recheck.waitFor();
    assert.equal(await recheck.isEnabled(), true);
    // Typing a token makes the submit the only live action, so the recheck
    // cannot quietly test the old credential and report the paste as failed.
    await page.locator('input[type="password"]').fill("token-value");
    await page.waitForFunction(
      () =>
        document.querySelectorAll("button")[
          [...document.querySelectorAll("button")].findIndex(
            (b) => b.textContent.trim() === "Verify sign-in",
          )
        ].disabled === true,
    );
    assert.equal(
      await page.getByRole("button", { name: "Sign in", exact: true }).isEnabled(),
      true,
    );
    assert.equal(verifies, 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});
