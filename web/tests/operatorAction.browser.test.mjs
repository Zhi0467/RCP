import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

/**
 * The operator who hit this stop for real could not tell that a command was
 * theirs to run, on which machine, or how to copy the two values GitHub wanted.
 * Each assertion below is one of those reports.
 */
test("a human stop is ordered, says where each command runs, and copies its values", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const { port } = server.httpServer.address();
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    await page.goto(`http://127.0.0.1:${port}/tests/fixtures/operatorAction.html`);
    await page.waitForSelector(".provisioning-operator-action");

    // The card is named for the human's task, not the machine check it paused.
    assert.equal(
      await page.locator(".provisioning-operator-action h2").innerText(),
      "Add a deploy key on GitHub",
    );

    // Commands span the card instead of wrapping down a quarter-width ribbon.
    const card = await page.locator(".provisioning-operator-action").boundingBox();
    for (const box of await page
      .locator(".operator-value")
      .evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().width))) {
      assert.ok(box > card.width * 0.6, `a value pane is only ${box}px of ${card.width}px`);
    }

    // Every command says which shell it belongs to, and how to reach it.
    const runOn = await page.locator(".operator-run-on .tag").allInnerTexts();
    assert.ok(runOn.length >= 2, `expected each command to name its shell, saw ${runOn.length}`);
    // innerText reflects the uppercase transform these tags are drawn with.
    assert.ok(
      runOn.every((text) => /^run on the server/i.test(text)),
      runOn.join(" | "),
    );
    assert.ok(
      (await page.locator(".operator-run-on code").first().innerText()).includes(
        "ssh operator@server.example",
      ),
    );

    // The stop is an ordered list: one entry per action, resuming last, and
    // each entry carries its own number rather than a filler heading.
    const steps = page.locator(".operator-steps > li");
    assert.ok((await steps.count()) >= 4, `expected numbered steps, saw ${await steps.count()}`);
    assert.equal(await steps.last().locator("header > span").innerText(), "Resume setup");
    const markers = await steps.evaluateAll((nodes) =>
      nodes.map((node) => window.getComputedStyle(node, "::before").content),
    );
    assert.ok(
      markers.every((value) => value && value !== "none"),
      `every step needs its number, saw ${markers.join(" | ")}`,
    );
    // Each step is named by the stop itself, not by a filler heading.
    const headings = await page.locator(".operator-steps > li > header > span").allInnerTexts();
    assert.deepEqual(headings, [
      "Add the key to GitHub",
      // The warning comes before the command that stops and waits for it.
      "Know the host key before you are asked to accept it",
      "Trust github.com from the server",
      "Resume setup",
    ]);

    // Two values are GitHub inputs with their own Copy button; the fingerprint
    // is only compared, so it is not offered as a third thing to paste.
    assert.equal(await page.locator(".operator-values label").count(), 2);
    assert.equal(await page.locator(".operator-evidence > div").count(), 1);
    assert.match(
      await page.locator(".operator-evidence dt").innerText(),
      /public key fingerprint/i,
    );
    assert.equal(await page.locator(".operator-evidence button").count(), 0);

    // Allow write access is its own requirement, not a trailing clause.
    assert.match(
      await page.locator(".operator-requirement").innerText(),
      /Enable Allow write access/,
    );

    // Purpose and expected success are behind one disclosure, not loose text.
    assert.equal(await page.locator(".operator-details[open]").count(), 0);
    await page.locator(".operator-details > summary").click();
    assert.equal(await page.locator(".operator-details[open]").count(), 1);

    // Each value GitHub asks for is copyable on its own.
    const label = await page.locator(".operator-values .operator-value").first().innerText();
    await page.locator('.operator-values button[aria-label^="Copy"]').first().click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), label);
    assert.equal(
      await page.locator('.operator-values button[aria-label^="Copy"]').first().innerText(),
      "Copied",
    );

    // Refresh sits with the resume command rather than elsewhere on the page.
    await page
      .locator(".operator-steps > li")
      .last()
      .getByRole("button", { name: "Refresh" })
      .click();
    assert.equal(await page.evaluate(() => document.body.dataset.refreshed), "yes");

    // A direct route signs in as the service account, which cannot then
    // elevate into itself, so it is not offered as the way into a command that
    // expects the operator's own login.
    await page.goto(`http://127.0.0.1:${port}/tests/fixtures/operatorAction.html?mode=direct_rcp`);
    await page.waitForSelector(".provisioning-operator-action");
    assert.ok((await page.locator(".operator-run-on .tag").count()) >= 2);
    assert.equal(await page.locator(".operator-run-on code").count(), 0);

    // A target holding shell metacharacters is quoted, not pasted raw: the
    // native validator rejects whitespace and slashes but not `;` or `$`, and
    // this string is copied straight into a shell.
    await page.goto(`http://127.0.0.1:${port}/tests/fixtures/operatorAction.html?mode=hostile`);
    await page.waitForSelector(".provisioning-operator-action");
    const hostile = await page.locator(".operator-run-on code").first().innerText();
    assert.equal(hostile, "ssh 'operator@host;echo${IFS}oops'");

    // A stop whose sole action is also its resume command is one step, not two.
    await page.goto(`http://127.0.0.1:${port}/tests/fixtures/operatorAction.html?mode=reentry`);
    await page.waitForSelector(".provisioning-operator-action");
    assert.equal(await page.locator(".operator-steps > li").count(), 1);
    assert.equal(
      await page.locator(".operator-steps > li header > span").innerText(),
      "Resume setup",
    );
    // Its shell is the service account, which the direct route does reach.
    assert.equal(
      await page.locator(".operator-run-on .tag").first().innerText(),
      "RUN ON THE SERVER AS RCP",
    );
    assert.equal(
      await page.locator(".operator-run-on code").first().innerText(),
      "ssh rcp@server.example",
    );

    // A route is saved before its probe runs, and a sudo route may name the
    // service account, so an unproved route is never offered as the way in.
    await page.goto(`http://127.0.0.1:${port}/tests/fixtures/operatorAction.html?mode=unproved`);
    await page.waitForSelector(".provisioning-operator-action");
    assert.ok((await page.locator(".operator-run-on .tag").count()) >= 2);
    assert.equal(await page.locator(".operator-run-on code").count(), 0);

    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    await server.close();
  }
});
