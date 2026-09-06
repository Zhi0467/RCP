import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("live steering preserves the next-turn draft and renders stored receipts without retry", async () => {
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
    const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let requests = [];
    let outcome = "delivered";
    let receipt;
    await page.route("**/api/projects/project/tasks/task/steer", async (route) => {
      const request = route.request().postDataJSON();
      requests.push(request);
      if (outcome === "disconnect") return route.abort("connectionfailed");
      receipt = {
        message_id: request.message_id,
        operation_id: "task",
        role: "user",
        text: request.message,
        timestamp: "2026-09-05T12:00:05Z",
        mode: "discuss",
        trigger: "human",
        attachments: [],
        steering: {
          attempt: request.attempt,
          turn_id: request.expected_turn_id,
          status: outcome,
          label: outcome === "delivered" ? "Delivered" : "Refused",
          reason: outcome === "delivered" ? null : "Completed before delivery",
        },
      };
      await route.fulfill({ json: receipt });
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/liveSteering.html`);
    const draft = page.getByRole("textbox", { name: "Message", exact: true });
    const steer = page.getByRole("textbox", { name: "Steer running turn" });
    await draft.fill("Keep this for my next turn");
    await steer.fill("Use a shorter explanation.");
    await page.getByRole("button", { name: "Send steer", exact: true }).click();
    await page.getByText("Delivered", { exact: true }).waitFor();
    assert.equal(requests.length, 1);
    assert.match(requests[0].message_id, /^[0-9a-f-]{36}$/);
    assert.deepEqual(
      { ...requests[0], message_id: "UUID" },
      {
        message_id: "UUID",
        attempt: 2,
        expected_turn_id: "provider-turn",
        message: "Use a shorter explanation.",
      },
    );
    assert.equal(await draft.inputValue(), "Keep this for my next turn");
    assert.equal(await page.getByText("Original prompt", { exact: true }).count(), 1);
    assert.equal(await page.getByText("Use a shorter explanation.", { exact: true }).count(), 1);

    // Reopening uses the canonical message and must retain the same receipt.
    await page.evaluate((message) => window.reopenSteeringFixture([message]), receipt);
    await page.getByText("Delivered", { exact: true }).waitFor();
    assert.equal(await draft.inputValue(), "Keep this for my next turn");
    assert.equal(requests.length, 1);

    outcome = "refused";
    await steer.fill("A steer racing completion");
    await page.getByRole("button", { name: "Send steer", exact: true }).click();
    await page.getByText("Completed before delivery", { exact: true }).waitFor();
    assert.equal(requests.length, 2);
    assert.notEqual(requests[0].message_id, requests[1].message_id);

    outcome = "disconnect";
    await steer.fill("A steer with a lost response");
    await page.getByRole("button", { name: "Send steer", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "Nothing was resent" }).waitFor();
    assert.equal(requests.length, 3);
    assert.equal(await steer.inputValue(), "A steer with a lost response");
    assert.equal(await draft.inputValue(), "Keep this for my next turn");

    // The recorded runtime can be exec after a profile's app-server fallback.
    await page.evaluate(() =>
      window.setSteeringFixture({
        can_steer: false,
        steer_unavailable_reason: "Codex exec has no live input channel.",
        steer_turn_id: null,
        runtime_id: "exec",
      }),
    );
    await page.getByText("Codex exec has no live input channel.", { exact: true }).waitFor();
    assert.equal(await steer.isDisabled(), true);
    assert.equal(
      await page.getByRole("button", { name: "Send steer", exact: true }).isDisabled(),
      true,
    );
    assert.equal(await draft.inputValue(), "Keep this for my next turn");

    await page.evaluate(() => window.setSteeringFixture({ steer_visible: false }));
    await page.getByRole("form", { name: "Steer running turn" }).waitFor({ state: "detached" });
    assert.equal(requests.length, 3);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
