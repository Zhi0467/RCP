import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { createServer } from "vite";

let cacheDir;
let server;
let browser;
before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "rcp-chat-branch-"));
  server = await createServer({
    configLoader: "runner",
    cacheDir,
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
  await rm(cacheDir, { recursive: true, force: true });
});

/** A fresh context on the fixture; `handle` answers every API request. */
async function openFixture(t, viewport, handle) {
  const context = await browser.newContext({ viewport });
  t.after(() => context.close());
  const page = await context.newPage();
  await page.route("**/api/**", handle);
  await page.goto(
    `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/chatBranch.html`,
  );
  return page;
}

/** Answers graph refs with `refs`, questions with none, and anything else with `{}`. */
const refsOnly = (refs) => (route) => {
  const path = new URL(route.request().url()).pathname;
  const data = path.endsWith("/graph-refs") ? refs : path.endsWith("/questions") ? [] : {};
  return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
};

const isFocused = (locator) => locator.evaluate((node) => node === document.activeElement);

const main = {
  kind: "main",
  branch_id: null,
  name: "main",
  archived: false,
  head: { target: { kind: "main" }, revision: 12, transition_id: null },
};
const branch = {
  kind: "branch",
  branch_id: "existing",
  name: "existing",
  archived: false,
  head: { target: { kind: "branch", branch_id: "existing" }, revision: 4, transition_id: null },
};

