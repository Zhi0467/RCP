import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { chromium } from "playwright";

import { createCommentsClient } from "../src/comments/api.ts";

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

async function serve(python, dataDir, home) {
  const port = await freePort();
  const child = spawn(
    python,
    [
      "-m",
      "rcp",
      "serve",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--web-assets",
      "prebuilt",
    ],
    {
      cwd: repo,
      env: { HOME: home, PATH: "/usr/bin:/bin", RCP_DATA_DIR: dataDir },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
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
      await new Promise((resolve) => setTimeout(resolve, 250));
      continue;
    }
    if (Date.now() > deadline) throw new Error(`rcp serve never became healthy:\n${output}`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

async function signedInCommentsClient(server, projectId, token) {
  const exchange = await fetch(`${server.base}/api/team/session/exchange`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "RCP-Team-Shell-Protocol": String(server.health.team_shell_protocol.maximum),
    },
    body: JSON.stringify({ token }),
  });
  assert.equal(exchange.status, 200, await exchange.text());
  const cookie = exchange.headers
    .getSetCookie()
    .map((line) => line.split(";")[0])
    .join("; ");
  return createCommentsClient(projectId, (path, init) =>
    fetch(`${server.base}${path}`, {
      ...init,
      headers: { ...init?.headers, Cookie: cookie, Origin: server.base },
    }),
  );
}

test("a mentioned member follows the Inbox link to the thread and settles it", async () => {
  const root = await mkdtemp(join(tmpdir(), "rcp-comments-team-"));
  const python = execFileSync("uv", ["run", "python", "-c", "import sys; print(sys.executable)"], {
    cwd: repo,
    encoding: "utf8",
  }).trim();
  const seeded = JSON.parse(
    execFileSync(python, ["-m", "tests.comments_team_browser_data", join(root, "team")], {
      cwd: repo,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "inherit"],
    })
      .trim()
      .split("\n")
      .at(-1),
  );
  let server;
  let browser;
  let passed = false;
  try {
    server = await serve(python, seeded.data_dir, join(root, "home"));
    const bob = await signedInCommentsClient(server, seeded.project_id, seeded.bob_token);
    assert.deepEqual(
      (await bob.mentions()).map((mention) => mention.thread.thread_id),
      [seeded.thread_id],
    );

    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const exchange = await context.request.post(`${server.base}/api/team/session/exchange`, {
      data: { token: seeded.bob_token },
      headers: { "RCP-Team-Shell-Protocol": String(server.health.team_shell_protocol.maximum) },
    });
    assert.equal(exchange.status(), 200, await exchange.text());
    const page = await context.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    const members = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/projects/${seeded.project_id}/members`) &&
        response.request().frame() !== page.mainFrame(),
    );
    await page.goto(`${server.base}/#/projects/${seeded.project_id}?view=attention`);
    await page.locator(".comment-mention-row").click();
    const viewer = page.frameLocator(".artifact-viewer iframe, iframe[src*='/viewer']").first();
    await viewer
      .locator(`[data-thread-id="${seeded.thread_id}"].selected`)
      .first()
      .waitFor({ timeout: 30_000 });
    assert.equal((await members).status(), 200);
    assert.deepEqual(pageErrors, []);

    const reply = await bob.reply(seeded.thread_id, "Looked at it.");
    const replyPost = reply.posts.at(-1).post_id;
    assert.equal((await bob.resolve(seeded.thread_id)).state, "resolved");
    assert.equal((await bob.reopen(seeded.thread_id)).state, "open");
    assert.equal(
      (await bob.editPost(replyPost, "Looked again.")).posts.at(-1).text,
      "Looked again.",
    );
    assert.deepEqual(await bob.deletePost(replyPost), { ok: true });
    passed = true;
  } finally {
    await browser?.close();
    if (server && server.child.exitCode === null) {
      server.child.kill("SIGTERM");
      await once(server.child, "exit");
    }
    if (passed) await rm(root, { recursive: true, force: true });
    else console.error(`Comments team evidence kept at ${root}\n${server?.output() ?? ""}`);
  }
});
