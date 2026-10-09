import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("repository requests preserve intent and open the bound provisioning review", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    const requestId = "11111111-1111-4111-8111-111111111111";
    let body;
    let confirmed = false;
    const projection = {
      request_id: requestId,
      kind: "add_repository",
      name: "Fixture",
      proposed_project_id: "project",
      authorized_by: { display_name: "Member" },
      status_label: "Ready",
      can_review: true,
      can_run_setup: false,
      can_cancel: false,
      machines: [],
      provider_checks: [],
      repositories: [
        { alias: "state", source_kind: "server_only", repository: null, status_label: "Ready" },
      ],
      readiness: { machines_total: 0, repositories_total: 1, providers_total: 0 },
      final_review: { authorized_by: { display_name: "Member" }, digest: "a".repeat(64) },
      operator_argv: ["rcp", "server", "project", "provision", requestId],
    };
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("repository-requests")) {
        body = route.request().postDataJSON();
        projection.kind = body.kind;
        return route.fulfill({ json: projection });
      }
      if (path.endsWith("/complete")) confirmed = true;
      return route.fulfill({ json: path.includes(requestId) ? projection : [] });
    });
    const url = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/repositoryRequests.html`;
    for (const kind of ["add_repository", "connect_repository"]) {
      await page.goto(kind === "add_repository" ? url : `${url}?connect`);
      if (kind === "add_repository") {
        await page.locator(".repository-requests > button").click();
        await page.locator('[name="alias"]').fill("analysis");
        assert.equal(await page.locator('[name="truth"]').isChecked(), true);
        await page.locator('[name="truth"]').uncheck();
      } else {
        assert.equal(await page.locator('button[type="submit"]').isDisabled(), true);
        await page.locator('[name="source"]').fill(" https://github.com/lab/state.git ");
      }
      await page.locator('button[type="submit"]').click();
      await page.locator(".provisioning-final-review").waitFor();
      assert.equal(new URL(page.url()).hash, `#/projects/new?request=${requestId}`);
      assert.deepEqual(
        body,
        kind === "add_repository"
          ? {
              kind,
              repository: {
                alias: "analysis",
                source: null,
                machine_alias: "server",
                count_as_project_truth: false,
              },
            }
          : { kind, alias: "state", source: "https://github.com/lab/state.git" },
      );
      assert.equal(confirmed, false);
      await Promise.all([
        page.waitForResponse((response) => response.url().endsWith("/complete")),
        page.locator(".provisioning-final-review button").click(),
      ]);
      assert.equal(confirmed, true);
      confirmed = false;
    }
    await page.goto(`${url}?setup`);
    await page.locator(".setup-field input").first().fill("Server project");
    await page.locator(".setup-actions button.primary").click();
    await page.locator(".team-repository-stack").waitFor();
    assert.equal(await page.locator(".team-repository-fields input").nth(1).inputValue(), "");
    await page.locator(".setup-actions button.primary").click();
    assert.equal(await page.locator('[role="alert"]').count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
