import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("the phone project chrome keeps one bar on top and the panels at the bottom", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const base = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures`;
    for (const viewport of [
      { width: 390, height: 844 },
      { width: 360, height: 640 },
    ]) {
      const page = await browser.newPage({ viewport, reducedMotion: "reduce" });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${base}/phoneChrome.html`);
      const bar = page.locator(".phone-project-bar");
      const tabs = page.locator(".phone-tab-bar");
      await bar.waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), viewport.width);
      assert.ok((await bar.boundingBox()).height <= 56, "The project chrome is one bar");

      // Both bars stay on screen while the panel scrolls under them.
      await page.mouse.wheel(0, 2000);
      await page.waitForFunction(() => window.scrollY > 0);
      assert.equal(Math.round((await bar.boundingBox()).y), 0);
      const tabBox = await tabs.boundingBox();
      assert.equal(Math.round(tabBox.y + tabBox.height), viewport.height);

      // A long project name truncates instead of pushing Sync or the menu away.
      const more = page.getByRole("button", { name: "Project menu" });
      const moreBox = await more.boundingBox();
      assert.ok(moreBox.x + moreBox.width <= viewport.width);
      await page.getByRole("button", { name: "Sync" }).waitFor();

      // Many open projects scroll inside the dock, which never runs off the
      // screen: the Agents shell clips the page, so page scroll cannot reach it.
      await page.locator(".phone-bar-name").click();
      const dockBox = await page.locator("#phone-project-dock").boundingBox();
      assert.ok(dockBox.y + dockBox.height <= viewport.height, "The dock fits on screen");
      const last = page.getByRole("button", { name: "Project 29", exact: true });
      await last.scrollIntoViewIfNeeded();
      const lastBox = await last.boundingBox();
      assert.ok(lastBox.y >= 0 && lastBox.y + lastBox.height <= viewport.height);
      await last.click();
      assert.equal(await page.locator("#phone-project-dock").count(), 0);

      // The menu names each control by its label and closes when one is chosen.
      await more.click();
      const history = page.getByRole("button", { name: "Project history" });
      assert.match(
        await history.evaluate((element) => getComputedStyle(element, "::after").content),
        /Project history/,
      );
      // Research's graph filter, hidden with the tab strip, is reachable here.
      await page.getByRole("combobox").selectOption("review");
      assert.equal(await page.locator("#phone-project-menu").isHidden(), false);
      await history.click();
      assert.equal(await page.locator("#phone-project-menu").isHidden(), true);
      await more.click();
      await page.keyboard.press("Escape");
      assert.equal(await page.locator("#phone-project-menu").isHidden(), true);

      // A panel in More is one tap away, and More reads as active while it is open.
      const moreTab = tabs.getByRole("button", { name: "More" });
      await moreTab.click();
      await page.locator("#phone-more-panels").getByRole("button", { name: "Settings" }).click();
      assert.equal(await page.locator("#phone-more-panels").isHidden(), true);
      assert.match(await moreTab.getAttribute("class"), /active/);

      // Ask floats above the tab bar rather than under it.
      const ask = await page.getByRole("button", { name: "Ask about this project" }).boundingBox();
      assert.ok(ask.y + ask.height <= (await tabs.boundingBox()).y);

      assert.deepEqual(await page.evaluate(() => window.chosen), [
        "project-29",
        "history",
        "settings",
      ]);
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser?.close();
    await server.close();
  }
});
