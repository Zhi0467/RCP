import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { chromium } from "playwright";

// A release qualifies only if a member can browse the copied data it was rehearsed
// on. The data module seeds the deployment tests' team project with an Experiment
// on a display cache 0.4.34 wrote and requires the update rehearsal to pass; this
// journey then serves that same data and a personal space from this checkout.
const repo = new URL("../..", import.meta.url).pathname;

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

/** Serve one data directory with no provider CLI or the developer's home in reach. */
async function serve(python, launcher, dataDir, home) {
  const port = await freePort();
  const options = ["--host", "127.0.0.1", "--port", String(port), "--web-assets", "prebuilt"];
  const child = spawn(python, [...launcher, ...options], {
    cwd: repo,
    env: { HOME: home, PATH: "/usr/bin:/bin", RCP_DATA_DIR: dataDir },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => (output += chunk));
  child.stderr.on("data", (chunk) => (output += chunk));
  const base = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 60_000;
  for (;;) {
    if (child.exitCode !== null) throw new Error(`rcp serve exited:\n${output}`);
    try {
      const health = await fetch(`${base}/api/health`);
      if (health.ok) return { child, base, health: await health.json(), output: () => output };
    } catch {
      // Not listening yet.
    }
    if (Date.now() > deadline) throw new Error(`rcp serve never became healthy:\n${output}`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

async function stop(server) {
  if (!server || server.child.exitCode !== null) return;
  server.child.kill("SIGTERM");
  await once(server.child, "exit");
}

test("a member browses the rehearsed copy across spaces without a failed read", async () => {
  const root = await mkdtemp(join(tmpdir(), "rcp-release-upgrade-"));
  const python = execFileSync("uv", ["run", "python", "-c", "import sys; print(sys.executable)"], {
    cwd: repo,
    encoding: "utf8",
  }).trim();
  const seeded = JSON.parse(
    execFileSync(python, ["-m", "tests.release_upgrade_browser_data", join(root, "team")], {
      cwd: repo,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "inherit"],
    })
      .trim()
      .split("\n")
      .at(-1),
  );
  const evidence = { responses: [], failedRequests: [], console: [], pageErrors: [] };
  let phase = "personal sign-in";
  let team;
  let personal;
  let browser;
  let passed = false;
  try {
    team = await serve(
      python,
      ["-m", "tests.release_upgrade_browser_data", "serve", join(root, "team")],
      seeded.data_dir,
      join(root, "team-home"),
    );
    personal = await serve(
      python,
      ["-m", "rcp", "serve"],
      join(root, "personal"),
      join(root, "personal-home"),
    );
    const signIn = personal.output().match(/RCP sign-in: (\S+)/)?.[1];
    assert.ok(signIn, "the personal backend prints its owner sign-in link");

    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.on("response", (response) => {
      evidence.responses.push({ phase, status: response.status(), url: response.url() });
    });
    page.on("requestfailed", (request) => {
      const failure = request.failure()?.errorText ?? "";
      // A navigation cancels the leaving page's reads; the spec does not report those.
      if (!failure.includes("ERR_ABORTED"))
        evidence.failedRequests.push({ phase, url: request.url(), failure });
    });
    page.on("console", (message) => {
      if (message.type() === "error")
        evidence.console.push({ phase, text: message.text(), url: message.location().url });
    });
    page.on("pageerror", (error) => evidence.pageErrors.push({ phase, error: String(error) }));

    /** Act, then wait until this phase has read each path; every such read must succeed. */
    async function reads(action, ...paths) {
      await action();
      const deadline = Date.now() + 30_000;
      for (const path of paths) {
        let served;
        while (
          !(served = evidence.responses.filter(
            (item) => item.phase === phase && new URL(item.url).pathname === path,
          )).length
        ) {
          assert.ok(Date.now() < deadline, `${phase}: the page never read ${path}`);
          await page.waitForTimeout(250);
        }
        for (const item of served) assert.equal(item.status, 200, `${item.url} failed`);
      }
      // Let the page settle its follow-up reads before the next step.
      await page.waitForTimeout(1_000);
    }
    const project = `/api/projects/${seeded.project_id}`;
    const open = (query) =>
      page.evaluate((hash) => (location.hash = hash), `#/projects/${seeded.project_id}${query}`);

    // The signed-out page asks who it is before it shows the sign-in form.
    const signedOutProbe = (item) =>
      item.phase === "personal sign-in" &&
      item.status === 401 &&
      new URL(item.url).pathname === "/api/identity";
    await page.goto(signIn);
    await reads(() => page.getByRole("button", { name: "Sign in" }).click(), "/api/projects");

    // Switch to the team space and sign in with the member's token.
    const protocol = String(team.health.team_shell_protocol.maximum);
    const exchange = await context.request.post(`${team.base}/api/team/session/exchange`, {
      data: { token: seeded.token },
      headers: { "RCP-Team-Shell-Protocol": protocol },
    });
    assert.equal(exchange.status(), 200, await exchange.text());
    phase = "team space";
    await reads(() => page.goto(`${team.base}/`), "/api/projects", "/api/space/runs");
    // Before any project opens, the Experiment on the cache 0.4.34 wrote is listed.
    const runs = await page.evaluate(async () => (await fetch("/api/space/runs")).json());
    assert.deepEqual(
      runs.map((run) => run.experiment_id),
      ["exp/recovery"],
    );
    await reads(() => open(""), project);
    await reads(() => open("?view=runs"), `${project}/experiment-episodes`);
    await reads(() => open("?view=attention"), `${project}/consolidation`);
    await reads(
      () => open(`?view=chats&chat=${seeded.chat_id}`),
      `${project}/chats/${seeded.chat_id}`,
    );
    await page.getByText("Drew the figure.").first().waitFor();
    await reads(() => open("?view=artifacts"), `${project}/artifacts`);
    const listed = await page.evaluate(
      async (path) => (await fetch(path)).text(),
      `${project}/artifacts`,
    );
    assert.ok(listed.includes(seeded.artifact_id), "the copied turn artifact is listed");

    // And back to the personal space, which this browser already signed in to.
    phase = "personal space";
    await reads(() => page.goto(`${personal.base}/`), "/api/projects");

    const expected = evidence.responses.filter(signedOutProbe);
    const unexpected = {
      failedResponses: evidence.responses.filter(
        (item) => item.status >= 400 && !expected.includes(item),
      ),
      failedRequests: evidence.failedRequests,
      // Chromium also logs each failed response; the expected probe's line is expected.
      console: evidence.console.filter(
        (item) => !expected.some((probe) => probe.phase === item.phase && probe.url === item.url),
      ),
      pageErrors: evidence.pageErrors,
    };
    assert.deepEqual(unexpected, {
      failedResponses: [],
      failedRequests: [],
      console: [],
      pageErrors: [],
    });
    passed = true;
  } finally {
    await browser?.close();
    await stop(team);
    await stop(personal);
    if (passed) await rm(root, { recursive: true, force: true });
    else {
      const path = join(root, "evidence.json");
      await writeFile(
        path,
        JSON.stringify(
          { ...evidence, teamLog: team?.output(), personalLog: personal?.output() },
          null,
          2,
        ),
      );
      console.error(`Release upgrade browser evidence kept at ${path}`);
    }
  }
});
