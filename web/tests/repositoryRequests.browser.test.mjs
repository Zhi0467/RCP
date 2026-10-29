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
      if (path.endsWith("/directories")) {
        const opened = route.request().postDataJSON().path ?? "/home/me";
        return route.fulfill({
          json: {
            path: opened,
            parent: "/home",
            entries:
              opened === "/home/me"
                ? [{ name: "Notes", path: "/home/me/Notes", protected: false }]
                : [],
            total: opened === "/home/me" ? 1 : 0,
            next_offset: null,
          },
        });
      }
      if (path.endsWith("repository-requests")) {
        body = route.request().postDataJSON();
        projection.kind = body.kind;
        return route.fulfill({ json: projection });
      }
      if (path.endsWith("/complete")) confirmed = true;
      return route.fulfill({ json: path.includes(requestId) ? projection : [] });
    });
    const url = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/repositoryRequests.html`;
    const cases = {
      folder: {
        kind: "add_repository",
        repository: { alias: "notes", path: "/home/me/Notes", count_as_project_truth: false },
      },
      github: {
        kind: "add_repository",
        repository: {
          alias: "state-2",
          source: "https://github.com/lab/state.git",
          count_as_project_truth: true,
        },
      },
      connect: {
        kind: "connect_repository",
        alias: "state",
        source: "https://github.com/lab/state.git",
      },
    };
    for (const [mode, expected] of Object.entries(cases)) {
      await page.goto(mode === "connect" ? `${url}?connect` : url);
      const submit = page.locator('button[type="submit"]');
      if (mode === "connect") {
        assert.equal(await submit.isDisabled(), true);
        await page.locator('[name="source"]').fill(" https://github.com/lab/state.git ");
      } else {
        await page.locator(".repository-requests > button").click();
        assert.equal(await page.locator('[aria-pressed="true"]').textContent(), "Folder");
        assert.equal(await page.locator('[name="truth"]').isChecked(), false);
        assert.equal(await submit.isDisabled(), true);
        if (mode === "folder") {
          await page.locator(".repository-add-path button").click();
          await page.locator('[data-path-picker-entry="folder"]').click();
          await page.locator('[aria-current="location"]', { hasText: "Notes" }).waitFor();
          await page.locator('[data-path-picker-action="pick"]').click();
          assert.equal(await page.locator('[name="path"]').inputValue(), "/home/me/Notes");
        } else {
          await page.locator('[aria-pressed="false"]').click();
          await page.locator('[name="source"]').fill("https://github.com/lab/state.git");
          await page.locator('[name="truth"]').check();
        }
        assert.equal(await page.locator('[name="alias"]').inputValue(), expected.repository.alias);
      }
      await submit.click();
      await page.locator(".provisioning-final-review").waitFor();
      assert.equal(new URL(page.url()).hash, `#/projects/new?request=${requestId}`);
      assert.deepEqual(body, expected);
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
    const names = page.locator(".team-repository-stack .repository-editor header strong");
    const fields = page.locator(".team-repository-fields input");
    assert.equal(await fields.count(), 1);
    assert.equal(await fields.first().inputValue(), "");
    assert.equal(await names.first().textContent(), "server-project");
    await page.locator(".team-repository-stack + .add-repository").click();
    assert.deepEqual(await names.allTextContents(), ["server-project", "server-project-2"]);
    assert.equal(await fields.count(), 4);
    await page.locator(".setup-actions button.primary").click();
    assert.equal(await page.locator('[role="alert"]').count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
