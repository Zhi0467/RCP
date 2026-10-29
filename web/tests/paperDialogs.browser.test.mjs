import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createPaperTestServer, openPaper, paperTestCard } from "./paperTestServer.mjs";

let server, browser;
before(async () => {
  server = await createPaperTestServer();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
async function open(t, width = 390, height = 600) {
  const { page } = await openPaper(t, {
    browser,
    origin: server.origin,
    width,
    height,
    cards: [
      {
        ...paperTestCard,
        kind: "rewrite",
        current: { start: 0, end: 5, revision: 1, text: "draft" },
      },
    ],
    proposedTexts: { c1: "revised" },
  });
  return page;
}

test("Paper dialog contains keyboard focus and restores the opener", async (t) => {
  const page = await open(t);
  const opener = page.getByRole("button", { name: "Paper history", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Paper history", exact: true });
  assert.ok(await dialog.evaluate((element) => element.matches("dialog:modal")));
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press(i < 4 ? "Tab" : "Shift+Tab");
    // Native modals may hand focus to browser chrome, never to the inert page.
    assert.ok(
      await dialog.evaluate(
        (element) =>
          element.contains(document.activeElement) || document.activeElement === document.body,
      ),
    );
  }
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  assert.ok(await opener.evaluate((element) => document.activeElement === element));
});

test("coach settings stay within a short phone pane and scroll to the last control", async (t) => {
  const page = await open(t);
  await page.getByRole("button", { name: "Coach", exact: true }).filter({ visible: true }).click();
  await page.getByRole("textbox", { name: "Message", exact: true }).evaluate((textarea) => {
    textarea.style.height = "180px";
  });
  const chip = page.getByRole("button", { name: /^Coach agent:/ });
  await chip.click();
  const popover = page.locator(".paper-agent-popover");
  const pane = await page.locator(".paper-coach").boundingBox();
  const bounds = await popover.boundingBox();
  assert.ok(bounds.y >= pane.y && bounds.y + bounds.height <= pane.y + pane.height);
  // The panel never covers the chip that opened it.
  assert.ok(bounds.y + bounds.height <= (await chip.boundingBox()).y);
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
  for (const field of await popover
    .locator("button:visible, input:visible, select:visible")
    .all()) {
    await field.scrollIntoViewIfNeeded();
    const control = await field.boundingBox();
    assert.ok(control.y >= bounds.y && control.y + control.height <= bounds.y + bounds.height + 1);
  }
  await page.keyboard.press("Escape");
  await popover.waitFor({ state: "detached" });
  await chip.click();
  await popover.waitFor();
  await chip.click();
  await popover.waitFor({ state: "detached" });
});

test("Paper phone icon actions retain 44px touch targets", async (t) => {
  const page = await open(t);
  for (const panel of ["Write", "Coach"]) {
    await page.getByRole("button", { name: panel, exact: true }).first().click();
    if (panel === "Write") await page.getByRole("button", { name: "Next suggestion" }).waitFor();
    const sizes = await page
      .locator(".paper-live-workspace .icon-button:visible")
      .evaluateAll((buttons) =>
        buttons.map((button) => {
          const { width, height } = button.getBoundingClientRect();
          return { width, height };
        }),
      );
    assert.ok(sizes.length > 0);
    assert.ok(sizes.every(({ width, height }) => width >= 44 && height >= 44));
  }
});

test("a held legacy paper keeps its version choice modal until one is chosen", async (t) => {
  const { page, posts } = await openPaper(t, {
    browser,
    origin: server.origin,
    width: 390,
    configure: ({ tree }) => {
      tree.hold = "legacy";
    },
  });
  const dialog = page.getByRole("alertdialog", { name: "Choose Paper version" });
  assert.ok(await dialog.evaluate((element) => element.matches("dialog:modal")));
  await page.keyboard.press("Escape");
  await page.mouse.click(2, 2);
  assert.ok(await dialog.isVisible());
  await dialog.getByRole("button", { name: "Keep draft" }).click();
  await dialog.waitFor({ state: "detached" });
  assert.deepEqual(posts.at(-1), {
    path: "/api/projects/p/paper/hold/resolve",
    body: { keep: "draft" },
  });
});
