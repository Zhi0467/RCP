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
      await page.getByRole("button", { name: "Chat menu", exact: true }).click();
      await page.getByRole("button", { name: "Branch this chat", exact: true }).click();
      const picker = page.getByRole("dialog", { name: "Branch this chat" });
      await picker.locator('[data-branch-id="existing"]').waitFor();
      assert.equal(await picker.locator('[data-branch-id="archived"]').count(), 0);
      assert.equal(
        await picker.getByRole("button", { name: /^(Merge|Rename|Archive) / }).count(),
        0,
      );
      const bounds = await picker.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width);
      if (width === 390) {
        await picker.getByRole("button", { name: "existing", exact: true }).click();
      } else {
        await picker.getByRole("button", { name: "New branch", exact: true }).click();
        await picker.getByLabel("New branch name").fill("new-direction");
        await picker.getByRole("button", { name: "Create branch", exact: true }).click();
        await picker.getByRole("button", { name: "Retry new branch", exact: true }).click();
      }
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
