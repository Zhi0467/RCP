import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { createServer } from "vite";

const main = {
  kind: "main",
  branch_id: null,
  name: "main",
  archived: false,
  head: { revision: 12 },
};
const branch = {
  kind: "branch",
  branch_id: "existing",
  name: "Existing branch",
  archived: false,
  head: { revision: 4 },
};

for (const width of [1000, 390]) {
  test(`Branch this chat creates an idle chat and navigates at ${width}px`, async () => {
    const cacheDir = await mkdtemp(join(tmpdir(), "rcp-chat-branch-"));
    const server = await createServer({
      configLoader: "runner",
      cacheDir,
      root: new URL("..", import.meta.url).pathname,
      logLevel: "silent",
      server: { host: "127.0.0.1", port: 0 },
    });
    let browser;
    try {
      await server.listen();
      browser = await chromium.launch({ headless: true });
      const page = await browser.newPage({ viewport: { width, height: 850 } });
      const errors = [];
      const requests = [];
      page.on("pageerror", (error) => errors.push(error.message));
      let result;
      await page.route("**/api/**", async (route) => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        const body = request.method() === "POST" ? request.postDataJSON() : null;
        requests.push({ method: request.method(), path, body });
        let data = {};
        if (path.endsWith("/graph-refs"))
          data = body
            ? { ...branch, branch_id: "new-branch", name: body.name }
            : [
                main,
                branch,
                { ...branch, branch_id: "archived", name: "Archived", archived: true },
              ];
        if (path.endsWith("/source/branch")) {
          result = {
            chat_id: "forked",
            title: "Source chat",
            graph_title: width === 390 ? "Existing branch" : "New direction",
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
            },
          };
          data = result;
        }
        if (path.endsWith("/chats/forked")) data = result;
        if (path.endsWith("/questions")) data = [];
        if (path.endsWith("/browser")) data = { enabled: false };
        await route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
      });
      const address = server.httpServer.address();
      await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/chatBranch.html`);
      await page.getByRole("button", { name: "Branch this chat", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "Branch this chat" });
      await dialog.getByRole("button", { name: "Branch chat", exact: true }).waitFor();
      await page.waitForFunction(
        () => !document.querySelector('.chat-branch-dialog button[type="submit"]').disabled,
      );
      assert.equal(await dialog.locator("option", { hasText: "Archived" }).count(), 0);
      assert.equal(await dialog.locator('option[value="main"]').count(), 0);
      if (width === 390) await dialog.getByLabel("Branch target").selectOption("existing");
      else await dialog.getByLabel("Branch name").fill("New direction");
      const bounds = await dialog.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width);
      await dialog.getByRole("button", { name: "Branch chat", exact: true }).click();
      await page.getByRole("link", { name: "Source chat", exact: true }).waitFor();
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
        assert.equal(forks[0].body.name, "New direction");
      }
      assert.deepEqual(requests.find((request) => request.path.endsWith("/source/branch")).body, {
        graph_target: result.graph_target,
      });
      assert.equal(
        await page.getByRole("textbox", { name: "Message", exact: true }).inputValue(),
        "",
      );
      await page.getByRole("textbox", { name: "Message", exact: true }).fill("Continue here");
      await page.getByRole("button", { name: "Start Discuss turn", exact: true }).click();
      await page.getByRole("button", { name: "Resend", exact: true }).waitFor();
      const sent = requests.find((request) => request.path === "/api/start").body;
      assert.equal(sent.provider, "codex");
      assert.equal(sent.model, "source-model");
      assert.equal(sent.reasoning, "high");
      assert.equal(sent.run_on, "local");
      assert.ok(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      );
      await page.getByRole("link", { name: "Source chat", exact: true }).click();
      await page.getByText("A retained answer", { exact: true }).waitFor();
      assert.deepEqual(errors, []);
    } finally {
      await browser?.close();
      await server.close();
      await rm(cacheDir, { recursive: true, force: true });
    }
  });
}
