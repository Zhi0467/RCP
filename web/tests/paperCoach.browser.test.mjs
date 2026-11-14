import assert from "node:assert/strict";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createPaperTestServer, openPaper } from "./paperTestServer.mjs";

let server, browser;
before(async () => {
  server = await createPaperTestServer();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
const open = (t, options = {}) =>
  openPaper(t, { browser, origin: server.origin, width: 390, ...options });
async function coach(page) {
  await page.getByRole("button", { name: "Coach", exact: true }).first().click();
}
async function fits(page) {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
}
async function target(locator, minimum = 44) {
  const box = await locator.boundingBox();
  assert.ok(box && box.height >= minimum && box.width >= minimum, JSON.stringify(box));
}

test("oversized UTF-8 edits leave the document editable and synchronized", async (t) => {
  const { page, room, errors } = await open(t);
  // Mount the editor with its explicit limit contract, independently of Workspace wiring.
  await page.evaluate(async () => {
    const { React, createRoot } = await import("/tests/fixtures/paperReact.ts");
    const { PaperEditor } = await import("/src/paper/PaperEditor.tsx");
    const { acquirePaperDocument } = await import("/src/paper/paperDocuments.ts");
    const owner = acquirePaperDocument(
      { backend: "limit-test", member: "member", project: "p", file: "f1" },
      "/api/projects/p",
      "Member",
    );
    document.querySelector("#root").style.display = "none";
    const host = document.body.appendChild(document.createElement("div"));
    host.id = "limited";
    function Limited() {
      const snapshot = React.useSyncExternalStore(owner.subscribe, owner.getSnapshot);
      return snapshot.ready
        ? React.createElement(PaperEditor, {
            owner,
            maxBytes: 9,
            format: "markdown",
            focus: null,
          })
        : null;
    }
    createRoot(host).render(React.createElement(Limited));
  });
  const editor = page.locator("#limited .cm-content");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content")?.textContent === "draft",
  );
  await editor.click();
  await page.keyboard.press("ControlOrMeta+End");
  const updates = room.updatesReceived;
  await page.keyboard.insertText("😀😀");
  assert.equal(await editor.textContent(), "draft");
  assert.equal(room.doc.getText("text").toString(), "draft");
  assert.equal(room.updatesReceived, updates);
  assert.ok(
    await page.locator("#limited .paper-code").evaluate((e) => e.getAnimations().length > 0),
  );
  await page.keyboard.insertText("😀");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content").textContent === "draft😀",
  );
  await room.waitForUpdates(updates + 1);
  assert.equal(room.doc.getText("text").toString(), "draft😀");
  assert.equal(await editor.getAttribute("contenteditable"), "true");
  // Undo restoring deleted text past the limit after a peer filled the room is refused too.
  // Wait out Y.UndoManager's 500 ms capture window so the deletion is its own undo step.
  await page.waitForTimeout(600);
  await page.keyboard.press("Backspace");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content").textContent === "draft",
  );
  room.doc.getText("text").insert(0, "abcd");
  await page.waitForFunction(
    () => document.querySelector("#limited .cm-content").textContent === "abcddraft",
  );
  const beforeUndo = room.updatesReceived;
  await page.keyboard.press("ControlOrMeta+z");
  assert.equal(await editor.textContent(), "abcddraft");
  assert.equal(room.doc.getText("text").toString(), "abcddraft");
  assert.equal(room.updatesReceived, beforeUndo);
  assert.deepEqual(errors, []);
});

test("the phone branch picker is a 44px target beside the coach", async (t) => {
  const { page } = await open(t);
  await coach(page);
  // The shared fixture mounts the workspace without the project-shell branch picker.
  await page.evaluate(async () => {
    const { React, createRoot } = await import("/tests/fixtures/paperReact.ts");
    const { GraphPicker } = await import("/src/graph/GraphPicker.tsx");
    const host = document.createElement("div");
    document.body.prepend(host);
    createRoot(host).render(
      React.createElement(GraphPicker, {
        refs: [],
        activeRef: { kind: "main" },
        projectId: "p",
        compact: true,
        onSelect: () => {},
        onRefUpdated: () => {},
        onCreate: async () => {},
      }),
    );
  });
  await page.locator(".branch-picker-trigger").waitFor();
  await target(page.locator(".branch-picker-trigger"));
  await fits(page);
});

const turn = {
  operation_id: "t1",
  kind: "project_chat",
  native_session_id: "s1",
  request: {
    message: "Review the introduction",
    chat_id: "intro",
    chat_scope: "project",
    focus: "paper",
  },
  result: { messages: ["The introduction needs a clearer claim."] },
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  active: false,
  attempt: 1,
};

