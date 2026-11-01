import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

const now = Date.now();
const ago = (minutes) => new Date(now - minutes * 60_000).toISOString();

const threads = [
  {
    thread_id: "space",
    kind: "space",
    project_id: null,
    project_name: null,
    agent_name: "Pip",
    unread: false,
    last_activity_at: ago(0),
    live_state: "idle",
    browser_requested: true,
  },
  {
    thread_id: "moss",
    kind: "project",
    project_id: "p1",
    project_name: "Sparse attention",
    agent_name: "Moss",
    unread: true,
    last_activity_at: ago(12),
    live_state: "needs_you",
    browser_requested: true,
  },
  {
    thread_id: "biscuit",
    kind: "project",
    project_id: "p2",
    project_name: "Tokenizer ablations",
    agent_name: "Biscuit",
    unread: false,
    last_activity_at: ago(1),
    live_state: "running",
    browser_requested: true,
  },
  {
    thread_id: "juniper",
    kind: "project",
    project_id: "p3",
    project_name: "Long-context eval",
    agent_name: "Juniper",
    unread: false,
    last_activity_at: ago(60 * 50),
    live_state: "idle",
    browser_requested: true,
  },
  {
    thread_id: "gone",
    kind: "project",
    project_id: "p4",
    project_name: "Left project",
    agent_name: "Orlando",
    unread: false,
    last_activity_at: null,
    live_state: "closed",
    browser_requested: true,
  },
];

const message = (id, role, text, operation, extra = {}) => ({
  message_id: id,
  operation_id: operation,
  role,
  text,
  timestamp: ago(30),
  native_session_id: null,
  provider: "claude",
  model: "opus",
  reasoning: "high",
  execution_machine: null,
  applied_revision: null,
  mode: "work",
  graph_update: null,
  trigger: "human",
  attachments: [],
  turn_outcome: null,
  ...extra,
});

const transcript = (chatId, messages) => ({
  chat_id: chatId,
  kind: "project_chat",
  node_id: null,
  title: chatId,
  updated_at: ago(0),
  message_count: messages.length,
  last_message_preview: "",
  graph_target: { kind: "main" },
  messages,
});

const command = (id, phase, second, text) => ({
  event_id: second,
  operation_id: "op-space",
  created_at: new Date(Date.UTC(2026, 10, 1, 8, 0, second)).toISOString(),
  level: "info",
  message: text ?? id,
  event_kind: "command",
  command_id: id,
  command_phase: phase,
});

const settled = (operationId, events = []) => ({
  operation_id: operationId,
  active: false,
  settled: true,
  paused: false,
  can_resume: false,
  can_retry: false,
  can_steer: false,
  steer_turn_id: null,
  status_message: "Done",
  request: {},
  events,
});

const spaceTranscript = transcript("space", [
  message("m1", "user", "Anything need me this morning?", "op-space"),
  message(
    "m2",
    "assistant",
    "**Sparse attention.** [sa-v2-lr3e4](#/projects/p1?view=runs) finished overnight. Validation loss at 32k context fell from 2.31 to 2.18. Branch `sparse-v2` is ready to merge.\n\n**Tokenizer ablations.** 2 of 4 runs are still training. I'll check them at 18:00.\n\n**Long-context eval.** Nothing new.",
    "op-space",
  ),
]);

const mossTranscript = transcript("moss", [
  message("s1", "user", "Morning check-in", "op-check", { trigger: "schedule" }),
  message(
    "s2",
    "assistant",
    "[sa-v3-warmup](#/projects/p1?view=runs) passed step 20k with no loss spike. This supports [H2 · Warmup prevents loss spikes](#/projects/p1?view=dag).",
    "op-check",
  ),
  message("s3", "user", "Good. Mark H2 supported and start the 64k run.", "op-moss", {
    source: "voice",
  }),
  message(
    "s4",
    "assistant",
    "Started the 64k run on the GPU machine. Changing H2's standing needs your approval.",
    "op-moss",
    { timestamp: ago(8) },
  ),
]);
const mossNewest = mossTranscript.messages.at(-1).timestamp;

