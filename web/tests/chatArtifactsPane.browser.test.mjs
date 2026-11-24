import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

const artifacts = [
  ["report", "Thinking-only steering report", "text/html", "2026-09-05T14:02:00Z"],
  ["plot", "thinking-tokens.png", "image/png", "2026-09-05T14:01:00Z"],
  ["table", "arm-results.csv", "text/csv", "2026-09-05T13:58:00Z"],
  ["notes", "Steering design notes", "text/markdown", "2026-09-05T11:40:00Z"],
].map(([artifact_id, name, media_type, created_at]) => ({
  artifact_id,
  name,
  media_type,
  view: "document",
  supplier: "turn",
  origin_operation_id: "turn",
  worker_label: null,
  created_at,
}));

const shots = process.env.RCP_PANE_SHOTS;
if (shots) mkdirSync(shots, { recursive: true });

async function withPage(width, chat, run) {
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
    const page = await browser.newPage({ viewport: { width, height: 760 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const requested = [];
    await page.route("**/api/projects/project/chats/*/artifact-list", (route) => {
      requested.push(new URL(route.request().url()).pathname);
      return route.fulfill({
        json: route.request().url().includes("/chats/chat/") ? artifacts : [],
      });
    });
    await page.goto(
      `http://127.0.0.1:${address.port}/tests/fixtures/chatArtifactsPane.html?chat=${chat}`,
    );
    await page.getByRole("textbox", { name: "Message", exact: true }).waitFor();
    await run(page, requested);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
}

async function openPane(page, width) {
  const chip = page.getByRole("button", { name: "Artifacts", exact: true });
  await chip.waitFor();
  assert.equal((await chip.textContent()).trim(), String(artifacts.length));
  assert.equal(await chip.getAttribute("aria-pressed"), "false");
  const header = page.locator(".chat-dock > header");
  const closedHeight = await header.evaluate((element) => element.offsetHeight);
  await chip.click();
  assert.equal(await chip.getAttribute("aria-pressed"), "true");
  const pane = page.getByRole("complementary", { name: "Artifacts" });
  const rows = pane.getByRole("listitem");
  await page.waitForFunction(
    () => getComputedStyle(document.querySelector(".chat-artifacts-pane")).opacity === "1",
  );
  assert.equal(await rows.count(), artifacts.length);
  assert.deepEqual(
    await rows.allTextContents(),
    artifacts.map((artifact) => artifact.name),
  );
  assert.equal(await header.evaluate((element) => element.offsetHeight), closedHeight);
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth),
    width,
    "the page never scrolls sideways",
  );
  if (shots) await page.screenshot({ path: `${shots}/chat-artifacts-pane-${width}.png` });
  return { chip, pane, rows };
}

test("the pane lists the chat's artifacts beside the transcript at 1040px", async () => {
  await withPage(1040, "chat", async (page) => {
    const { pane, rows } = await openPane(page, 1040);
    const box = await pane.boundingBox();
    assert.equal(Math.round(box.width), 300);
    const lines = await page.locator(".agent-chat-view-lines").boundingBox();
    assert.ok(lines.x + lines.width <= box.x + 1, "the transcript sits beside the pane");
    await rows.nth(2).getByRole("button").click();
    assert.deepEqual(await page.evaluate(() => window.opened), ["table"]);
    assert.equal(await pane.evaluate((element) => element.classList.contains("is-open")), true);
    await pane.getByRole("button", { name: "Close", exact: true }).click();
    await pane.waitFor({ state: "hidden" });
  });
});

test("at 390px the pane covers the chat and a pick closes it", async () => {
  await withPage(390, "chat", async (page) => {
    const { chip, pane, rows } = await openPane(page, 390);
    const box = await pane.boundingBox();
    assert.equal(Math.round(box.x), 0);
    assert.equal(Math.round(box.width), 390);
    await rows.first().getByRole("button").click();
    assert.deepEqual(await page.evaluate(() => window.opened), ["report"]);
    await pane.waitFor({ state: "hidden" });
    assert.equal(await chip.getAttribute("aria-pressed"), "false");
  });
});

test("a chat without artifacts shows no chip", async () => {
  await withPage(1040, "empty", async (page, requested) => {
    await page.waitForFunction(() => document.querySelector(".chat-dock"));
    await page.waitForTimeout(300);
    assert.deepEqual(requested, ["/api/projects/project/chats/empty/artifact-list"]);
    assert.equal(await page.getByRole("button", { name: "Artifacts", exact: true }).count(), 0);
  });
});