test("a preview selection starts a shared thread, which the rail sends through the coach", async (t) => {
  const { page, errors } = await open(t, {
    width: 1280,
    init: `window.paperCoachFixture = { tasks: [${JSON.stringify(turn)}], started: [] };`,
    configure: ({ tree }) => tree.files.push({ ...tree.files[0], file_id: "f2", path: "other.md" }),
  });
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  const text = page.locator(".paper-right-pane [data-paper-path] p").first();
  await text.waitFor();
  const offer = page.locator(".chat-selection-comment");
  const select = async () => {
    const box = await text.boundingBox();
    await page.mouse.move(box.x + 1, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width - 1, box.y + box.height / 2, { steps: 4 });
    await page.mouse.up();
    await offer.click();
  };
  await select();
  const composer = page.getByRole("dialog", { name: "Comment" });
  const comment = composer.getByRole("textbox", { name: "Comment", exact: true });
  await comment.waitFor();
  await comment.press("Escape");
  await composer.waitFor({ state: "detached" });
  await select();
  await comment.fill("Tighten this claim");
  await composer.getByRole("button", { name: "Comment", exact: true }).click();
  await composer.waitFor({ state: "detached" });
  const rail = page.getByRole("complementary", { name: "Comments" });
  await rail.getByText("Tighten this claim").waitFor();
  const created = await page.evaluate(() =>
    fetch("/api/projects/p/comment-threads?target_kind=paper&target_id=f1")
      .then((response) => response.json())
      .then((page) => page.threads[0]),
  );
  assert.deepEqual(created.target_kind, "paper");
  assert.equal(created.anchor.kind, "text");
  assert.match(created.anchor.where, /\(preview\)$/);
  await rail.getByRole("button", { name: "Send thread to agent" }).click();
  await page.waitForFunction(() => window.paperCoachFixture.started.length === 1);
  const [started] = await page.evaluate(() => window.paperCoachFixture.started);
  assert.equal(started.request.focus, "paper");
  assert.deepEqual(started.request.comment_thread_ids, [created.thread_id]);
  assert.match(started.requestId, /^[0-9a-f-]{36}$/);
  // A composer drawn on one file closes when another is shown.
  await select();
  await comment.fill("half written");
  await page.locator(".paper-file", { hasText: "other.md" }).click();
  await composer.waitFor({ state: "detached", timeout: 5000 });
  await fits(page);
  assert.deepEqual(errors, []);
});

for (const state of ["ready", "warning", "refusal"])
  test(`phone HTML preview keeps every control visible in its ${state} state`, async (t) => {
    const { page, context } = await open(t, {
      configure: ({ tree }) =>
        tree.files.push({
          file_id: "f3",
          path: "page.html",
          kind: "text",
          format: "html",
          size: 5,
        }),
    });
    await context.route("**/files/f3/preview/state", (route) =>
      route.fulfill(
        state === "refusal"
          ? { status: 503, json: { detail: "Preview unavailable" } }
          : { json: { revision: 1, key: "page-1" } },
      ),
    );
    await context.route("**/files/f3/preview?*", (route) =>
      route.fulfill({ contentType: "text/html", body: "<p>Saved page</p>" }),
    );
    await page.getByRole("button", { name: "Show files", exact: true }).click();
    await page.locator(".paper-file", { hasText: "page.html" }).click();
    await page.getByRole("button", { name: "Preview", exact: true }).first().click();
    const toolbar = page.locator(".paper-phone-segments");
    if (state === "refusal")
      await toolbar.getByRole("button", { name: "Retry HTML preview" }).waitFor();
    else {
      await page.locator(".paper-html-preview iframe").waitFor();
      if (state === "warning") {
        await page
          .locator(".paper-html-preview iframe")
          .contentFrame()
          .getByText("Saved page")
          .evaluate(() =>
            parent.postMessage(
              { kind: "rcp-artifact-error", message: "Saved image unavailable", count: 1 },
              location.origin,
            ),
          );
        await toolbar.getByRole("status", { name: "Saved image unavailable" }).waitFor();
      }
    }
    const controls = toolbar.locator(":is(button, input, [role=status]):visible");
    assert.equal(await controls.count(), state === "ready" ? 7 : 8);
    const segments = await toolbar.getByRole("group", { name: "Paper view" }).boundingBox();
    const tools = await toolbar.locator(".paper-panel-tools").boundingBox();
    assert.ok(tools.y >= segments.y + segments.height);
    for (const control of await controls.all()) {
      const box = await control.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
    }
    for (const control of await toolbar.locator(".icon-button:visible").all())
      await target(control);
    await fits(page);
    await page.screenshot({ path: join(tmpdir(), `paper-html-controls-390-${state}.png`) });
  });

