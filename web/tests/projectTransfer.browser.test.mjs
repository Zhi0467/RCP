import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("transfer option stays off by default, binds retries, and survives review reload", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const address = server.httpServer.address();
    browser = await chromium.launch({ headless: true });
    for (const includeLocalCommits of [false, true]) {
      const context = await browser.newContext();
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      const projectId = "33333333-3333-4333-8333-333333333333";
      const profiles = [
        "seed",
        "refresh",
        "node_chat",
        "project_chat",
        "paper_coach",
        "orchestrator",
      ];
      const profile = {
        provider: "codex",
        runtime: "exec",
        model: "test-model",
        reasoning: "medium",
        run_on: "server",
      };
      await page.route("**/api/projects/**", async (route) => {
        const url = new URL(route.request().url());
        const result = url.pathname.endsWith(projectId)
          ? {
              id: projectId,
              name: "Transfer fixture",
              state_repository: "state",
              default_auto_research_invocation_ceiling: 3,
              repositories: [{ alias: "state", machine: "server", path: "/tmp/personal-state" }],
              machines: [{ alias: "server", host: "", os_account: "researcher" }],
              agent_profiles: Object.fromEntries(profiles.map((name) => [name, profile])),
            }
          : [];
        await route.fulfill({ json: result });
      });
      await page.addInitScript(
        ({ projectId, failFirst }) => {
          const connection = {
            connection_id: "66666666-6666-4666-8666-666666666666",
            expected_space_id: "55555555-5555-4555-8555-555555555555",
            display_name: "Fixture team",
            ssh_target: "fixture-team",
            local_origin: "https://fixture.rcp.localhost",
            operator_route: { ssh_target: "rcp@fixture-team" },
          };
          const providers = [
            {
              provider: "codex",
              label: "Codex",
              installed: true,
              authenticated: true,
              runtimes: [{ id: "exec", label: "Exec" }],
              default_runtime: "exec",
              models: [
                {
                  id: "test-model",
                  label: "Test model",
                  reasoning: ["medium"],
                  default_reasoning: "medium",
                },
              ],
            },
          ];
          window.__TAURI_INTERNALS__ = {
            invoke: async (command, args) => {
              if (command === "desktop_list_team_connections") return [connection];
              if (command === "desktop_establish_team_session")
                return { connection, identity: { space_id: connection.expected_space_id } };
              if (command === "desktop_read_target_project_provisioning_options") return providers;
              if (command === "desktop_load_project_transfer")
                return JSON.parse(localStorage.getItem("transfer-bundle"));
              if (command !== "desktop_prepare_project_transfer")
                throw new Error(`Unexpected native command: ${command}`);
              const attempts = JSON.parse(localStorage.getItem("transfer-attempts") ?? "[]");
              attempts.push(args.request);
              localStorage.setItem("transfer-attempts", JSON.stringify(attempts));
              if (failFirst && attempts.length === 1)
                throw new Error("Interrupted preparation fixture");
              const request = args.request;
              const repo = {
                alias: "state",
                machine_alias: "server",
                repository: { identity: "example/state" },
              };
              const bundle = {
                source: {
                  request_id: request.source_request_id,
                  source_configuration: {
                    repositories: [
                      {
                        ...repo,
                        ...(request.include_local_commits ? { source_commit: "a".repeat(40) } : {}),
                      },
                    ],
                  },
                },
                target: {
                  request_id: request.target_request_id,
                  project_id: projectId,
                  target_space_id: connection.expected_space_id,
                },
                incoming_provisioning: {
                  ...request.target_provisioning,
                  status_label: "Ready for review",
                  authorized_by: { display_name: "Fixture owner" },
                  machines: request.target_provisioning.machines,
                  repositories: [{ ...repo, resolved_path: "/tmp/team-state" }],
                  readiness: {
                    repositories_ready: 1,
                    repositories_total: 1,
                    providers_ready: 6,
                    providers_total: 6,
                  },
                  operator_argv: [],
                  final_review: {
                    proposed_project_id: projectId,
                    digest: "b".repeat(64),
                    authorized_by: { display_name: "Fixture owner" },
                  },
                },
                target_provider_setup: providers,
                ...(request.include_local_commits ? { include_local_commits: true } : {}),
              };
              localStorage.setItem("transfer-bundle", JSON.stringify(bundle));
              return bundle;
            },
          };
        },
        { projectId, failFirst: includeLocalCommits },
      );
      await page.goto(
        `http://127.0.0.1:${address.port}/tests/fixtures/projectTransfer.html#/projects/new?intent=move_personal_project_to_team&source_project_id=${projectId}`,
      );
      await page.getByRole("radio", { name: /Fixture team/ }).check();
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      const checkbox = page.getByRole("checkbox", { name: "Include local unpushed commits" });
      await checkbox.waitFor();
      assert.equal(await checkbox.isChecked(), false);
      await page.getByText(/Local unpushed commits stay behind/).waitFor();
      if (includeLocalCommits) await checkbox.check();
      await page.getByRole("button", { name: "Prepare team target" }).click();
      if (includeLocalCommits) {
        await page
          .getByRole("alert")
          .filter({ hasText: "Interrupted preparation fixture" })
          .waitFor();
        assert.equal(await checkbox.isChecked(), true);
        assert.equal(await checkbox.isDisabled(), true);
        assert.equal(
          await page
            .locator("input, select")
            .evaluateAll((fields) => fields.every((field) => field.disabled)),
          true,
        );
        assert.equal(
          await page.getByRole("button", { name: "Back", exact: true }).isEnabled(),
          true,
        );
        await page.getByRole("button", { name: "Retry preparation" }).click();
      }
      await page.getByRole("heading", { name: "Final review" }).waitFor();
      const attempts = await page.evaluate(() =>
        JSON.parse(localStorage.getItem("transfer-attempts")),
      );
      assert.equal(Object.hasOwn(attempts[0], "include_local_commits"), includeLocalCommits);
      if (includeLocalCommits) assert.deepEqual(attempts[0], attempts[1]);
      await page.reload();
      await page.getByRole("heading", { name: "Final review" }).waitFor();
      assert.equal(await checkbox.count(), 0);
      await page
        .getByText(/Uncommitted files and external data\/output directories remain excluded/)
        .waitFor();
      await page.getByText(/RCP does not push to GitHub/).waitFor();
      if (includeLocalCommits) {
        await page.getByText(/Committed files and history are copied as saved/).waitFor();
        await page.getByText(/detached HEAD at the saved source commit/).waitFor();
        await page.getByText("a".repeat(40), { exact: true }).waitFor();
      } else {
        await page.getByText(/Local unpushed commits stay behind/).waitFor();
      }
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally {
    await browser?.close();
    await server.close();
  }
});
