import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import test from "node:test";
import {
  ago,
  message,
  transcript,
  settled,
  mockApi,
  withFixture,
} from "./fixtures/agentThreadsApi.mjs";

const shots = process.env.RCP_LIMIT_SHOTS;

for (const width of [1440, 390]) {
  test(`limit setting and decline round-trip across chats (${width}px)`, async () => {
    await withFixture(async (browser, base) => {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await mockApi(page, []);
      let enabled = true;
      let declined = false;
      let estimated = false;
      const writes = [];
      const reset = new Date(Date.now() + 3600_000).toISOString();
      await page.route("**/api/providers/limit-resume", (route) => {
        if (route.request().method() === "PUT") {
          enabled = route.request().postDataJSON().enabled;
          writes.push(enabled);
        }
        return route.fulfill({ json: { enabled } });
      });
      await page.route("**/api/agent-threads/*/transcript", (route) =>
        route.fulfill({
          json: transcript("space", [message("l1", "user", "Run the ablation", "op-limit")]),
        }),
      );
      await page.route("**/api/agent-threads/*/tasks/op-limit", (route) =>
        route.fulfill({
          json: {
            ...settled("op-limit"),
            kind: "project_chat",
            created_at: ago(1),
            failed: true,
            can_resume: true,
            can_retry: true,
            provider_label: "Codex",
            error: "Provider quota exhausted",
            status_message: "Provider quota exhausted",
            limit_reset: {
              resets_at: reset,
              estimated,
              state: enabled && !declined ? "waiting" : "stopped",
              reason: declined ? "declined" : enabled ? null : "switch_off",
            },
          },
        }),
      );
      const declines = [];
      await page.route("**/api/agent-threads/*/tasks/op-limit/limit-resume/decline", (route) => {
        declines.push([route.request().method(), new URL(route.request().url()).pathname]);
        declined = true;
        return route.fulfill({ status: 202, json: { ok: true } });
      });
      await page.goto(`${base}#/agent/space`);
      const row = page.locator("[data-limit-state]");
      await row.waitFor();
      assert.equal(await row.count(), 1);
      assert.equal(await page.locator(".chat-task-inline.paused").count(), 1);
      const menu = page.getByRole("button", { name: "Chat menu", exact: true });
      await menu.click();
      const toggle = page.getByRole("switch", { name: "Resume after usage limits" });
      await toggle.waitFor();
      await page.waitForFunction(
        () => !document.querySelector('[data-setting="limit-resume"]').disabled,
      );
      assert.equal(await toggle.isChecked(), true);
      await toggle.uncheck();
      await page.locator('[data-limit-state="stopped"]').waitFor();
      await menu.click();
      await page.goto(`${base}#/agent/moss`);
      await menu.click();
      await page.waitForFunction(
        () => !document.querySelector('[data-setting="limit-resume"]').disabled,
      );
      assert.equal(await toggle.isChecked(), false);
      await toggle.check();
      await page.locator('[data-limit-state="waiting"]').waitFor();
      await menu.click();
      const checkLayout = async () => {
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
          ),
          true,
        );
        for (const button of await row.getByRole("button").all())
          assert.equal(
            await button.evaluate((node) => getComputedStyle(node).whiteSpace),
            "nowrap",
          );
      };
      await checkLayout();
      if (shots) {
        mkdirSync(shots, { recursive: true });
        await page.screenshot({ path: `${shots}/limit-row-waiting-${width}.png`, fullPage: true });
      }
      await row.getByRole("button", { name: "Don't resume" }).click();
      await page.locator('[data-limit-state="stopped"]').waitFor();
      assert.deepEqual(declines, [
        ["POST", "/api/agent-threads/moss/tasks/op-limit/limit-resume/decline"],
      ]);
      assert.equal(await row.getByRole("button", { name: "Resume", exact: true }).count(), 1);
      assert.equal(await row.getByRole("button", { name: "Retry", exact: true }).count(), 1);
      assert.ok(await row.locator("span[title]").getAttribute("title"));
      assert.equal(await page.locator(".chat-task-inline.paused").count(), 1);
      await checkLayout();
      if (shots)
        await page.screenshot({ path: `${shots}/limit-row-stopped-${width}.png`, fullPage: true });
      declined = false;
      estimated = true;
      await page.reload();
      await page.locator('[data-limit-state="waiting"]').waitFor();
      assert.match(await row.textContent(), /Resumes later/);
      assert.deepEqual(writes, [false, true]);
      assert.deepEqual(errors, []);
    });
  });
}
