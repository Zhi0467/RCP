import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("external job Cancel uses one watcher and the machine card saves compute on the space machine", async () => {
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
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    page.on("request", (request) => {
      if (request.url().includes("/api/")) requests.push(request.url());
    });
    await page.route("**/api/**", (route) => route.fulfill({ json: [] }));
    // Compute lives on the space machine card, which the project card reads.
    const idle = { scheduler: null, helper: null };
    const spaceMachine = (alias, jobManager) => ({
      machine_id: `m-${alias}`,
      name: alias,
      host: alias === "local" ? "" : alias,
      os_account: "rcp",
      writable_paths: [],
      hidden_folders: [],
      provider_autocompact: {},
      provider_shell_timeout: {},
      autocompact_providers: [],
      shell_timeout_providers: [],
      hidden_read: null,
      projects: [{ project_id: "project", project_name: "Project", alias }],
      in_use: true,
      job_manager: jobManager,
      jobs_root: "",
      access_hint: "",
      provider_paths: {},
      providers: [],
      provider_readiness: {},
      compute_probes: idle,
      reach: [],
    });
    const machines = {
      "m-local": spaceMachine("local", "none"),
      "m-cluster": spaceMachine("cluster", null),
    };
    const patches = [];
    await page.route("**/api/space/machines", (route) =>
      route.fulfill({ json: { machines: Object.values(machines) } }),
    );
    await page.route("**/api/space/machines/*/browser", (route) =>
      route.fulfill({
        json: { status: "ready", detail: null, apt_command: null, admin_command: null },
      }),
    );
    await page.route("**/api/space/machines/*/dependencies", (route) =>
      route.fulfill({
        json: {
          outcome: "ready",
          platform: null,
          distribution: null,
          tested: true,
          missing: [],
          install_command: null,
          install_notes: [],
          reason: null,
          checked_at: new Date().toISOString(),
        },
      }),
    );
    await page.route("**/api/space/machines/m-*", async (route) => {
      const id = new URL(route.request().url()).pathname.split("/").pop();
      const body = route.request().postDataJSON();
      patches.push({ id, method: route.request().method(), body });
      machines[id] = { ...machines[id], ...body };
      await route.fulfill({ json: machines[id] });
    });
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
          cancel_requested_by_name: "Ada Researcher",
          cancel_requested_at: new Date().toISOString(),
          cancel_error: cancellations === 1 ? "Scheduler unavailable; retry after repair" : null,
        },
      });
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/externalJobs.html`);
    const job = page.getByRole("region", { name: "External job" });
    const cancel = job.getByRole("button", { name: "Cancel job train.log" });
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      const button = await cancel.boundingBox();
      const title = await job.locator("strong").boundingBox();
      const row = await job.boundingBox();
      assert.ok(button.x > title.x + title.width, "Cancel sits to the right of the title");
      assert.ok(button.x + button.width <= row.x + row.width, "Cancel stays inside the row");
      assert.ok(
        title.y >= button.y && title.y < button.y + button.height,
        "Cancel shares the title row",
      );
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await cancel.click();
    await job.getByRole("alert").filter({ hasText: "Scheduler unavailable" }).waitFor();
    await cancel.click();
    await cancel.waitFor({ state: "hidden" });
    assert.equal(cancellations, 2);
    assert.equal(await job.locator(":scope > .watcher-state.stopped").count(), 1);
    assert.match(await job.innerText(), /Ada Researcher/);
    assert.equal(await job.locator(":scope > .watcher-state.completed").count(), 0);

    assert.equal(
      requests.some((url) => url.includes("compute-jobs")),
      false,
    );

    // One machine's card is open at a time; its tile opens it.
    const card = page.locator(".provider-machine");
    const compute = card.locator('[data-machine-fold="compute"]');
    const open = async (alias) => {
      await page.locator(`.machine-tiles [data-machine-tile="${alias}"]`).click();
      await compute.locator("summary").click();
    };
    const route = (name) => compute.locator(`[data-compute-route="${name}"]`);
    await open("cluster");
    assert.equal(await route("scheduler").count(), 0);
    // Off on an undecided card is still saved, so a later check cannot turn Slurm on.
    const slurm = compute.getByRole("group", { name: "Slurm" });
    const offSaved = page.waitForResponse((response) => response.request().method() === "PATCH");
    await slurm.getByRole("button", { name: "Off", exact: true }).click();
    await offSaved;
    await slurm.getByRole("button", { name: "On", exact: true }).click();
    await route("scheduler").waitFor();
    const jobsRoot = compute.getByRole("textbox", { name: "Job output folder" });
    await jobsRoot.fill("/cluster-edited");
    const saved = page.waitForResponse((response) => response.request().method() === "PATCH");
    await jobsRoot.press("Enter");
    await saved;
    assert.deepEqual(patches, [
      { id: "m-cluster", method: "PATCH", body: { job_manager: "none" } },
      { id: "m-cluster", method: "PATCH", body: { job_manager: "slurm" } },
      { id: "m-cluster", method: "PATCH", body: { jobs_root: "/cluster-edited" } },
    ]);
    assert.equal(await jobsRoot.inputValue(), "/cluster-edited");

    await page.route("**/api/space/machines/m-cluster/compute/check", (route) =>
      route.fulfill({
        json: {
          ...machines["m-cluster"],
          compute_probes: {
            scheduler: {
              backend_id: "slurm",
              ready: true,
              state: "ready",
              status_label: "Ready",
              status_tone: "ready",
              diagnostic: "Scheduler reachable",
            },
            helper: {
              backend_id: "systemd_user",
              ready: false,
              state: "failed",
              status_label: "Failed",
              status_tone: "error",
              diagnostic: "User manager is unavailable",
              required_action: "Enable linger for the rcp account",
            },
          },
          reach: [
            {
              from: "local",
              probe: {
                compute_id: "cluster",
                execution_machine: "local",
                state: "opaque",
                reachable: true,
                diagnostic: "",
                required_action: null,
                status_label: "Reachable",
                status_tone: "ready",
              },
            },
          ],
        },
      }),
    );
    await compute.locator('[data-machine-action="check-compute"]').click();
    await route("scheduler").getByText("Scheduler reachable").waitFor();
    await route("helper").getByText("Enable linger for the rcp account").waitFor();
    assert.equal(await route("scheduler").getAttribute("class"), "compute-probe ready");
    assert.equal(await route("helper").getAttribute("class"), "compute-probe error");
    assert.equal(await compute.locator('[data-compute-reach="local"]').count(), 1);
    await open("local");
    assert.deepEqual(
      await compute
        .locator("[data-compute-route]")
        .evaluateAll((routes) => routes.map((route) => route.dataset.computeRoute)),
      ["helper"],
      "A machine without a job manager offers only the helper route",
    );

    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

for (const kind of ["shell", "graph"]) {
  test(`completed ${kind} watchers hide in Runs, persist, and never appear in Chat`, async () => {
    const label = (id) =>
      kind === "shell"
        ? `${id}.log`
        : id === "grouped"
          ? "Proposal on hypothesis/grouped is resolved"
          : `decision/${id} reaches decided`;
    const hideName = (id) => `Hide watcher ${label(id)}`;
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
      const mutations = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("**/api/**", (route) => {
        if (route.request().method() !== "GET") mutations.push(route.request().url());
        return route.fulfill({ json: [] });
      });
      const url = `http://127.0.0.1:${address.port}/tests/fixtures/externalJobs.html?watchers=${kind}`;
      await page.goto(url);
      const run = page.getByRole("region", { name: "Experiment run", exact: true });
      const chat = page.getByRole("region", { name: "Chat about Watcher experiment", exact: true });
      // The chat strip shows only the live watcher; ended ones stay in Runs.
      await chat.getByRole("button", { name: "1 active watcher", exact: true }).click();
      assert.equal(await chat.locator(".chat-watcher-row").count(), 1);
      assert.equal(await chat.getByRole("button", { name: /^Hide watcher/ }).count(), 0);
      if (kind === "shell") await run.getByText("Finished batch", { exact: true }).click();
      const hide = run.getByRole("button", { name: hideName("completed"), exact: true });
      for (const width of [1280, 390]) {
        await page.setViewportSize({ width, height: 900 });
        const button = await hide.boundingBox();
        const title = await run
          .locator(".chat-watcher-row > strong")
          .filter({ hasText: label("completed") })
          .boundingBox();
        assert.ok(button.x > title.x + title.width, "Hide sits to the right of the title");
        assert.ok(
          title.y >= button.y && title.y < button.y + button.height,
          "Hide shares the title row",
        );
      }
      await page.setViewportSize({ width: 1280, height: 900 });
      await hide.click();
      await run.getByRole("button", { name: hideName("grouped"), exact: true }).click();
      await run.getByRole("button", { name: "Show hidden watchers (2)", exact: true }).waitFor();
      assert.equal(await run.locator(".experiment-run-watcher").count(), 1);
      assert.equal(await run.locator(".experiment-run-watcher-group").count(), 0);
      assert.equal(await chat.locator(".chat-watcher-row").count(), 1);

      await page.reload();
      await run.getByRole("button", { name: "Show hidden watchers (2)" }).click();
      await run.getByRole("button", { name: hideName("completed") }).waitFor();
      assert.equal(await run.locator(".experiment-run-watcher").count(), 3);

      // A watcher that becomes live again joins the chat strip.
      await page.evaluate(() => {
        const { watchers, setWatchers } = window.watcherFixture;
        setWatchers(
          watchers.map((watcher) =>
            watcher.watcher_id === "completed"
              ? { ...watcher, status: "active", can_cancel: true }
              : watcher,
          ),
        );
      });
      await chat.getByRole("button", { name: "2 active watchers", exact: true }).waitFor();
      assert.deepEqual(mutations, []);
      assert.deepEqual(errors, []);
    } finally {
      await browser?.close();
      await server.close();
    }
  });
}