const card = {
  question_id: "q1",
  owner_kind: "chat",
  owner_id: "moss",
  operation_id: "op-moss",
  question: "H2's evidence now covers both schedules.",
  choices: [],
  multiple: false,
  state: "pending",
  answer: null,
  chosen_choices: [],
  resolved_by: null,
  resolved_at: null,
  withdrawn_readonly: false,
  can_answer: true,
  created_at: ago(5),
  agent_waiting: false,
  answer_continues: null,
  capability: "work_auto",
  card: {
    kind: "proposal",
    action: { kind: "proposal", project_id: "p1", proposal_id: "pr-1", decision: "approved" },
    pinned: { title: "H2 standing → supported", proposal_state: "pending" },
    pinned_sha256: "sha-1",
    expires_at: new Date(now + 3_600_000).toISOString(),
    state: "pending",
  },
};

const readiness = {
  provider: "claude",
  label: "Claude",
  installed: true,
  authenticated: true,
  binary_path: "/bin/claude",
  path_state: "resolved",
  models: [{ id: "opus", label: "opus", reasoning: ["high"] }],
  runtimes: [],
  default_runtime: "cli",
};

const schedules = [
  {
    schedule_id: "s-a",
    at_local: "2026-11-01T18:00",
    timezone: "UTC",
    note: "Tokenizer ablations: check the 4 runs",
    state: "active",
  },
  {
    schedule_id: "s-b",
    cron: "0 9 * * 1-5",
    timezone: "UTC",
    note: "Morning summary",
    state: "active",
  },
];

/** Answers every API route the Agent view reads, and records each write. */
async function mockApi(page, writes) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = decodeURIComponent(url.pathname);
    const method = request.method();
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (method !== "GET") {
      writes.push({ method, path, body: request.postData() && JSON.parse(request.postData()) });
    }
    if (path === "/api/agent-threads") return json({ threads });
    if (path === "/api/resource-watermarks")
      return json({
        epoch: "e",
        as_of: new Date().toISOString(),
        projects: [],
        unavailable: [],
        agent_threads: { version: "1" },
      });
    if (path === "/api/agent-threads/space/transcript") return json(spaceTranscript);
    if (path === "/api/projects/p1/chats/moss") return json(mossTranscript);
    if (/^\/api\/projects\/p\d\/chats\/[^/]+$/.test(path))
      return json(transcript(path.split("/").at(-1), []));
    if (path === "/api/projects/p1/chats/moss/questions") return json([card]);
    if (path.endsWith("/questions")) return json([]);
    if (path === "/api/agent-threads/space/tasks/op-space")
      return json(
        settled("op-space", [
          command("a", "start", 0, "status --project sparse-attention"),
          command("a", "exit", 2),
          command("b", "start", 2, "graph log sparse-v2 --since 18h"),
          command("b", "exit", 5),
          command("c", "start", 5, "compute jobs --project tokenizer-ablations"),
          command("c", "exit", 9),
          command("d", "start", 9, "tail -n 40 runs/sa-v2-lr3e4/train.log"),
          command("d", "exit", 12),
        ]),
      );
    if (/\/tasks\/[^/]+$/.test(path) && method === "GET")
      return json(settled(path.split("/").at(-1)));
    if (/^\/api\/agent-threads\/[^/]+\/schedules$/.test(path))
      return json({ schedules: path.includes("/space/") ? schedules : schedules.slice(1) });
    if (method === "DELETE") return route.fulfill({ status: 204 });
    if (path === "/api/agent-threads/space/turns") return json({ operation_id: "op-new" }, 202);
    if (/\/read$/.test(path)) {
      const id = path.split("/").at(-2);
      return json({ ...threads.find((thread) => thread.thread_id === id), unread: false });
    }
    if (path === "/api/approval-cards/q1") return json({ outcome: "approved" });
    if (/^\/api\/projects\/p\d\/readiness$/.test(path))
      return json({
        provider_readiness: { local: { claude: readiness } },
        agent_profiles: {
          project_chat: { provider: "claude", model: "opus", reasoning: "high", run_on: "local" },
        },
      });
    if (path === "/api/space/machines")
      return json({
        machines: [{ machine_id: "local", host: "", provider_readiness: { claude: readiness } }],
      });
    return json({ detail: "not in this fixture" }, 404);
  });
}

