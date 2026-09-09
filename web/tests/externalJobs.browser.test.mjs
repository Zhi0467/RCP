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

for (const kind of ["shell", "graph"]) {
  test(`completed ${kind} watchers hide as whole cards across Runs and Chat, persist, and restore`, async () => {
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
      await chat.getByRole("button", { name: "3 watchers", exact: true }).click();
      if (kind === "shell") await run.getByText("Finished batch", { exact: true }).click();
      const hide = run.getByRole("button", { name: hideName("completed"), exact: true });
      for (const width of [1280, 390]) {
        await page.setViewportSize({ width, height: 900 });
        for (const surface of [run, chat]) {
          const button = await surface
            .getByRole("button", { name: hideName("completed"), exact: true })
            .boundingBox();
          const title = await surface
            .locator(".chat-watcher-row > strong")
            .filter({ hasText: label("completed") })
            .boundingBox();
          assert.ok(button.x > title.x + title.width, "Hide sits to the right of the title");
          assert.ok(
            title.y >= button.y && title.y < button.y + button.height,
            "Hide shares the title row",
          );
        }
      }
      await page.setViewportSize({ width: 1280, height: 900 });
      await hide.click();
      await run.getByRole("button", { name: hideName("grouped"), exact: true }).click();
      await chat.getByRole("button", { name: "Show hidden watchers (2)", exact: true }).waitFor();
      assert.equal(await run.locator(".experiment-run-watcher").count(), 1);
      assert.equal(await run.locator(".experiment-run-watcher-group").count(), 0);
      assert.equal(await chat.locator(".chat-watcher-row").count(), 1);
      if (kind === "shell") {
        assert.equal(await run.getByRole("button", { name: "Cancel job active.log" }).count(), 1);
      } else {
        assert.equal(await run.getByRole("button", { name: hideName("active") }).count(), 0);
      }
      await page.evaluate(() => {
        const { watchers, setWatchers } = window.watcherFixture;
        setWatchers(watchers.map((watcher) => ({ ...watcher })));
      });
      assert.equal(await run.locator(".experiment-run-watcher").count(), 1);
      await page.reload();
      await run.getByRole("button", { name: "Show hidden watchers (2)" }).waitFor();
      await chat.getByRole("button", { name: "1 watcher, 2 hidden", exact: true }).click();
      await chat.getByRole("button", { name: "Show hidden watchers (2)" }).click();
      await run.getByRole("button", { name: hideName("completed") }).waitFor();
      assert.equal(await run.locator(".experiment-run-watcher").count(), 3);
      await chat.getByRole("button", { name: hideName("completed") }).click();
      await run.getByRole("button", { name: "Show hidden watchers (1)" }).waitFor();

      await page.evaluate(() => window.watcherFixture.setProjectId("other-project"));
      await run.getByRole("button", { name: hideName("completed") }).waitFor();
      await page.evaluate(() => window.watcherFixture.setProjectId("project"));
      await run.getByRole("button", { name: "Show hidden watchers (1)" }).waitFor();
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
      if (kind === "shell") {
        await run.getByRole("button", { name: "Cancel job completed.log" }).waitFor();
      } else {
        await run
          .locator(".chat-watcher-row > strong")
          .filter({ hasText: label("completed") })
          .waitFor();
        assert.equal(await run.getByRole("button", { name: hideName("completed") }).count(), 0);
      }
      assert.equal(await run.getByRole("button", { name: /Show hidden watchers/ }).count(), 0);

      // When every current card is hidden, each surface still offers a way back.
      await page.evaluate(() => {
        const { watchers, setWatchers } = window.watcherFixture;
        setWatchers(
          watchers.map((watcher) => ({ ...watcher, status: "completed", can_cancel: false })),
        );
      });
      await chat.getByRole("button", { name: hideName("active") }).click();
      await chat.getByRole("button", { name: hideName("grouped") }).click();
      await run.getByRole("button", { name: "Show hidden watchers (3)" }).waitFor();
      assert.equal(await run.locator(".experiment-run-watcher").count(), 0);
      assert.equal(await chat.locator(".chat-watcher-row").count(), 0);
      await chat.getByRole("button", { name: "0 watchers, 3 hidden", exact: true }).click();
      await chat.getByRole("button", { name: "0 watchers, 3 hidden", exact: true }).click();
      await run.getByRole("button", { name: "Show hidden watchers (3)" }).click();
      await chat.getByRole("button", { name: hideName("active") }).waitFor();
      assert.equal(await run.locator(".experiment-run-watcher").count(), 3);
      assert.deepEqual(mutations, []);
      assert.deepEqual(errors, []);
    } finally {
      await browser?.close();
      await server.close();
    }
  });
}