for (const width of [1280, 390])
  test(`a new selection in the HTML page keeps the typed comment and its own mark at ${width}px`, async (t) => {
    const { page, context, errors } = await open(t, {
      width,
      configure: ({ tree }) =>
        tree.files.push({
          file_id: "f3",
          path: "page.html",
          kind: "text",
          format: "html",
          size: 5,
        }),
    });
    // A stand-in shell that counts the clears the app sends it.
    await context.route("**/files/f3/preview/state", (route) =>
      route.fulfill({ json: { revision: 1, key: "page-1" } }),
    );
    await context.route("**/files/f3/preview?*", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: '<script>addEventListener("message", (e) => e.data?.type === "rcp-inline-selection-clear" && (window.clears = (window.clears ?? 0) + 1))</script>',
      }),
    );
    if (width === 390) await page.getByRole("button", { name: "Show files", exact: true }).click();
    await page.locator(".paper-file", { hasText: "page.html" }).click();
    await page.getByRole("button", { name: "Preview", exact: true }).first().click();
    await page.getByRole("button", { name: "Comment on the page", pressed: true }).waitFor();
    if (width === 390) {
      await target(page.getByRole("slider", { name: "Preview zoom" }));
      await target(page.getByRole("button", { name: "Reset preview zoom", exact: true }));
    }
    await fits(page);
    await page.screenshot({ path: join(tmpdir(), `paper-html-controls-${width}.png`) });
    await page.locator(".paper-html-preview iframe").waitFor();
    const shell = page.frame({ url: /\/files\/f3\/preview\?revision=1$/ });
    const post = (text) =>
      shell.evaluate(
        (text) =>
          parent.postMessage(
            {
              type: "rcp-artifact-selection",
              version: 1,
              selection: { kind: "text", text, surrounding_text: "" },
              description: "",
            },
            location.origin,
          ),
        text,
      );
    await post("First words");
    const composer = page.getByRole("dialog", { name: "Comment" });
    const comment = composer.getByRole("textbox", { name: "Comment", exact: true });
    await comment.fill("half written");
    await post("Second words");
    await composer.locator("blockquote", { hasText: "Second words" }).waitFor();
    assert.equal(await comment.inputValue(), "half written");
    assert.equal(await shell.evaluate(() => window.clears ?? 0), 0);
    const inside = await composer.boundingBox();
    assert.ok(inside.x >= 0 && inside.x + inside.width <= width, JSON.stringify(inside));
    await composer.getByRole("button", { name: "Comment", exact: true }).click();
    await composer.waitFor({ state: "detached" });
    const created = await page.evaluate(() =>
      fetch("/api/projects/p/comment-threads?target_kind=paper&target_id=f3")
        .then((response) => response.json())
        .then((page) => page.threads[0]),
    );
    assert.equal(created.anchor.text, "Second words");
    assert.equal(created.anchor.where, "page.html (preview)");
    await fits(page);
    assert.deepEqual(errors, []);
  });

test("returning to Paper reopens the coach chat picked from history", async (t) => {
  const newer = {
    ...turn,
    operation_id: "t2",
    native_session_id: "s2",
    request: { ...turn.request, message: "Tighten the abstract", chat_id: "abstract" },
  };
  const summary = (chat_id, title, updated_at) => ({
    chat_id,
    title,
    updated_at,
    kind: "project_chat",
    graph_target: { kind: "main" },
    graph_title: "Main",
    node_id: null,
    message_count: 2,
    last_message_preview: "",
    conversation_kind: "chat",
    orchestrator_episode_id: null,
    focus: "paper",
  });
  const { page, errors } = await open(t, {
    init: `window.paperCoachFixture = { tasks: ${JSON.stringify([newer, turn])}, started: [] };`,
    configure: (fixture) => {
      fixture.chats.push(
        summary("abstract", "Abstract", "2026-11-02T00:00:00Z"),
        summary("intro", "Introduction", "2026-11-01T00:00:00Z"),
      );
      for (const id of ["t1", "t2"]) fixture.turns[id] = { list: { files: [] }, files: {} };
    },
  });
  const prompts = page.locator(".paper-coach .agent-chat-view-line.human .agent-chat-view-text");
  await coach(page);
  await page.getByText("Tighten the abstract").waitFor();
  await page.getByRole("button", { name: "Coach chat history" }).click();
  await page.getByRole("button", { name: "Introduction", exact: true }).click();
  await page.getByText("Review the introduction").waitFor();
  await page.evaluate(() => window.paperTab.leave());
  await page.evaluate(() => window.paperTab.back());
  await page.getByText("Review the introduction").waitFor();
  assert.deepEqual(await prompts.allTextContents(), ["Review the introduction"]);
  assert.deepEqual(errors, []);
});
