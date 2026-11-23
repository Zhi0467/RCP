import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { chromium } from "playwright";

const page = readFileSync(new URL("../../pages/open.html", import.meta.url), "utf8");
const space = "9b2f6c1e-1d2a-4c3b-8e4f-5a6b7c8d9e0f";

for (const [name, agent] of [
  ["a Mac hands the link to the app", "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0)"],
  ["a phone offers both ways", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"],
]) {
  test(`the public share page: ${name}`, async () => {
    const browser = await chromium.launch();
    try {
      const context = await browser.newContext({
        userAgent: agent,
        viewport: { width: 390, height: 700 },
      });
      const tab = await context.newPage();
      const errors = [];
      tab.on("console", (message) => message.type() === "error" && errors.push(message.text()));
      await tab.route("https://pages.example/**", (route) =>
        route.fulfill({ contentType: "text/html", body: page }),
      );
      await tab.goto(`https://pages.example/RCP/open#s=${space}&h=rcp.lab.example&l=tok_9-x`, {
        waitUntil: "commit",
      });
      const app = tab.getByRole("link", { name: "Open in RCP" });
      await app.waitFor();
      assert.equal(
        await app.getAttribute("href"),
        `rcp://open?space=${space}&host=rcp.lab.example&l=tok_9-x`,
      );
      assert.equal(
        await tab.getByRole("link", { name: "Open on the team server" }).getAttribute("href"),
        "https://rcp.lab.example/open#l=tok_9-x",
      );
      assert.deepEqual(errors, []);
      const broken = await context.newPage();
      await broken.route("https://pages.example/**", (route) =>
        route.fulfill({ contentType: "text/html", body: page }),
      );
      await broken.goto(`https://pages.example/RCP/open#s=${space}&h=evil.example/x&l=tok`);
      await broken.getByText("This link is incomplete", { exact: false }).waitFor();
      assert.equal(await broken.getByRole("link", { name: "Open in RCP" }).isVisible(), false);
    } finally {
      await browser.close();
    }
  });
}
