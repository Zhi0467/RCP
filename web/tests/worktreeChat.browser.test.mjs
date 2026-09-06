import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

const unbound = {
  show_chooser: true,
  can_choose: true,
  unavailable_reason: null,
  binding: null,
  integration_options: [],
  can_remove: false,
  remove_reason: null,
  ahead_count: null,
  remote_branch_exists: null,
  remote_branch_evidence: null,
  dirty_worktree: [],
};
const bound = {
  ...unbound,
  show_chooser: false,
  can_choose: false,
  can_remove: true,
  binding: {
    project_id: "project",
    chat_scope: "project",
    node_id: null,
    repository_alias: "repo",
    machine: "local",
    execution_host: "",
    shared_path: "/repo",
    worktree_path: "/repo-worktree",
    git_common_dir: "/repo/.git",
    branch: "rcp/chat-one",
    starting_branch: "develop",
    starting_commit: "abc",
    chat_id: "worktree-chat",
    status: "ready",
  },
  ahead_count: 2,
  integration_options: [
    {
      id: "pull_request",
      label: "Open a pull request",
      enabled: true,
      reason: null,
      target_branch: null,
    },
    {
      id: "starting_branch",
      label: "Merge into develop",
      enabled: false,
      reason: "Shared checkout has uncommitted changes: local.txt",
      target_branch: "develop",
    },
    {
      id: "default_branch",
      label: "Merge into release",
      enabled: true,
      reason: null,
      target_branch: "release",
    },
  ],
};

test("composer binds a worktree and dispatches integration through ordinary Work with explicit removal", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    let projection = unbound;
    let removals = 0;
    let removalPreviews = 0;
    await page.route("**/api/projects/project/chats/*/worktree**", async (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() === "GET") {
        assert.equal(url.searchParams.get("run_on"), "local");
        assert.deepEqual(url.searchParams.getAll("run_truth_scope"), ["repo"]);
        assert.equal(url.searchParams.get("chat_scope"), "project");
        if (url.searchParams.get("inspect_removal") === "true") {
          removalPreviews += 1;
          await route.fulfill({
            json: {
              ...projection,
              remote_branch_exists: false,
              remote_branch_evidence: "Checked origin using git ls-remote.",
            },
          });
          return;
        }
      } else {
        assert.equal(route.request().method(), "DELETE");
        removals += 1;
        projection = {
          ...bound,
          can_remove: false,
          remove_reason: "Worktree removed",
          integration_options: [],
          binding: { ...bound.binding, status: "removed" },
        };
      }
      await route.fulfill({ json: projection });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/worktreeChat.html`,
    );
    await page.getByRole("checkbox", { name: "Work in a worktree" }).check();
    await page.getByRole("button", { name: "Work", exact: true }).click();
    await page.getByRole("textbox", { name: "Message" }).fill("Create my change.");
    projection = bound;
    await page.getByRole("button", { name: "Start Work turn" }).click();
    await page.getByText("Worktree: rcp/chat-one (ready)").waitFor();
    const first = await page.evaluate(() => window.worktreeRequests[0]);
    assert.equal(first.worktree, true);
    assert.equal(first.mode, "work");
    assert.equal(await page.getByRole("checkbox", { name: "Work in a worktree" }).count(), 0);

    await page.getByRole("textbox", { name: "Message" }).fill("Preserve this draft.");
    await page.getByRole("button", { name: "Integrate", exact: true }).click();
    assert.equal(await page.getByRole("button", { name: "Merge into develop" }).isDisabled(), true);
    await page.getByText("Shared checkout has uncommitted changes: local.txt").waitFor();
    await page.getByRole("button", { name: "Open a pull request", exact: true }).click();
    await page.waitForFunction(() => window.worktreeRequests.length === 2);
    const integration = await page.evaluate(() => window.worktreeRequests[1]);
    assert.equal(integration.worktree_integration, "pull_request");
    assert.equal(integration.mode, "work");
    assert.equal(integration.session_id, "session-one");
    assert.deepEqual(integration.run_truth_scope, ["repo"]);
    assert.equal(
      await page.getByRole("textbox", { name: "Message" }).inputValue(),
      "Preserve this draft.",
    );

    await page.getByRole("button", { name: "Remove worktree", exact: true }).click();
    const confirmation = page.getByRole("group", { name: "Confirm worktree removal" });
    await confirmation.waitFor();
    assert.match(await confirmation.textContent(), /Ahead of develop: 2 commits/);
    assert.match(await confirmation.textContent(), /Remote branch: absent/);
    assert.match(await confirmation.textContent(), /Checked origin using git ls-remote/);
    assert.equal(removalPreviews, 1);
    assert.match(await confirmation.textContent(), /Branch rcp\/chat-one will be kept/);
    assert.equal(removals, 0);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    assert.equal(removals, 0);
    await page.getByRole("button", { name: "Remove worktree", exact: true }).click();
    await page.getByRole("button", { name: "Confirm removal", exact: true }).click();
    await page.getByText("Worktree: rcp/chat-one (removed)").waitFor();
    assert.equal(removals, 1);
    assert.equal(removalPreviews, 2);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
