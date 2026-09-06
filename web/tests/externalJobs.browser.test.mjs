import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("external job Cancel and machine setup use one watcher and preserve newer settings", async () => {
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
    const page = await browser.newPage();
    const errors = [];
    const requests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => {
      if (request.url().includes("/api/")) requests.push(request.url());
    });
    await page.route("**/api/**", (route) => route.fulfill({ json: [] }));
    let cancellations = 0;
    await page.route("**/watchers/watcher-1/cancel", async (route) => {
      cancellations += 1;
      assert.equal(route.request().method(), "POST");
      assert.equal(route.request().postData(), null);
      const watcher = await page.evaluate(() => window.jobFixture.watcher);
      await route.fulfill({
        json: {
          ...watcher,
          can_cancel: cancellations === 1,
          cancel_requested_by: "human-1",
          cancel_requested_at: new Date().toISOString(),
          cancel_error: cancellations === 1 ? "Scheduler unavailable; retry after repair" : null,
        },
      });
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/externalJobs.html`);
    const job = page.getByRole("region", { name: "External job" });
    const cancel = job.getByRole("button", { name: "Cancel job train.log" });
    await cancel.click();
    await job.getByRole("alert").filter({ hasText: "Scheduler unavailable" }).waitFor();
    await cancel.click();
    await cancel.waitFor({ state: "hidden" });
    assert.equal(cancellations, 2);
    assert.match(await job.innerText(), /Watcher stopped/);
    assert.match(await job.innerText(), /Cancel requested by human-1/);
    assert.doesNotMatch(await job.innerText(), /completed|success|succeeded/i);
    assert.equal(
      requests.some((url) => url.includes("compute-jobs")),
      false,
    );

    const machines = page.locator(".provider-machine");
    const local = machines.nth(0);
    const cluster = machines.nth(1);
    await cluster.getByRole("textbox", { name: "Jobs root" }).fill("/cluster-edited");
    await cluster.getByRole("checkbox", { name: "Use Slurm" }).check();
    await page.evaluate(() => {
      const { project, setProject } = window.jobFixture;
      setProject({
        ...project,
        machines: project.machines.map((machine) =>
          machine.alias === "local"
            ? { ...machine, compute: { job_manager: null, jobs_root: "/new" } }
            : machine,
        ),
      });
    });
    await page.waitForFunction(() =>
      [...document.querySelectorAll("input")].some((input) => input.value === "/new"),
    );
    assert.equal(await local.getByRole("textbox", { name: "Jobs root" }).inputValue(), "/new");
    const staged = await page.evaluate(() =>
      JSON.parse(localStorage.getItem("rcp:settings-draft:project")),
    );
    assert.deepEqual(staged.machineComputeEdits, {
      cluster: { job_manager: "slurm", jobs_root: "/cluster-edited" },
    });
    let settingsRequest;
    await page.route("**/api/projects/project/settings", async (route) => {
      settingsRequest = route.request().postDataJSON();
      const project = await page.evaluate(() => window.jobFixture.project);
      await route.fulfill({
        json: {
          ...project,
          machines: project.machines.map((machine) =>
            Object.hasOwn(settingsRequest.machine_compute, machine.alias)
              ? { ...machine, compute: settingsRequest.machine_compute[machine.alias] }
              : machine,
          ),
        },
      });
    });
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await page.getByText("Saved.", { exact: true }).waitFor();
    assert.deepEqual(settingsRequest.machine_compute, {
      cluster: { job_manager: "slurm", jobs_root: "/cluster-edited" },
    });
    assert.equal(await local.getByRole("textbox", { name: "Jobs root" }).inputValue(), "/new");
    assert.equal(await page.getByText("Slurm account", { exact: true }).count(), 0);
    assert.equal(await page.getByText("Slurm partition", { exact: true }).count(), 0);

    await page.route("**/machines/local/compute/probe", (route) =>
      route.fulfill({
        json: {
          backend_id: "systemd_user",
          ready: false,
          state: "failed",
          status_label: "Failed",
          status_tone: "error",
          diagnostic: "User manager is unavailable",
          required_action: "Enable linger for the rcp account",
        },
      }),
    );
    await local.getByRole("button", { name: "Probe", exact: true }).click();
    await local.getByText("User manager is unavailable").waitFor();
    await page.evaluate(() => {
      const { project, setProject } = window.jobFixture;
      setProject({
        ...project,
        machines: project.machines.map((machine) => ({ ...machine, compute_probe: null })),
      });
    });
    await local.getByText("User manager is unavailable").waitFor();
    assert.match(await local.innerText(), /Enable linger for the rcp account/);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
