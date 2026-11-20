import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

for (const [actionLabel, receiptLabel] of [
  ["Steer running turn", "Delivered"],
  ["Send to the running turn", "Delivered"],
]) {
  test(`the composer renders ${actionLabel} and stored receipts without retry`, async () => {
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
      let gate = null;
      await page.route("**/api/projects/project/tasks/task/steer", async (route) => {
        const request = route.request().postDataJSON();
        requests.push(request);
        if (outcome === "disconnect") return route.abort("connectionfailed");
        if (gate) await gate.promise;
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
            label: outcome === "delivered" ? receiptLabel : "Refused",
            reason: outcome === "delivered" ? null : "Completed before delivery",
          },
        };
        await route.fulfill({ json: receipt });
      });
      await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/liveSteering.html`);
      await page.evaluate(
        (label) => window.setSteeringFixture({ steer_action_label: label }),
        actionLabel,
      );
      const composer = page.getByRole("textbox", { name: "Message", exact: true });
      const sendSteer = page.getByRole("button", { name: actionLabel, exact: true });
      const startTurn = page.getByRole("button", { name: "Send", exact: true });
      await sendSteer.waitFor();
      // The running turn owns the ordinary composer: no separate steering control.
      assert.equal(await page.getByRole("form", { name: "Steer running turn" }).count(), 0);
      assert.equal(await startTurn.count(), 0);

      assert.equal(await page.getByRole("group", { name: "Conversation mode" }).count(), 0);
      await composer.fill("Use a shorter explanation.");
      await sendSteer.click();
      await page.getByText(receiptLabel, { exact: true }).waitFor();
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
      assert.equal(await composer.inputValue(), "");
      assert.equal(await page.getByText("Original prompt", { exact: true }).count(), 1);
      assert.equal(await page.getByText("Use a shorter explanation.", { exact: true }).count(), 1);

      // Reopening uses the canonical message and must retain the same receipt.
      await page.evaluate((message) => window.reopenSteeringFixture([message]), receipt);
      await page.getByText(receiptLabel, { exact: true }).waitFor();
      assert.equal(requests.length, 1);

      outcome = "refused";
      await composer.fill("A steer racing completion");
      await sendSteer.click();
      await page.getByText("Completed before delivery", { exact: true }).waitFor();
      assert.equal(requests.length, 2);
      assert.notEqual(requests[0].message_id, requests[1].message_id);

      // While the receipt is awaited the composer is fenced, so the delivered text is
      // exactly what is consumed and no later selection can leak into the next turn.
      outcome = "delivered";
      let open;
      gate = { promise: new Promise((resolve) => (open = resolve)) };
      await composer.fill("A slow steer");
      await sendSteer.click();
      while (requests.length < 3) await new Promise((resolve) => setTimeout(resolve, 10));
      assert.equal(await composer.isDisabled(), true);
      assert.equal(await page.getByRole("button", { name: "Add", exact: true }).isDisabled(), true);
      gate = null;
      open();
      await page.getByText("A slow steer", { exact: true }).waitFor();
      assert.equal(requests.length, 3);
      await page.locator('textarea[aria-label="Message"]:enabled').waitFor();
      assert.equal(await composer.inputValue(), "");

      outcome = "disconnect";
      await composer.fill("A steer with a lost response");
      await sendSteer.click();
      await page.getByText("Nothing was resent").waitFor();
      assert.equal(requests.length, 4);
      assert.equal(await composer.inputValue(), "A steer with a lost response");

      // The recorded runtime can be exec after a profile's app-server fallback. The
      // disabled send button alone says sending is unavailable.
      await page.evaluate(() =>
        window.setSteeringFixture({
          can_steer: false,
          steer_unavailable_reason: "Codex exec has no live input channel.",
          steer_turn_id: null,
          runtime_id: "exec",
        }),
      );
      await startTurn.waitFor();
      assert.equal(await sendSteer.count(), 0);
      assert.equal(await startTurn.isDisabled(), true);
      await composer.press("Enter");
      assert.equal(requests.length, 4);
      assert.equal(await composer.inputValue(), "A steer with a lost response");

      await page.evaluate(() => window.setSteeringFixture({ steer_visible: false }));
      assert.equal(await sendSteer.count(), 0);
      assert.equal(await startTurn.isDisabled(), true);
      assert.equal(requests.length, 4);
      assert.deepEqual(errors, []);
    } finally {
      await browser?.close();
      await server.close();
    }
  });
}

for (const [kind, width] of [
  ["node_chat", 390],
  ["project_chat", 1000],
]) {
  test(`${kind} stops the addressed turn before sending its draft`, async () => {
    const server = await createServer({
      root: new URL("..", import.meta.url).pathname,
      logLevel: "silent",
      server: { host: "127.0.0.1", port: 0 },
    });
    let browser;
    try {
      await server.listen();
      browser = await chromium.launch();
      const page = await browser.newPage({ viewport: { width, height: 800 } });
      const starts = [];
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      let rejectPause = true;
      await page.route("**/api/**", (route) =>
        route.fulfill({
          json: route.request().url().includes("/worktree")
            ? { show_chooser: false, binding: null, integration_options: [] }
            : [],
        }),
      );
      await page.route("**/api/projects/project/tasks/task/pause", (route) =>
        route.fulfill(
          rejectPause
            ? { status: 409, json: { detail: "Could not stop" } }
            : { json: { operation_id: "task", active: true, pausing: true } },
        ),
      );
      await page.route(`**/api/projects/project/tasks/${kind}`, (route) => {
        starts.push(route.request().postDataJSON());
        return route.fulfill({ json: { operation_id: "next" } });
      });
      const url = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/liveSteering.html${kind === "node_chat" ? "?node" : ""}`;
      await page.goto(url);
      const composer = page.getByRole("textbox", { name: "Message" });
      const stop = page.getByRole("button", { name: "Stop and send", exact: true });
      const stopped = {
        active: false,
        paused: true,
        can_pause: false,
        can_steer: false,
        status_message: "Provider process paused.",
      };
      await composer.fill("Tell me where you stopped");
      await stop.click({ timeout: 5000 });
      await page.getByText("Could not stop", { exact: true }).waitFor();
      assert.equal(await composer.inputValue(), "Tell me where you stopped");
      assert.equal(starts.length, 0);

      rejectPause = false;
      await stop.click();
      assert.equal(starts.length, 0);
      await page.evaluate(() => window.setSteeringFixture({ operation_id: "newer" }));
      await page.evaluate((state) => window.setSteeringFixture(state), stopped);
      await page.getByRole("button", { name: "Send", exact: true }).waitFor();
      assert.equal(await composer.inputValue(), "Tell me where you stopped");
      assert.equal(starts.length, 0);

      await page.reload();
      await composer.fill("Keep this draft when I switch chats");
      await stop.click();
      await page.evaluate(() => window.reopenSteeringFixture([]));
      await page.evaluate((state) => window.setSteeringFixture(state), stopped);
      await page.getByRole("button", { name: "Send", exact: true }).waitFor();
      assert.equal(starts.length, 0);

      await page.reload();
      await page.evaluate(() => {
        window.setSteeringFixture({ parent_operation_id: "parent" });
        window.reopenSteeringFixture([
          {
            message_id: "original",
            operation_id: "parent",
            role: "user",
            text: "Original prompt",
            timestamp: "2026-09-05T11:59:00Z",
            mode: "discuss",
            trigger: "human",
            attachments: [],
          },
        ]);
      });
      await composer.fill("Stop here and explain");
      assert.equal(await page.getByText("Original prompt", { exact: true }).count(), 1);
      assert.equal(await stop.count(), 1);
      await stop.click();
      await Promise.all([
        page.waitForResponse((response) => response.url().endsWith("/tasks/" + kind)),
        page.evaluate((state) => window.setSteeringFixture(state), stopped),
      ]);
      assert.equal(starts.length, 1);
      assert.equal(starts[0].chat_id, "steering-chat");
      assert.equal(starts[0].session_id, "native-session");
      assert.equal(starts[0].message, "Stop here and explain");
      assert.equal(starts[0].node_id, kind === "node_chat" ? "test-node" : null);
      assert.deepEqual(errors, []);
    } finally {
      await browser?.close();
      await server.close();
    }
  });
}
