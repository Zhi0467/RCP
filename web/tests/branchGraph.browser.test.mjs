import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("branch graph keeps canvas editing and chat while removed nodes are historical", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({
      reducedMotion: "reduce",
      viewport: { width: 1400, height: 1000 },
    });
    const errors = [];
    const requests = [];
    const worktreeRequests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/projects/branch-fixture/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("graph-edit-options"))
        return route.fulfill({
          json: {
            node_prefixes: {
              research_question: "rq",
              hypothesis: "hyp",
              experiment: "exp",
              evidence: "ev",
              decision: "dec",
              blocker: "blk",
            },
            relations: [{ name: "relates_to", assessment_required_for: [] }],
          },
        });
      if (url.pathname.endsWith("/tasks/node_chat")) {
        requests.push({ url: route.request().url(), body: route.request().postDataJSON() });
        return route.fulfill({
          json: {
            operation_id: `task-${requests.length}`,
            project_id: "branch-fixture",
            kind: "node_chat",
            request: requests.at(-1).body,
            graph_target: { kind: "branch", branch_id: "episode-branch" },
            status: "succeeded",
            active: false,
            settled: true,
            finished: true,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            result: { answers: [] },
          },
        });
      }
      if (url.pathname.endsWith("/worktree")) {
        worktreeRequests.push(url);
        return route.fulfill({
          json: {
            show_chooser: false,
            can_choose: false,
            binding: null,
            integration_options: [],
            can_remove: false,
          },
        });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/branchGraph.html`,
    );
    await page.getByRole("button", { name: "Layout", exact: true }).click();
    await page.getByRole("button", { name: "Research flow", exact: true }).click();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator('.dag-node[data-node-id="hyp/distant"]').count(), 0);
    assert.equal(await page.locator(".dag-edit-controls").count(), 1);
    const handle = (id) => page.locator(`.dag-node[data-node-id="${id}"] .dag-connect-handle`);
    assert.equal(await handle("hyp/changed").count(), 1);
    assert.equal(await handle("hyp/removed").count(), 0);
    await page.getByRole("button", { name: "Expand context", exact: true }).click();
    await page.locator('.dag-node[data-node-id="hyp/distant"]').waitFor();
    await page.getByRole("button", { name: "Changes + context", exact: true }).click();
    await page.locator('[data-node-id="hyp/new"] .dag-node-inspect').click();
    const detail = page.locator(".branch-change-detail");
    await detail.waitFor();
    assert.equal(await detail.locator(".branch-change-fields").count(), 0);
    assert.equal(await detail.locator(":scope > .eyebrow").count(), 1);
    await page.getByRole("button", { name: "Close detail", exact: true }).click();
    await page.locator('[data-node-id="hyp/removed"] .dag-node-inspect').click();
    assert.equal(
      await page.getByRole("button", { name: "Edit node", exact: true }).isDisabled(),
      true,
    );
    assert.equal(
      await page.getByRole("button", { name: "Ask about this node", exact: true }).isDisabled(),
      true,
    );
    assert.ok(
      await page.getByLabel("Change history").getByText("Removed duplicate claim").isVisible(),
    );
    await page.getByRole("button", { name: "Close detail", exact: true }).click();
    await page.locator('[data-node-id="hyp/changed"] .dag-node-inspect').click();
    assert.ok(
      await page
        .getByLabel("Branch changes")
        .getByText("Original claim", { exact: true })
        .first()
        .isVisible(),
    );
    assert.equal(
      await page.getByRole("button", { name: "Edit node", exact: true }).isDisabled(),
      false,
    );
    await page.getByLabel("Branch changes").getByRole("button", { name: "View task" }).click();
    assert.equal(await page.getByLabel("Inspected task").textContent(), "worker-task");
    await page.getByRole("button", { name: "Edit node", exact: true }).click();
    await page.getByLabel("Title", { exact: true }).fill("Human review title");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    const staged = JSON.parse(await page.getByLabel("Staged request").textContent());
    assert.equal(staged.nodes[0].changes.title, "Human review title");
    await page.getByRole("button", { name: "Ask about this node", exact: true }).click();
    await page.getByRole("textbox", { name: /message/i }).fill("Why did this claim change?");
    await page.getByRole("button", { name: "Start Discuss turn", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("textarea")?.value === "");
    assert.equal(requests[0].body.mode, "discuss");
    assert.equal(new URL(requests[0].url).searchParams.get("branch_id"), "episode-branch");
    await page.getByRole("button", { name: "Work", exact: true }).click();
    await page
      .getByRole("textbox", { name: /message/i })
      .fill("Refine this claim using the result.");
    await page.getByRole("button", { name: "Start Work turn", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("textarea")?.value === "");
    assert.equal(requests[1].body.mode, "work");
    assert.equal(requests[1].body.chat_id, requests[0].body.chat_id);
    assert.equal(requests[1].body.chat_id, "ordinary-review-chat");
    assert.ok(worktreeRequests.length > 0);
    assert.ok(
      worktreeRequests.every((url) => url.searchParams.get("branch_id") === "episode-branch"),
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
