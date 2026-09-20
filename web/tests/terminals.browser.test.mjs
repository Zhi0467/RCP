import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

let server;
let browser;
let origin;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

const protectionNotice =
  "Canonical-state protection is unavailable on this machine. There is no filesystem fence around canonical state.";
const repositories = [
  {
    repository_id: "code",
    machine_id: "local",
    backend_id: "systemd_user",
    backend_name: "systemd user manager",
    containment: "mirrored",
    reason: "Local Linux supports the canonical-state mount profile.",
    path: "/srv/project/code",
    eligible: true,
    unavailable_reason: null,
    running_work: [],
  },
  {
    repository_id: "notes",
    machine_id: "local",
    backend_id: "systemd_user",
    backend_name: "systemd user manager",
    containment: "mirrored",
    reason: "Local Linux supports the canonical-state mount profile.",
    path: "/srv/project/notes",
    eligible: true,
    unavailable_reason: null,
    running_work: [],
  },
  {
    repository_id: "remote",
    machine_id: "remote",
    backend_id: null,
    backend_name: null,
    containment: null,
    reason: "PTY-over-SSH transport is not built.",
    path: "/remote/code",
    eligible: false,
    unavailable_reason: "PTY-over-SSH transport is not built.",
    running_work: [],
  },
];
async function fixture(t, { failLaunch = false, cooperative = false } = {}) {
  let projectedRepositories = repositories.map((repository) =>
    cooperative && repository.eligible
      ? {
          ...repository,
          containment: "cooperative",
          backend_id: "pty",
          backend_name: "Local PTY",
          reason: protectionNotice,
        }
      : repository,
  );
  const context = await browser.newContext();
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  const input = [];
  let sessions = [];
  let opens = 0;
  let connections = 0;
  page.on("pageerror", (error) => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.route("**/api/projects/alpha/terminals**", async (route) => {
    const request = route.request();
    if (request.url().endsWith("/repositories"))
      return route.fulfill({ json: projectedRepositories });
    if (request.method() === "POST") {
      opens++;
      if (failLaunch)
        return route.fulfill({
          status: 503,
          json: { detail: "Member terminals require systemd-run; executable not found." },
        });
      const repository = projectedRepositories.find(
        (repo) => repo.repository_id === request.postDataJSON().repository_id,
      );
      const session = {
        session_id: `${repository.repository_id}-session`,
        repository_id: repository.repository_id,
        path: repository.path,
        state: "live",
        containment: repository.containment,
        protection_notice: repository.containment === "cooperative" ? protectionNotice : null,
        running_work:
          repository.repository_id === "code"
            ? [{ operation_id: "work-1", title: "Update results" }]
            : [],
      };
      sessions.push(session);
      return route.fulfill({ json: session });
    }
    if (request.method() === "DELETE") {
      sessions = sessions.filter((session) => !request.url().endsWith(`/${session.session_id}`));
      return route.fulfill({ json: { ended: true } });
    }
    return route.fulfill({ json: sessions });
  });
  await page.routeWebSocket("**/terminals/*/ws", (socket) => {
    connections++;
    socket.send(Buffer.from("repository $ "));
    socket.onMessage((message) => {
      const event = JSON.parse(message);
      input.push(event);
      if (event.type === "input") socket.send(Buffer.from(event.data));
    });
  });
  await page.goto(`${origin}/tests/fixtures/terminals.html`);
  await page.getByRole("button", { name: "Terminals", exact: true }).click();
  await page.getByRole("button", { name: "code /srv/project/code", exact: true }).waitFor();
  return {
    page,
    input,
    opens: () => opens,
    connections: () => connections,
    setRepositories: (value) => {
      projectedRepositories = value;
    },
  };
}

test("Terminals hashes restore the project destination", async () => {
  const { parseProjectHash, projectHashAfterViewChange } =
    await server.ssrLoadModule("/src/experimentBoard.ts");
  assert.equal(parseProjectHash("#/projects/alpha?view=terminals").view, "terminals");
  assert.equal(
    projectHashAfterViewChange("#/projects/alpha", "terminals"),
    "#/projects/alpha?view=terminals",
  );
  assert.equal(
    projectHashAfterViewChange("#/projects/alpha?view=terminals", "scientific"),
    "#/projects/alpha",
  );
});

test("sessions reconnect after navigating, resize and type, expose Work on both rows, and end individually", async (t) => {
  const { page, input, opens, connections } = await fixture(t);
  assert.equal(
    await page.getByRole("button", { name: /remote \/remote\/code/ }).isDisabled(),
    true,
  );
  await page.getByRole("button", { name: "code /srv/project/code", exact: true }).click();
  await page
    .locator(".terminal-work-strip")
    .filter({ hasText: "Work running: Update results" })
    .waitFor();
  await page.locator(".xterm-helper-textarea").fill("git status");
  await page.waitForFunction(() => !document.querySelector(".terminal-connection"));
  await page.locator(".xterm-helper-textarea").press("Enter");
  assert.ok(input.some((event) => event.type === "resize" && event.cols > 0 && event.rows > 0));
  assert.ok(input.some((event) => event.type === "input"));
  const pasted = "a".repeat(16383) + "🌱" + "b".repeat(17000);
  const inputCount = input.length;
  await page.locator(".xterm-helper-textarea").evaluate((element, text) => {
    const clipboardData = new DataTransfer();
    clipboardData.setData("text/plain", text);
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData, bubbles: true }));
  }, pasted);
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const chunks = input
    .slice(inputCount)
    .filter((event) => event.type === "input")
    .map((event) => event.data);
  assert.equal(chunks.join(""), pasted);
  assert.ok(chunks.every((chunk) => Buffer.byteLength(chunk) <= 16384));

  await page.getByRole("button", { name: "notes /srv/project/notes", exact: true }).click();
  await page.locator(".terminal-active-heading").filter({ hasText: "notes" }).waitFor();
  assert.equal(await page.locator(".terminal-work-strip").count(), 0);
  assert.equal(await page.locator(".terminal-work-mark").textContent(), "Work running");
  await page.getByRole("button", { name: "Research", exact: true }).click();
  await page.getByRole("button", { name: "Terminals", exact: true }).click();
  await page.getByRole("button", { name: "End code terminal" }).waitFor();
  await page.waitForFunction(() => !document.querySelector(".terminal-connection"));
  assert.equal(opens(), 2);
  assert.ok(connections() >= 3);
  await page.getByRole("button", { name: "End code terminal" }).click();
  await page.getByRole("button", { name: "code /srv/project/code", exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "End notes terminal" }).count(), 1);
});