for (const width of [1000, 390]) {
  test(`Branch this chat creates an idle chat and navigates at ${width}px`, async (t) => {
    const errors = [];
    const requests = [];
    let result;
    const page = await openFixture(t, { width, height: 850 }, async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const body = request.method() === "POST" ? request.postDataJSON() : null;
      requests.push({ method: request.method(), path, body });
      let data = {};
      if (path.endsWith("/graph-refs"))
        data = body
          ? { ...branch, branch_id: "new-branch", name: body.name }
          : [main, branch, { ...branch, branch_id: "archived", name: "Archived", archived: true }];
      if (path.endsWith("/source/branch") && !body) {
        await route.fulfill({ status: 204 });
        return;
      }
      if (path.endsWith("/source/branch")) {
        // The first response at desktop width is lost; the retry must reuse its id.
        if (
          width !== 390 &&
          requests.filter((item) => item.body && item.path === path).length === 1
        ) {
          await route.fulfill({ status: 503, body: JSON.stringify({ detail: "lost" }) });
          return;
        }
        result = {
          chat_id: "forked",
          title: "Source chat",
          graph_title: width === 390 ? "existing" : "new-direction",
          graph_target: body.graph_target,
          messages: [],
          origin: {
            source_chat_id: "source",
            source_operation_id: "source-turn",
            source_graph_target: { kind: "main" },
            source_title: "Source chat",
            provider: "codex",
            model: "source-model",
            reasoning: "high",
            run_on: "local",
            // At phone width the fork stands for one a restore detached.
            pending: width !== 390,
          },
        };
        data = result;
      }
      if (path.endsWith("/chats/forked")) data = result;
      if (path.endsWith("/questions")) data = [];
      if (path.endsWith("/browser")) data = { enabled: false };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.locator(".chat-header-menu > .icon-button").click();
    // The repository list stays inside the screen's 16px gutter.
    await page.locator(".chat-scope-control .scope-trigger").click();
    const scope = await page.locator(".scope-popover").boundingBox();
    assert.ok(scope.x >= 16 && scope.x + scope.width <= width - 16);
    await page.keyboard.press("Escape");
    await page.locator(".chat-branch-picker .branch-picker-trigger").click();
    const picker = page.locator(".branch-picker-popover");
    await picker.locator('[data-branch-id="existing"]').waitFor();
    assert.equal(await picker.locator('[data-branch-id="archived"]').count(), 0);
    assert.equal(await picker.getByRole("button", { name: /^(Merge|Rename|Archive) / }).count(), 0);
    const bounds = await picker.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width);
    if (width === 390) {
      await picker.locator('[data-branch-id="existing"] .branch-picker-name').click();
    } else {
      await picker.getByRole("button", { name: "New branch", exact: true }).click();
      await picker.getByLabel("New branch name").fill("new-direction");
      await picker.getByRole("button", { name: "Create branch", exact: true }).click();
      await picker.getByRole("button", { name: "Retry new branch", exact: true }).click();
    }
    const originLink = page.locator(".chat-origin-link");
    await originLink.waitFor();
    // A pending fork keeps its source's agent; a detached one picks its own.
    assert.equal(await page.locator(".agent-config-chip").isDisabled(), width !== 390);
    // The link starts the transcript's column, never left of the first message's edge.
    const column = await page.locator(".node-chat-lines").evaluate((lines) => {
      const style = getComputedStyle(lines);
      const inner =
        lines.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      return (
        lines.getBoundingClientRect().left +
        parseFloat(style.paddingLeft) +
        Math.max(0, (inner - 820) / 2)
      );
    });
    assert.ok(Math.abs((await originLink.boundingBox()).x - column) < 1);
    const hash = await page.evaluate(() => window.location.hash);
    assert.match(hash, /chat=forked/);
    assert.match(hash, width === 390 ? /branch_id=existing/ : /branch_id=new-branch/);
    assert.equal(requests.filter((request) => request.path === "/api/start").length, 0);
    const forks = requests.filter(
      (request) => request.method === "POST" && request.path.endsWith("/graph-refs"),
    );
    assert.equal(forks.length, width === 390 ? 0 : 1);
    if (forks.length) {
      assert.equal(forks[0].body.at, "main@12");
      assert.equal(forks[0].body.name, "new-direction");
    }
    const branched = requests.filter(
      (request) => request.method === "POST" && request.path.endsWith("/source/branch"),
    );
    // Only a new branch is preceded by the source check; it runs before the graph fork.
    const checked = requests.findIndex(
      (request) => request.method === "GET" && request.path.endsWith("/source/branch"),
    );
    assert.equal(checked >= 0, width !== 390);
    assert.equal(branched.length, width === 390 ? 1 : 2);
    assert.deepEqual(branched.at(-1).body.graph_target, result.graph_target);
    assert.equal(new Set(branched.map((request) => request.body.request_id)).size, 1);
    const message = page.getByRole("textbox", { name: "Message", exact: true });
    assert.equal(await message.inputValue(), "");
    await message.fill("Continue here");
    await page.getByRole("button", { name: "Start Discuss turn", exact: true }).click();
    await page.getByRole("button", { name: "Resend", exact: true }).waitFor();
    const sent = requests.find((request) => request.path === "/api/start").body;
    assert.equal(sent.provider, "codex");
    assert.equal(sent.model, "source-model");
    assert.equal(sent.reasoning, "high");
    assert.equal(sent.run_on, "local");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await originLink.click();
    await page.getByText("A retained answer", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
  });
}

for (const [width, height] of [
  [844, 390],
  [1280, 500],
]) {
  test(`Branch this chat stays inside a short ${width}x${height} window`, async (t) => {
    const branches = Array.from({ length: 12 }, (_, index) => ({
      ...branch,
      branch_id: `branch-${index}`,
      name: `branch-${index}`,
      head: { ...branch.head, target: { kind: "branch", branch_id: `branch-${index}` } },
    }));
    const page = await openFixture(t, { width, height }, refsOnly([main, ...branches]));
    await page.locator(".chat-header-menu > .icon-button").click();
    await page.locator(".chat-branch-picker .branch-picker-trigger").click();
    const picker = page.locator(".branch-picker-popover");
    await picker.locator('[data-branch-id="branch-11"]').waitFor();
    // The picker keeps the 16px gutter on every side; its list scrolls inside.
    const bounds = await picker.boundingBox();
    assert.ok(bounds.y >= 16 && bounds.y + bounds.height <= height - 16);
    assert.ok(bounds.x >= 16 && bounds.x + bounds.width <= width - 16);
    await picker.getByRole("button", { name: "New branch", exact: true }).click();
    await picker.getByLabel("New branch name").waitFor();
  });
}

test("Escape and presses inside Branch this chat close only the innermost layer", async (t) => {
  const page = await openFixture(t, { width: 1000, height: 850 }, refsOnly([main, branch]));
  const menu = page.locator(".chat-header-menu > .icon-button");
  await menu.click();
  await page.locator(".chat-branch-picker .branch-picker-trigger").click();
  const picker = page.locator(".branch-picker-popover");
  await picker.getByRole("button", { name: "New branch", exact: true }).click();
  await picker.getByLabel("New branch name").press("Escape");
  assert.equal(await picker.getByLabel("New branch name").count(), 0);
  assert.equal(await menu.getAttribute("aria-expanded"), "true");
  await picker.getByRole("button", { name: "New branch", exact: true }).waitFor();
  await picker.locator(".branch-picker-heading").click();
  assert.equal(await menu.getAttribute("aria-expanded"), "true");
  await picker.waitFor();
  await page.keyboard.press("Escape");
  await picker.waitFor({ state: "detached" });
  assert.equal(await menu.getAttribute("aria-expanded"), "true");
  await page.keyboard.press("Escape");
  assert.equal(await menu.getAttribute("aria-expanded"), "false");
});

test("Escape closes the repository list before the menu; the agent picker and Add menu close like popovers", async (t) => {
  const page = await openFixture(t, { width: 1000, height: 850 }, refsOnly([main, branch]));
  const menu = page.locator(".chat-header-menu > .icon-button");
  await menu.click();
  const scope = page.locator(".chat-scope-control .scope-trigger");
  await scope.click();
  await page.locator(".scope-popover").waitFor();
  await page.keyboard.press("Escape");
  await page.locator(".scope-popover").waitFor({ state: "detached" });
  assert.equal(await menu.getAttribute("aria-expanded"), "true");
  assert.equal(await isFocused(scope), true);
  await page.keyboard.press("Escape");
  assert.equal(await menu.getAttribute("aria-expanded"), "false");
  assert.equal(await isFocused(menu), true);

  const chip = page.locator(".agent-config-chip");
  const panel = page.locator(".agent-config");
  await chip.click();
  await panel.locator("select").first().focus();
  await page.keyboard.press("Escape");
  await panel.waitFor({ state: "detached" });
  assert.equal(await chip.getAttribute("aria-expanded"), "false");
  assert.equal(await isFocused(chip), true);
  await chip.click();
  await panel.waitFor();
  await page.mouse.click(5, 400);
  await panel.waitFor({ state: "detached" });
  await chip.click();
  await panel.waitFor();
  await chip.click();
  await panel.waitFor({ state: "detached" });

  // The composer's Add menu closes on Escape, an outside press, and a send.
  const add = page.getByRole("button", { name: "Add", exact: true });
  const addMenu = page.locator(".chat-add-menu");
  await add.click();
  await page.keyboard.press("Escape");
  await addMenu.waitFor({ state: "detached" });
  assert.equal(await isFocused(add), true);
  await add.click();
  await page.mouse.click(5, 400);
  await addMenu.waitFor({ state: "detached" });
  await add.click();
  await page.getByRole("textbox", { name: "Message", exact: true }).fill("Send closes it");
  await page.keyboard.press("Enter");
  await addMenu.waitFor({ state: "detached" });
});

test("The chat menu and Branch this chat work by keyboard; the current branch is not a choice", async (t) => {
  const branched = [];
  const answer = refsOnly([main, branch]);
  const page = await openFixture(t, { width: 1000, height: 850 }, (route) => {
    const request = route.request();
    if (request.url().endsWith("/source/branch") && request.method() === "POST") {
      branched.push(request.postDataJSON());
      return route.fulfill({ status: 503, body: JSON.stringify({ detail: "probe" }) });
    }
    return answer(route);
  });
  const menu = page.locator(".chat-header-menu > .icon-button");
  await menu.focus();
  await page.keyboard.press("Enter");
  // The first row takes focus; the arrows, Home, and End move between rows.
  const first = page.getByRole("menuitem").first();
  assert.equal(await isFocused(first), true);
  await page.keyboard.press("End");
  assert.equal(await isFocused(page.locator(".chat-scope-control .scope-trigger")), true);
  await page.keyboard.press("ArrowDown");
  assert.equal(await isFocused(first), true);
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("ArrowUp");
  assert.equal(await isFocused(page.locator(".chat-branch-picker .branch-picker-trigger")), true);
  await page.keyboard.press("Enter");
  const picker = page.locator(".branch-picker-popover");
  await picker.waitFor();
  // The chat is on main already: that row is inert, and focus starts on a choice.
  const current = picker.locator('[data-branch-id="main"] .branch-picker-name');
  assert.equal(await current.isDisabled(), true);
  const existing = picker.locator('[data-branch-id="existing"] .branch-picker-name');
  assert.equal(await isFocused(existing), true);
  const create = picker.getByRole("button", { name: "New branch", exact: true });
  // The arrows and Tab both cycle within the open picker.
  for (const key of ["ArrowDown", "ArrowDown", "Tab", "Tab", "Shift+Tab", "Shift+Tab"]) {
    const before = await isFocused(existing);
    await page.keyboard.press(key);
    assert.equal(await isFocused(before ? create : existing), true);
  }
  await page.keyboard.press("Enter");
  await picker.waitFor({ state: "detached" });
  await page.locator(".chat-branch-picker[data-failed]").waitFor();
  assert.deepEqual(
    branched.map((body) => body.graph_target),
    [{ kind: "branch", branch_id: "existing" }],
  );
});