const shots = process.env.RCP_AGENT_SHOTS;

test("the space Agent view lists threads, opens one, and resolves a card by tap", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const base = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/agentThreads.html`;
    if (shots) mkdirSync(shots, { recursive: true });

    // Desktop: rail and thread side by side.
    const writes = [];
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await mockApi(page, writes);
    await page.goto(`${base}#/agent`);
    const rail = page.getByRole("listbox", { name: "Threads" });
    await rail.getByRole("option").first().waitFor();
    assert.equal(
      await page.getByRole("button", { name: "Agent", exact: true }).getAttribute("aria-pressed"),
      "true",
    );
    // The Space thread leads, a closed thread is gone, and the open one is the Space thread.
    const options = rail.getByRole("option");
    assert.equal(await options.count(), 4);
    assert.match(await options.first().getAttribute("aria-label"), /^Pip,/);
    assert.equal(await options.first().getAttribute("aria-selected"), "true");
    // Only Moss carries a mark: it needs the member, which outranks its unread reply.
    assert.equal(await rail.locator(".thread-row-icon > span").count(), 1);
    const thread = page.getByRole("region", { name: "Pip" });
    await thread.locator(".chat-progress").waitFor();
    assert.equal(await thread.locator(".chat-progress li").count(), 4);
    assert.equal(await thread.locator('.chat-markdown a[href^="#/projects/"]').count(), 1);
    await page.waitForTimeout(150);
    if (shots) await page.screenshot({ path: `${shots}/agent-space-1440.png` });

    // Upcoming lists both schedules; Cancel deletes one.
    const upcoming = thread.getByRole("button", { name: "2 upcoming" });
    await upcoming.click();
    const popover = page.getByRole("dialog", { name: "Upcoming" });
    assert.equal(await popover.locator(".upcoming-row").count(), 2);
    if (shots) await page.screenshot({ path: `${shots}/agent-upcoming-1440.png` });
    await popover.locator(".upcoming-row").first().getByRole("button").click();
    await page.keyboard.press("Escape");
    assert.ok(writes.some((write) => write.method === "DELETE" && write.path.endsWith("/s-a")));

    // A send is one Work turn with the browser's timezone.
    await thread.getByRole("textbox", { name: "Message" }).fill("Plan the 64k sweep");
    await thread.getByRole("button", { name: "Send", exact: true }).click();
    await page.waitForFunction(
      () => document.querySelector('textarea[aria-label="Message"]').value === "",
    );
    const turn = writes.find((write) => write.path === "/api/agent-threads/space/turns");
    assert.equal(turn.body.message, "Plan the 64k sweep");
    assert.equal(typeof turn.body.timezone, "string");

    // The mic binds voice to the open thread.
    await thread.getByRole("button", { name: "Talk" }).click();
    assert.deepEqual(await page.evaluate(() => window.voiceOpened), ["space"]);

    // The rail folds with the Agents list's control and opens again.
    await page.getByRole("button", { name: "Collapse agent list" }).click();
    assert.equal(await rail.isHidden(), true);
    await page.getByRole("button", { name: "Expand agent list" }).click();
    assert.equal(await rail.isVisible(), true);

    // A project thread shows its card; Approve sends the pinned hash it showed.
    await options.nth(1).click();
    const mossThread = page.getByRole("region", { name: "Moss" });
    const approve = mossThread.getByRole("button", { name: "Approve" });
    await approve.waitFor();
    assert.ok(writes.some((write) => write.path === "/api/agent-threads/moss/read"));
    await mossThread.getByRole("button", { name: "See details" }).click();
    assert.equal(await mossThread.locator(".approval-details dd").count(), 2);
    await mossThread.getByRole("button", { name: "See details" }).click();
    await page.waitForTimeout(150);
    if (shots) await page.screenshot({ path: `${shots}/agent-project-thread-1440.png` });
    await approve.click();
    await page.waitForFunction(() => !document.querySelector(".approval-actions .spin"));
    const decision = writes.find((write) => write.path === "/api/approval-cards/q1");
    assert.deepEqual(decision.body, { decision: "approve", pinned_sha256: "sha-1" });
    assert.deepEqual(errors, []);
    await page.close();

    // Phone: the list, then one thread under a back control; the header stays one line.
    const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
    await mockApi(phone, []);
    await phone.goto(`${base}#/agent`);
    const phoneRail = phone.getByRole("listbox", { name: "Threads" });
    await phoneRail.getByRole("option").first().waitFor();
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390);
    const header = phone.locator(".space-header");
    assert.ok((await header.boundingBox()).height <= 64, "The header is one line");
    assert.equal(await phone.locator(".landing-identity-trigger-copy").isVisible(), false);
    assert.equal(await phone.getByRole("region", { name: "Pip" }).count(), 0);
    await phone.waitForTimeout(150);
    if (shots) await phone.screenshot({ path: `${shots}/agent-threads-390.png` });
    await phoneRail.getByRole("option").nth(1).click();
    const phoneThread = phone.getByRole("region", { name: "Moss" });
    await phoneThread.getByRole("button", { name: "Approve" }).waitFor();
    assert.equal(await header.isVisible(), false);
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390);
    await phone.waitForTimeout(150);
    if (shots) await phone.screenshot({ path: `${shots}/agent-thread-390.png` });
    const composer = await phoneThread.locator(".chat-composer").boundingBox();
    assert.ok(composer.x >= 0 && composer.x + composer.width <= 390);
    await phoneThread.getByRole("button", { name: "Threads" }).click();
    await phoneRail.waitFor();
    await phone.close();
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("inside a project, Agents opens the member's thread with the team list one press away", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await mockApi(page, []);
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/agentThreads.html?view=project`,
    );
    const thread = page.getByRole("region", { name: "Moss" });
    await thread.getByRole("button", { name: "Approve" }).waitFor();
    const list = page.getByRole("complementary", { name: "Project conversations" });
    assert.equal(await list.isHidden(), true);
    await page.waitForTimeout(150);
    if (shots) await page.screenshot({ path: `${shots}/project-agents-1440.png` });
    await page.getByRole("button", { name: "Expand conversation list" }).click();
    const options = list.getByRole("option");
    assert.match(await options.first().getAttribute("aria-label"), /^Moss,/);
    assert.equal(await options.count(), 3);
    await page.waitForTimeout(150);
    if (shots) await page.screenshot({ path: `${shots}/project-agents-team-list-1440.png` });
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("a send, a read mark and history reads stay with the thread they belong to", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const writes = [];
    await mockApi(page, writes);
    // Routes added later answer first: a held Space send, a slow Moss transcript,
    // and a history turn whose read always fails.
    let failSend;
    const sendHeld = new Promise((resolve) => (failSend = resolve));
    await page.route("**/api/agent-threads/space/turns", async (route) => {
      await sendHeld;
      await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
    });
    await page.route("**/api/projects/p1/chats/moss", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 400));
      await route.fallback();
    });
    let historyReads = 0;
    await page.route("**/api/projects/p1/tasks/op-check", async (route) => {
      historyReads += 1;
      await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/agentThreads.html#/agent`,
    );
    const space = page.getByRole("region", { name: "Pip" });
    await space.locator(".chat-progress").waitFor();
    await space.getByRole("textbox", { name: "Message" }).fill("Plan the sweep");
    await space.getByRole("button", { name: "Send", exact: true }).click();

    // Switch while the Space send is in flight, then let it fail.
    await page.getByRole("listbox", { name: "Threads" }).getByRole("option").nth(1).click();
    const moss = page.getByRole("region", { name: "Moss" });
    failSend();
    await moss.getByRole("button", { name: "Approve" }).waitFor();
    await page.waitForTimeout(300);
    assert.equal(await moss.getByRole("button", { name: "Resend" }).count(), 0);
    await moss.getByRole("textbox", { name: "Message" }).fill("Next");
    assert.equal(await moss.getByRole("button", { name: "Send", exact: true }).isEnabled(), true);
    assert.equal(await moss.locator(".node-chat-line").getByText("Plan the sweep").count(), 0);

    // Moss is marked read only through its own newest message.
    const reads = writes.filter((write) => write.path.endsWith("/read"));
    assert.deepEqual(reads, [
      { method: "POST", path: "/api/agent-threads/moss/read", body: { read_through: mossNewest } },
    ]);

    // A failing history read is retried with the transcript, not in a tight loop.
    await page.waitForTimeout(1500);
    assert.ok(historyReads >= 1 && historyReads <= 4, `history reads: ${historyReads}`);

    // An upload that finishes after switching threads stays owned by its old route.
    let releaseUpload;
    const uploadHeld = new Promise((resolve) => (releaseUpload = resolve));
    await page.route("**/api/projects/p1/chats/moss/attachments", async (route) => {
      await uploadHeld;
      await route.fulfill({
        json: {
          attachment_set_id: "old-set",
          attachment: {
            attachment_id: "old-file",
            name: "old.txt",
            media_type: "text/plain",
            size: 3,
            expires_at: new Date(now + 86400_000).toISOString(),
          },
        },
      });
    });
    await moss.locator('input[type="file"]').setInputFiles({
      name: "old.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("old"),
    });
    await moss.locator(".chat-attachment-chip.preparing").waitFor();

    // Back in the Space thread, its failed send waits there with Resend.
    await page.getByRole("listbox", { name: "Threads" }).getByRole("option").first().click();
    await space.getByRole("button", { name: "Resend" }).waitFor();
    const cleaned = page.waitForRequest(
      (request) =>
        request.method() === "DELETE" &&
        new URL(request.url()).pathname === "/api/projects/p1/chats/moss/attachments/old-file",
    );
    releaseUpload();
    await cleaned;
    assert.equal(await space.locator(".chat-attachment-chip").count(), 0);
    // The failed line stays in its thread, and the composer keeps a copy.
    assert.equal(
      await space.locator(".node-chat-line.unsent").getByText("Plan the sweep").count(),
      1,
    );
    assert.equal(
      await space.getByRole("textbox", { name: "Message" }).inputValue(),
      "Plan the sweep",
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("thread windows share artifact rendering, comments, uploads, and in-place card outcomes", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const writes = [];
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await mockApi(page, writes);
    const artifact = {
      artifact_id: "report",
      name: "report.html",
      media_type: "text/html",
      view: "html",
      size_bytes: 42,
      available: true,
      unavailable_reason: null,
      can_open: true,
      can_download: true,
      can_keep: true,
      can_discuss: true,
    };
    let approved = false;
    const windowTranscript = (space) => {
      const op = space ? "op-space" : "op-moss";
      const messages = [
        message("request", "user", "Inspect the report", op, {
          timestamp: ago(10),
          source: space ? "handoff" : "voice",
        }),
        message(
          "answer",
          "assistant",
          `The result is ready.\n\n![Report](/workspace/.research/turns/${op}/artifacts/report.html)`,
          op,
          { timestamp: ago(1) },
        ),
      ];
      if (!space && approved)
        messages.push(
          message("card-delivery", "user", "Approved the proposed action", "op-followup", {
            timestamp: ago(0),
            question_id: "q1",
          }),
        );
      return transcript(space ? "space" : "moss", messages);
    };
    await page.route("**/api/projects/p1/chats/moss", (route) =>
      route.fulfill({ json: windowTranscript(false) }),
    );
    await page.route("**/api/agent-threads/space/transcript", (route) =>
      route.fulfill({ json: windowTranscript(true) }),
    );
    await page.route("**/api/projects/p1/chats/moss/questions", (route) =>
      route.fulfill({
        json: [
          {
            ...card,
            ...(approved
              ? { state: "answered", answer: "approved", card: { ...card.card, state: "approved" } }
              : {}),
          },
        ],
      }),
    );
    await page.route("**/api/approval-cards/q1", (route) => {
      approved = true;
      writes.push({ path: "/api/approval-cards/q1", body: route.request().postDataJSON() });
      return route.fulfill({ json: { outcome: "approved" } });
    });
    await page.route(
      /\/api\/(?:projects\/p1|agent-threads\/space)\/tasks\/op-(?:moss|space)$/,
      (route) => {
        const op = new URL(route.request().url()).pathname.split("/").at(-1);
        return route.fulfill({ json: { ...settled(op), result: { artifacts: [artifact] } } });
      },
    );
    await page.route("**/api/projects/p1/artifacts/report/state", (route) =>
      route.fulfill({
        json: {
          artifact_id: "report",
          current_version: "one",
          version_number: 1,
          can_comment: true,
          can_keep: true,
          viewer_url: "/api/projects/p1/tasks/op-moss/artifacts/report/viewer",
        },
      }),
    );
    await page.route(/\/artifacts\/report\/viewer(?:\?.*)?$/, (route) =>
      route.fulfill({ contentType: "text/html", body: "<p>Rendered report</p>" }),
    );
    await page.route(/\/attachments$/, (route) => {
      writes.push({
        path: new URL(route.request().url()).pathname,
        body: route.request().postData(),
      });
      return route.fulfill({
        json: {
          attachment_set_id: "set-1",
          attachment: {
            attachment_id: "file-1",
            name: "notes.txt",
            media_type: "text/plain",
            size: 5,
            expires_at: new Date(now + 86400_000).toISOString(),
          },
        },
      });
    });
    let admitProjectSend;
    let sawProjectSend;
    const projectSendHeld = new Promise((resolve) => (admitProjectSend = resolve));
    const projectSendStarted = new Promise((resolve) => (sawProjectSend = resolve));
    await page.route("**/api/projects/p1/tasks/project_chat", async (route) => {
      writes.push({
        path: "/api/projects/p1/tasks/project_chat",
        body: route.request().postDataJSON(),
      });
      sawProjectSend();
      await projectSendHeld;
      return route.fulfill({ json: { operation_id: "op-new" } });
    });
    const base = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/agentThreads.html`;
    await page.goto(`${base}#/agent/moss`);
    const project = page.getByRole("region", { name: "Moss" });
    const projectFrame = project.locator(".chat-inline-artifact-frame");
    await projectFrame.waitFor();
    assert.equal(
      await project.locator('.chat-human-message [aria-label="Said by voice"]').count(),
      1,
    );
    assert.match(
      await projectFrame.getAttribute("src"),
      /\/api\/projects\/p1\/tasks\/op-moss\/artifacts\/report\/viewer/,
    );
    assert.equal(
      await project
        .locator(".approval-card")
        .evaluate((card) =>
          Boolean(
            card.compareDocumentPosition(document.querySelector(".chat-annotatable-answer")) &
            Node.DOCUMENT_POSITION_FOLLOWING,
          ),
        ),
      true,
    );
    await project.getByRole("button", { name: "Approve", exact: true }).click();
    await project.locator(".approval-card[data-state='approved']").waitFor();
    assert.equal(await project.locator(".node-chat-line.human").count(), 1);
    assert.equal(
      await project.getByText("Approved the proposed action", { exact: true }).count(),
      0,
    );
    await project.getByRole("button", { name: "Comment on this answer" }).click();
    const selection = page.getByRole("textbox", { name: "Select answer text" });
    await selection.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
    await page.getByRole("button", { name: "Comment on selection" }).click();
    await page.getByRole("textbox", { name: "Comment", exact: true }).fill("Show the comparison");
    await page.getByRole("textbox", { name: "Comment", exact: true }).press("Enter");
    await project
      .locator('input[type="file"]')
      .setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("notes") });
    await project.locator(".chat-attachment-chip.ready").waitFor();
    await project.getByRole("textbox", { name: "Message" }).fill("Continue");
    await project.getByRole("button", { name: "Send", exact: true }).click();
    await projectSendStarted;
    await page.evaluate(() => {
      location.hash = "#/agent/space";
    });
    const space = page.getByRole("region", { name: "Pip" });
    await space.getByRole("button", { name: "Comment on this answer" }).click();
    await page
      .getByRole("textbox", { name: "Select answer text" })
      .press(process.platform === "darwin" ? "Meta+A" : "Control+A");
    await page.getByRole("button", { name: "Comment on selection" }).click();
    await page
      .getByRole("textbox", { name: "Comment", exact: true })
      .fill("Keep the Space comparison");
    await page.getByRole("textbox", { name: "Comment", exact: true }).press("Enter");
    admitProjectSend();
    // The completed project's comments and draft clear while Space keeps its own comment.
    await page.waitForFunction(
      () =>
        !sessionStorage.getItem("rcp:member-draft:alex:agent-window-comments:p1:moss") &&
        !localStorage.getItem("rcp:member-draft:alex:agent-window:p1:moss"),
    );
    assert.equal(await space.getByRole("button", { name: "1 annotation" }).count(), 1);
    const sent = writes.find((write) => write.path === "/api/projects/p1/tasks/project_chat");
    assert.ok(sent.body.message.includes("Show the comparison"));
    assert.equal(sent.body.attachment_set_id, "set-1");
    assert.equal(typeof sent.body.attachment_client_id, "string");
    assert.ok(writes.some((write) => write.path === "/api/projects/p1/chats/moss/attachments"));
    const spaceFrame = space.locator(".chat-inline-artifact-frame");
    await spaceFrame.waitFor();
    assert.equal(
      await space
        .locator('.chat-human-message [aria-label="Handed over from your Space thread"]')
        .count(),
      1,
    );
    assert.match(
      await spaceFrame.getAttribute("src"),
      /\/api\/agent-threads\/space\/tasks\/op-space\/artifacts\/report\/viewer/,
    );
    await space.locator(".chat-inline-artifact").getByRole("button", { name: "Expand" }).click();
    const viewer = page.locator(".artifact-viewer iframe");
    await viewer.waitFor();
    assert.match(
      await viewer.getAttribute("src"),
      /\/api\/agent-threads\/space\/tasks\/op-space\/artifacts\/report\/viewer/,
    );
    assert.equal(page.context().pages().length, 1);
    await page.getByRole("button", { name: "Close viewer", exact: true }).click();

    assert.equal(await space.locator('[data-artifact-action="keep"]').count(), 0);
    assert.equal(await space.getByRole("button", { name: "Comment on a part" }).count(), 0);
    await space
      .locator('input[type="file"]')
      .setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("notes") });
    await space.locator(".chat-attachment-chip.ready").waitFor();
    assert.ok(writes.some((write) => write.path === "/api/agent-threads/space/attachments"));
    await space.getByRole("textbox", { name: "Message" }).fill("Read this file");
    await space.getByRole("button", { name: "Send", exact: true }).click();
    await page.waitForFunction(
      () => document.querySelector('textarea[aria-label="Message"]').value === "",
    );
    const spaceSent = writes.find((write) => write.path === "/api/agent-threads/space/turns");
    assert.ok(spaceSent.body.message.includes("Keep the Space comparison"));
    assert.equal(spaceSent.body.attachment_set_id, "set-1");
    assert.equal(typeof spaceSent.body.attachment_client_id, "string");
    assert.equal("artifact_context" in spaceSent.body, false);
    assert.equal("references" in spaceSent.body, false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