test("both themes and all modes show the cooperative warning, repaint the emulator, and keep mobile controls reachable", async (t) => {
  const { page } = await fixture(t, { cooperative: true });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.getByRole("button", { name: "code /srv/project/code", exact: true }).click();
  await page.locator(".xterm-screen").waitFor();
  for (const theme of ["Aqua", "Classic"]) {
    await page.getByRole("button", { name: theme, exact: true }).click();
    for (const mode of ["Light", "Dark", "System"]) {
      await page.getByRole("button", { name: mode, exact: true }).click();
      await page.waitForFunction(
        ({ theme, mode }) =>
          document.documentElement.dataset.theme === theme.toLowerCase() &&
          document.documentElement.dataset.colorMode === (mode === "Light" ? "light" : "dark"),
        { theme, mode },
      );
      const warning = page.getByRole("alert").filter({ hasText: protectionNotice });
      assert.ok(await warning.isVisible());
      const style = await warning.evaluate((element) => {
        const actual = getComputedStyle(element);
        const tokenSample = document.createElement("div");
        tokenSample.style.color = "var(--walnut)";
        tokenSample.style.backgroundColor = "var(--amber-soft)";
        document.body.appendChild(tokenSample);
        const expected = getComputedStyle(tokenSample);
        const result = {
          readableInk: actual.color === expected.color,
          warningSurface: actual.backgroundColor === expected.backgroundColor,
          distinctSurface:
            actual.backgroundColor !== getComputedStyle(element.parentElement).backgroundColor,
          fullSize: parseFloat(actual.fontSize) >= 14,
          opacity: actual.opacity,
          icon: !!element.querySelector("svg"),
        };
        tokenSample.remove();
        return result;
      });
      assert.deepEqual(style, {
        readableInk: true,
        warningSurface: true,
        distinctSurface: true,
        fullSize: true,
        opacity: "1",
        icon: true,
      });
      await page.waitForFunction(
        () =>
          getComputedStyle(document.querySelector(".xterm-viewport")).backgroundColor ===
          getComputedStyle(document.querySelector(".terminal-active")).backgroundColor,
      );
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.waitForFunction(
    () =>
      document.documentElement.dataset.colorMode === "light" &&
      getComputedStyle(document.querySelector(".xterm-viewport")).backgroundColor ===
        getComputedStyle(document.querySelector(".terminal-active")).backgroundColor,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.getByRole("button", { name: "End code terminal" }).isVisible());
  assert.ok(await page.getByRole("alert").filter({ hasText: protectionNotice }).isVisible());
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});

test("launch failure displays the real missing systemd diagnostic", async (t) => {
  const { page } = await fixture(t, { failLaunch: true });
  await page.getByRole("button", { name: "code /srv/project/code", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "systemd-run; executable not found" }).waitFor();
  assert.equal(await page.locator(".xterm-screen").count(), 0);
  assert.equal(await page.locator(".terminal-protection-warning").count(), 0);
});

test("Terminals tab disappears when no machine can host a session and returns for cooperative support", async (t) => {
  const { page, setRepositories } = await fixture(t);
  await page.getByRole("button", { name: "Research", exact: true }).click();
  const tab = page.getByRole("button", { name: "Terminals", exact: true });
  for (const unavailable of [[repositories[2]], []]) {
    setRepositories(unavailable);
    await page.getByRole("button", { name: "Refresh project", exact: true }).click();
    await tab.waitFor({ state: "hidden" });
    assert.ok(await page.getByRole("heading", { name: "Research", exact: true }).isVisible());
    setRepositories([{ ...repositories[0], containment: "cooperative", reason: protectionNotice }]);
    await page.getByRole("button", { name: "Refresh project", exact: true }).click();
    await tab.waitFor({ state: "visible" });
  }
});
