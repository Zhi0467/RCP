import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test(
  "the Space composer uploads, removes, and sends files through real routes",
  { timeout: 60_000 },
  async () => {
    const backend = spawn(
      "uv",
      ["run", "--no-sync", "python", "-m", "tests.space_attachments_browser"],
      {
        cwd: new URL("../..", import.meta.url),
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let logs = "";
    backend.stderr.on("data", (chunk) => (logs += chunk));
    let server;
    let browser;
    try {
      const lines = createInterface({ input: backend.stdout });
      let seed;
      for await (const line of lines) {
        if (line.startsWith('{"base":')) {
          seed = JSON.parse(line);
          break;
        }
      }
      assert.ok(seed, logs);
      server = await createServer({
        root: new URL("..", import.meta.url).pathname,
        logLevel: "error",
        server: { host: "127.0.0.1", port: 0, proxy: { "/api": { target: seed.base } } },
      });
      await server.listen();
      browser = await chromium.launch({ headless: true });
      const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
      const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
      const signedIn = await page.request.post(`${origin}/api/owner/redeem`, {
        data: { code: seed.code },
      });
      assert.equal(signedIn.status(), 200);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      page.on("response", (response) => {
        if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
      });
      const base = `/api/agent-threads/${seed.thread_id}`;
      await page.goto(
        `${origin}/tests/fixtures/agentThreads.html?personal#/agent/${seed.thread_id}`,
      );
      const composer = page.locator(".chat-composer");
      const input = composer.locator('input[type="file"]');
      const file = {
        name: "notes.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("private attachment"),
      };
      const upload = async () => {
        const response = page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname === `${base}/attachments` &&
            response.request().method() === "POST",
        );
        await input.setInputFiles(file);
        const uploaded = await response;
        assert.equal(uploaded.status(), 200, await uploaded.text());
        await composer.locator(".chat-attachment-chip.ready").waitFor();
        return uploaded.json();
      };
      const first = await upload();
      const removed = page.waitForResponse(
        (response) =>
          response.request().method() === "DELETE" &&
          new URL(response.url()).pathname ===
            `${base}/attachments/${first.attachment.attachment_id}`,
      );
      await composer.locator(".chat-attachment-chip.ready").getByRole("button").click();
      assert.equal((await removed).status(), 200);
      await composer.locator(".chat-attachment-chip").waitFor({ state: "detached" });
      const second = await upload();
      await composer.getByRole("textbox", { name: "Message" }).fill("Read this");
      const sent = page.waitForResponse(
        (response) => new URL(response.url()).pathname === `${base}/turns`,
      );
      await composer.getByRole("button", { name: "Send", exact: true }).click();
      const admitted = await sent;
      assert.equal(admitted.status(), 202, await admitted.text());
      const { operation_id } = await admitted.json();
      const taskResponse = await page.request.get(`${origin}${base}/tasks/${operation_id}`);
      assert.equal(taskResponse.status(), 200);
      const task = await taskResponse.json();
      // Admission renews retention while preserving the staged file's metadata.
      assert.deepEqual(task.request.attachments, [
        { ...second.attachment, expires_at: task.request.attachments[0]?.expires_at },
      ]);
      assert.equal(task.request.message, "Read this");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
      assert.deepEqual(errors, []);
      assert.doesNotMatch(logs, /Traceback|ERROR/);
    } finally {
      await browser?.close();
      await server?.close();
      if (backend.exitCode === null) {
        const exited = once(backend, "exit");
        backend.kill("SIGTERM");
        await exited;
      }
    }
  },
);
