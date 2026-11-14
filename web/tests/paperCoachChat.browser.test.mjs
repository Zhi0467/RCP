import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createPaperTestServer, openPaper } from "./paperTestServer.mjs";

const SHOTS = process.env.RCP_PAPER_COACH_SHOTS;

let server, browser;
before(async () => {
  server = await createPaperTestServer();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

const now = new Date().toISOString();
const turn = {
  operation_id: "turn-1",
  kind: "project_chat",
  status: "succeeded",
  native_session_id: "s1",
  request: {
    chat_id: "coach-1",
    chat_scope: "project",
    focus: "paper",
    mode: "work",
    message: "Tighten the method section and fix every figure reference.",
  },
  result: { messages: ["Rewrote the method and fixed the figure references."] },
  created_at: now,
  updated_at: now,
  active: false,
  attempt: 1,
};
const summary = {
  chat_id: "coach-1",
  graph_target: { kind: "main" },
  graph_title: "Main",
  kind: "project_chat",
  node_id: null,
  title: "Coach",
  updated_at: now,
  message_count: 2,
  last_message_preview: "",
  conversation_kind: "chat",
  orchestrator_episode_id: null,
  focus: "paper",
};
const method = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`);

function seed(fixture) {
  fixture.chats.push(summary);
  fixture.turns["turn-1"] = {
    list: {
      before_version_id: "v-before",
      after_version_id: "v-after",
      files: [
        { path: "posts/two-shortcuts.html", kind: "write", clash: true, added: 1, removed: 1 },
        { path: "sections/method.md", kind: "write", clash: false, added: 2, removed: 1 },
      ],
    },
    files: {
      "sections/method.md": {
        path: "sections/method.md",
        kind: "write",
        clash: false,
        before: method.join("\n") + "\n",
        coach:
          [...method.slice(0, 18), "coupling", "a link", ...method.slice(19)].join("\n") + "\n",
        current: null,
        hunks: [{ old_start: 19, new_start: 19, old: ["line 19"], new: ["coupling", "a link"] }],
      },
    },
  };
}

for (const width of [1280, 390])
  test(`a finished Coach turn folds its files; a row's diff line takes a comment the chat sends at ${width}px`, async (t) => {
    const { page, errors } = await openPaper(t, {
      browser,
      origin: server.origin,
      width,
      init: `window.paperCoachFixture = { tasks: [${JSON.stringify(turn)}], started: [] };`,
      configure: seed,
    });
    await page.getByRole("button", { name: "Coach", exact: true }).first().click();
    const folded = page.getByRole("button", { name: /2 files changed/ });
    await folded.waitFor();
    assert.equal(await folded.getAttribute("aria-expanded"), "false");
    await folded.click();
    const rows = page.locator(".paper-turn-file");
    assert.deepEqual(await rows.locator(".paper-turn-path").allTextContents(), [
      "posts/two-shortcuts.html",
      "sections/method.md",
    ]);
    assert.match(await rows.first().getAttribute("class"), /clash/);

    await rows.nth(1).click();
    const diff = page.getByRole("complementary", { name: "Changes to sections/method.md" });
    await diff.waitFor();
    const added = diff.getByRole("button", { name: "Comment on new line 20" });
    await added.click();
    await page.getByRole("textbox", { name: "Comment", exact: true }).fill("Keep the old wording.");
    await page.getByRole("button", { name: "Send to agent" }).click();
    await page.waitForFunction(() => window.paperCoachFixture.started.length === 1);
    const [started] = await page.evaluate(() => window.paperCoachFixture.started);
    assert.equal(started.request.focus, "paper");
    assert.equal(started.request.reasoning, "high");
    assert.equal(started.request.chat_id, "coach-1");
    assert.deepEqual(started.request.comment_thread_ids, ["c1"]);

    const thread = await page.evaluate(() =>
      fetch("/api/projects/p/comment-threads?target_kind=reply&chat_id=coach-1")
        .then((response) => response.json())
        .then((page) => page.threads[0]),
    );
    const { kind, path, label, text } = thread.anchor;
    assert.deepEqual(
      [thread.target_kind, thread.target_id, kind, path, label, text],
      ["reply", "turn-1", "element", "sections/method.md", "new line 20", "a link"],
    );

    const chat = await page.locator(".paper-coach-body > .chat-dock").boundingBox();
    const pane = await diff.boundingBox();
    if (width === 390) assert.ok(pane.y + pane.height <= chat.y + 1, "the diff stacks above");
    else assert.ok(pane.x + pane.width <= chat.x + 1, "the diff sits beside the chat");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/coach-${width}.png` });
    await diff.getByRole("button", { name: "Close diff" }).click();
    await diff.waitFor({ state: "detached" });
    assert.deepEqual(errors, []);
  });
