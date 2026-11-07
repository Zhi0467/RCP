import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import {
  createPaperTestServer,
  openPaper,
  paperTestCard as card,
  PaperTestRoom,
} from "./paperTestServer.mjs";

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
async function redEdge(locator) {
  assert.ok(
    await locator.evaluate((element) => {
      const probe = document.createElement("span");
      probe.style.color = "var(--coral)";
      element.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return getComputedStyle(element).boxShadow.includes(color);
    }),
  );
}
async function fits(page) {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
}
async function target(locator, minimum = 44) {
  const box = await locator.boundingBox();
  assert.ok(box && box.height >= minimum && box.width >= minimum, JSON.stringify(box));
}

test("card refusals have a visible edge and dismiss; transient failures retry", async (t) => {
  const { page, context } = await open(t, { cards: [{ ...card, state: "accepted" }] });
  await coach(page);
  const change = page.locator('[data-change-id="c1"]');
  for (const [state, action] of [
    ["accepted", "Undo change"],
    ["outdated", "Refresh change"],
    ["pending", "Accept change"],
  ]) {
    await context.route("**/paper/changes", (route) =>
      route.fulfill({
        json: { groups: [{ changes: [{ ...card, state }] }] },
      }),
    );
    await page.reload();
    await coach(page);
    let status = 409;
    await context.route("**/changes/g1/*", (route) => route.fulfill({ status, json: {} }));
    await change.getByRole("button", { name: action, exact: true }).click();
    await page.waitForFunction(() =>
      document.querySelector('[data-change-id="c1"]').classList.contains("paper-failure"),
    );
    await redEdge(change);
    await fits(page);
    // A pending or outdated card's own actions are its way forward; only a settled one dismisses.
    const dismiss = change.getByRole("button", { name: "Dismiss change failure" });
    assert.equal(await dismiss.count(), state === "accepted" ? 1 : 0);
    if (state === "accepted") {
      await target(dismiss);
      await dismiss.click();
      assert.equal(await change.evaluate((e) => e.classList.contains("paper-failure")), false);
    }
    status = 503;
    await change.getByRole("button", { name: action, exact: true }).click();
    await change.getByRole("button", { name: "Retry change", exact: true }).waitFor();
    await redEdge(change);
    await context.unroute("**/changes/g1/*");
    await context.unroute("**/paper/changes");
  }
});

test("summary refusals expose one dismiss action and a red edge", async (t) => {
  const { page, context } = await open(t, {
    cards: [
      { ...card, state: "applied" },
      { ...card, change_id: "c2", group_id: "g2", state: "applied" },
    ],
  });
  await context.route("**/changes/undo-all", (route) => route.fulfill({ status: 409, json: {} }));
  await coach(page);
  await page.getByRole("button", { name: "Undo all", exact: true }).click();
  const summary = page.locator(".paper-change-summary");
  const dismiss = summary.getByRole("button", { name: "Dismiss changes failure" });
  await dismiss.waitFor();
  await redEdge(summary);
  await dismiss.click();
  assert.equal(await summary.evaluate((e) => e.classList.contains("paper-failure")), false);
});

test("outdated cards can be rejected", async (t) => {
  const { page, posts } = await open(t, { cards: [{ ...card, state: "outdated" }] });
  await coach(page);
  await page.getByRole("button", { name: "Reject change", exact: true }).click();
  await page.locator('[data-change-id="c1"][data-state="rejected"]').waitFor();
  assert.ok(posts.some((post) => post.path.endsWith("/g1/reject")));
});

test("one change undoes on its own row; two or more add a task-level undo", async (t) => {
  const one = await open(t, { cards: [{ ...card, state: "accepted" }] });
  await coach(one.page);
  await one.page.getByRole("button", { name: "Undo change", exact: true }).waitFor();
  assert.equal(await one.page.locator(".paper-change-summary").count(), 0);
  const { page, posts } = await open(t, {
    cards: [
      { ...card, state: "accepted" },
      { ...card, change_id: "c2", group_id: "g2", state: "accepted" },
    ],
  });
  await coach(page);
  const response = page.waitForResponse((response) => response.url().endsWith("/undo-all"));
  await page.getByRole("button", { name: "Undo all", exact: true }).click();
  await response;
  assert.deepEqual(posts.find((post) => post.path.endsWith("/undo-all"))?.body, {
    task_id: card.task_id,
  });
});

test("create cards never locate a nonexistent file", async (t) => {
  const { page } = await open(t, {
    cards: [{ ...card, kind: "create", file_id: "new", path: "new.md" }],
  });
  await coach(page);
  const change = page.locator('[data-change-id="c1"]');
  await change.waitFor();
  assert.equal(await change.getByRole("button", { name: "Locate change" }).count(), 0);
});

test("card line coordinates shift with the live anchor", async (t) => {
  const { page } = await open(t, {
    cards: [
      {
        ...card,
        lines: { start: 8, end: 10 },
        current: { start: 0, end: 5, revision: 1, text: "draft", line: 12 },
      },
    ],
  });
  await coach(page);
  // Compare coordinates, not the surrounding UI wording.
  const label = await page.locator(".paper-change-name > span").textContent();
  assert.deepEqual(label.match(/\d+/g).map(Number), [12, 14]);
});

test("inline suggestions precede trailing newlines and have usable desktop and phone actions", async (t) => {
  for (const width of [1280, 390]) {
    const room = new PaperTestRoom();
    room.doc.getText("text").insert(5, "\nnext paragraph");
    const { page, errors, posts } = await open(t, {
      width,
      room,
      initialText: "draft\nnext paragraph",
      cards: [
        {
          ...card,
          current: { start: 0, end: 6, revision: 1, text: "draft\n", line: 1 },
        },
      ],
    });
    const widget = page.locator(".paper-inline-suggestion");
    await widget.waitFor();
    assert.equal(
      await widget.evaluate((e) => e.closest(".cm-line").nextElementSibling.textContent),
      "next paragraph",
    );
    assert.ok(await widget.evaluate((e) => parseFloat(getComputedStyle(e).marginRight) > 0));
    for (const action of ["accept", "reject"])
      await target(
        widget.getByRole("button", { name: action + " suggestion" }),
        width === 390 ? 44 : 28,
      );
    await fits(page);
    await widget.getByRole("button", { name: "reject suggestion" }).click();
    await widget.waitFor({ state: "detached" });
    assert.ok(posts.some((post) => post.path.endsWith("/g1/reject")));
    assert.deepEqual(errors, []);
  }
});

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
            changes: [],
            focus: null,
            onDecide: () => {},
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

test("on a touch phone Return writes a new line and a tap outside closes chat history", async (t) => {
  const session = {
    provider: "codex",
    native_session_id: "s1",
    execution_machine: "local",
    title: "Earlier",
    model: "",
    created_at: new Date().toISOString(),
  };
  const { page } = await open(t, {
    touch: true,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) =>
        path.endsWith("/sessions") ? { json: [session] } : respond(path, ...rest);
    },
  });
  await coach(page);
  const message = page.getByRole("textbox", { name: "Message" });
  await message.tap();
  await page.keyboard.type("one");
  await page.keyboard.press("Enter");
  await page.keyboard.type("two");
  assert.equal(await message.inputValue(), "one\ntwo");
  await page.getByRole("button", { name: "Coach chat history" }).tap();
  await page.locator(".paper-session-list").waitFor();
  await page.locator(".paper-coach .node-chat-lines").tap();
  await page.locator(".paper-session-list").waitFor({ state: "detached" });
});

// One finished coach turn in session s1, for the tests that continue it.
const turn = {
  operation_id: "t1",
  kind: "paper_coach",
  native_session_id: "s1",
  request: { message: "Review the introduction", session_id: null },
  result: { messages: ["The introduction needs a clearer claim."] },
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  active: false,
  attempt: 1,
};

async function mountCoachPicker(page) {
  await page.evaluate(async () => {
    window.paperTab.leave();
    const { React, createRoot } = await import("/tests/fixtures/paperReact.ts");
    const { PaperWorkspace } = await import("/src/paper/PaperWorkspace.tsx");
    const { ResourceStore } = await import("/src/core/resourceStore.ts");
    const { RefreshRuntimeContext } = await import("/src/core/useResource.ts");
    const models = [
      { id: "settings-model", label: "Settings model", reasoning: ["medium"] },
      { id: "picked-model", label: "Picked model", reasoning: ["low", "high"] },
      { id: "no-effort-model", label: "No effort model", reasoning: [] },
    ];
    const project = {
      id: "p",
      revision: 1,
      run_on: "local",
      agent_profiles: {
        paper_coach: { provider: "codex", model: "settings-model", reasoning: "medium" },
      },
      provider_readiness: {},
      registry: [
        { provider: "codex", label: "Codex", models },
        { provider: "claude", label: "Claude", models: [models[2]] },
      ],
      machines: [],
    };
    const control = (window.coachPicker = { started: [] });
    function Harness() {
      const [tasks, setTasks] = React.useState([]);
      const [mount, setMount] = React.useState(0);
      const remembered = React.useRef({ view: "coach", right: "coach" });
      control.update = setTasks;
      control.remount = () => setMount((value) => value + 1);
      return React.createElement(PaperWorkspace, {
        key: mount,
        project,
        apiBase: "/api/projects/p",
        editorState: remembered.current,
        onEditorStateChange: (state) => (remembered.current = state),
        tasks,
        onStartTask: async (kind, request) => {
          const at = new Date(Date.now() + control.started.length * 1000).toISOString();
          const task = {
            operation_id: `picked-${control.started.length}`,
            kind,
            request,
            native_session_id: null,
            result: null,
            active: true,
            can_pause: true,
            attempt: 1,
            created_at: at,
            updated_at: at,
          };
          control.started.push(task);
          return task;
        },
      });
    }
    const host = document.body.appendChild(document.createElement("div"));
    createRoot(host).render(
      React.createElement(
        RefreshRuntimeContext.Provider,
        { value: { store: new ResourceStore() } },
        React.createElement(Harness),
      ),
    );
  });
  await page.locator(".paper-coach .agent-config-chip").waitFor();
}

test("the coach keeps its picked config through admission, checkpoint, settlement and remount", async (t) => {
  for (const width of [1280, 390]) {
    let sessions = [];
    const { page, errors } = await open(t, {
      width,
      configure: (fixture) => {
        const respond = fixture.respond;
        fixture.respond = (path, ...rest) =>
          path.endsWith("/sessions") ? { json: sessions } : respond(path, ...rest);
      },
    });
    await mountCoachPicker(page);
    const chip = page.locator(".paper-coach .agent-config-chip");
    await chip.click();
    await page.getByRole("combobox", { name: "Model", exact: true }).selectOption("picked-model");
    await page.getByRole("combobox", { name: "Reasoning", exact: true }).selectOption("high");
    await chip.click();
    await page.getByRole("textbox", { name: "Message" }).fill("Review this draft.");
    await page.getByRole("button", { name: "Send coach message" }).click();
    await page.getByRole("button", { name: "Stop coach" }).waitFor();
    assert.match(await chip.innerText(), /picked-model · high/);
    const admitted = await page.evaluate(() => window.coachPicker.started[0]);
    assert.deepEqual(
      [admitted.request.provider, admitted.request.model, admitted.request.reasoning],
      ["codex", "picked-model", "high"],
    );
    const checkpoint = { ...admitted, native_session_id: "picked-session" };
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/tasks?")),
      page.evaluate((task) => window.coachPicker.update([task]), checkpoint),
    ]);
    assert.match(await chip.innerText(), /picked-model · high/);
    sessions = [
      {
        provider: "codex",
        native_session_id: "picked-session",
        execution_machine: "local",
        model: "picked-model",
        reasoning: "high",
        created_at: admitted.created_at,
      },
    ];
    await page.evaluate((task) => window.coachPicker.update([task]), {
      ...checkpoint,
      active: false,
      updated_at: new Date(Date.now() + 2000).toISOString(),
    });
    await page.waitForFunction(
      () => !document.querySelector(".paper-coach .agent-config-chip").disabled,
    );
    assert.match(await chip.innerText(), /picked-model · high/);
    await page.evaluate(() => window.coachPicker.remount());
    await chip.waitFor();
    assert.match(await chip.innerText(), /picked-model · high/);
    await fits(page);
    assert.deepEqual(errors, []);
  }
});

test("an active coach continuation shows its admitted pick while the writing session is stale", async (t) => {
  const active = {
    ...turn,
    operation_id: "t2",
    active: true,
    can_pause: true,
    result: null,
    created_at: new Date(Date.now() + 1000).toISOString(),
    request: {
      message: "Continue",
      session_id: "s1",
      provider: "codex",
      model: "picked-model",
      reasoning: "high",
    },
  };
  const { page, errors } = await open(t, {
    init: `window.paperCoachFixture = ${JSON.stringify({ tasks: [turn, active], started: [] })};`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) =>
        path.endsWith("/sessions")
          ? {
              json: [
                {
                  provider: "codex",
                  native_session_id: "s1",
                  model: "old-model",
                  reasoning: "low",
                  created_at: turn.created_at,
                },
              ],
            }
          : respond(path, ...rest);
    },
  });
  await coach(page);
  const chip = page.locator(".paper-coach .agent-config-chip");
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
  assert.match(await chip.innerText(), /picked-model · high/);
  await page.evaluate(() => window.paperTab.leave());
  await page.evaluate(() => window.paperTab.back());
  await chip.waitFor();
  assert.match(await chip.innerText(), /picked-model · high/);
  assert.deepEqual(errors, []);
});

test("switching the coach provider keeps a model without native effort through remount", async (t) => {
  const { page, errors } = await open(t);
  await mountCoachPicker(page);
  await page.evaluate((task) => window.coachPicker.update([task]), {
    ...turn,
    request: {
      ...turn.request,
      provider: "codex",
      model: "settings-model",
      reasoning: "medium",
      run_on: "previous-base",
    },
  });
  const chip = page.locator(".paper-coach .agent-config-chip");
  await chip.click();
  await page.getByRole("combobox", { name: "Provider", exact: true }).selectOption("claude");
  assert.equal(await page.getByRole("combobox", { name: "Reasoning", exact: true }).count(), 0);
  await chip.click();
  await page.getByText(/starts a fresh .* session/).waitFor();
  await page.getByRole("textbox", { name: "Message" }).fill("Continue with this provider.");
  await page.getByRole("button", { name: "Send coach message" }).click();
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
  const admitted = await page.evaluate(() => window.coachPicker.started[0]);
  assert.deepEqual(
    [
      admitted.request.provider,
      admitted.request.model,
      admitted.request.reasoning,
      admitted.request.session_id,
      admitted.request.run_on,
    ],
    ["claude", "no-effort-model", "", "s1", "local"],
  );
  const switched = {
    ...admitted,
    request: { ...admitted.request, session_id: null, previous_session_id: "s1" },
  };
  await page.evaluate((task) => window.coachPicker.update([task]), switched);
  await page.evaluate(() => window.coachPicker.remount());
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
  assert.equal(await chip.innerText(), "Claude · no-effort-model");
  await page.evaluate((task) => window.coachPicker.update([task]), {
    ...switched,
    native_session_id: "new-provider-session",
  });
  assert.equal(await chip.innerText(), "Claude · no-effort-model");
  assert.deepEqual(errors, []);
});

test("an evicted coach cohort keeps its held session through a failed second provider switch", async (t) => {
  const previous = {
    ...turn,
    request: { ...turn.request, provider: "codex", model: "settings-model", reasoning: "medium" },
  };
  const switched = {
    ...turn,
    operation_id: "t2",
    native_session_id: "s2",
    created_at: new Date(Date.now() + 1000).toISOString(),
    request: {
      message: "Switch",
      provider: "claude",
      model: "no-effort-model",
      reasoning: "",
      session_id: null,
      previous_session_id: "s1",
    },
  };
  const failed = {
    ...turn,
    operation_id: "t3",
    native_session_id: null,
    failed: true,
    settled: true,
    can_retry: true,
    error: "The provider quit before checkpoint.",
    result: null,
    created_at: new Date(Date.now() + 2000).toISOString(),
    request: {
      message: "Switch again",
      provider: "opencode",
      model: "picked-model",
      reasoning: "high",
      session_id: null,
      previous_session_id: "s2",
    },
  };
  const { page, errors, requests } = await open(t, {
    init: `window.paperCoachFixture = { tasks: [], started: [] };`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) => {
        if (path === "/api/projects/p/tasks") return { json: [failed, switched, previous] };
        if (path.endsWith("/sessions"))
          return {
            json: [
              {
                provider: "codex",
                native_session_id: "s1",
                model: "settings-model",
                reasoning: "medium",
                created_at: turn.created_at,
              },
            ],
          };
        return respond(path, ...rest);
      };
    },
  });
  await mountCoachPicker(page);
  const chip = page.locator(".paper-coach .agent-config-chip");
  await page.waitForFunction(() =>
    document.querySelector(".paper-coach .agent-config-chip")?.textContent.includes("picked-model"),
  );
  await page.evaluate(() => window.coachPicker.remount());
  await page.waitForFunction(() =>
    document.querySelector(".paper-coach .agent-config-chip")?.textContent.includes("picked-model"),
  );
  assert.ok(requests.some((url) => url.includes("paper_session=s1")));
  await chip.click();
  await page.getByRole("combobox", { name: "Provider", exact: true }).selectOption("claude");
  await chip.click();
  await page.getByRole("textbox", { name: "Message" }).fill("Continue with the held provider.");
  await page.getByRole("button", { name: "Send coach message" }).click();
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
  const admitted = await page.evaluate(() => window.coachPicker.started[0]);
  assert.equal(admitted.request.session_id, "s2");
  assert.equal(admitted.request.provider, "claude");
  await fits(page);
  assert.deepEqual(errors, []);
});

test("the coach stages answer comments and files as chat does and sends them with its turn", async (t) => {
  const uploads = [];
  const { page } = await open(t, {
    width: 1280,
    init: `window.paperCoachFixture = { tasks: [${JSON.stringify(turn)}], started: [] };`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, method, ...rest) => {
        const upload = path.match(/\/chats\/([^/]+)\/attachments$/);
        if (!upload) return respond(path, method, ...rest);
        uploads.push(upload[1]);
        return {
          json: {
            attachment_set_id: "set-1",
            attachment: {
              attachment_id: "a1",
              name: "notes.md",
              media_type: "text/markdown",
              size: 5,
              expires_at: new Date(Date.now() + 3_600_000).toISOString(),
            },
          },
        };
      };
    },
  });
  await coach(page);
  const segment = page.locator(".paper-desktop-segments button", { hasText: "Coach" });
  const width = (await segment.boundingBox()).width;
  // The picker stays editable inside an existing coach session.
  assert.equal(
    await page.locator(".paper-coach .chat-composer-tools .agent-config-chip").isDisabled(),
    false,
  );

  await page.getByRole("button", { name: "Comment on this answer" }).click();
  const source = page.getByRole("textbox", { name: "Select answer text" });
  await source.waitFor();
  // The keyboard way in: the answer opens as selectable text.
  await source.press("ControlOrMeta+A");
  await page.getByRole("button", { name: "Comment on selection" }).click();
  await page.getByRole("textbox", { name: "Comment", exact: true }).fill("Name the claim.");
  await page.getByRole("button", { name: "Add comment" }).click();
  await page.locator(".paper-coach .chat-composer .chat-annotation-count").waitFor();
  // The Coach segment counts the waiting comment, so it shows while the preview is open.
  const waiting = page.locator(".paper-desktop-segments .paper-comment-count");
  assert.equal(await waiting.textContent(), "1");
  // The count rides on the segment's corner without resizing it, and is read out.
  assert.equal((await segment.boundingBox()).width, width);
  assert.match(await segment.evaluate((el) => el.textContent), /1 waiting/);
  // A reload keeps the waiting comment in this tab. It names the segment
  // "Coach 1 waiting", so the click finds the segment, not the bare name.
  await page.reload();
  await waiting.waitFor();
  await segment.click();
  await page.locator(".paper-coach .chat-composer .chat-annotation-count").waitFor();
  assert.equal(await waiting.textContent(), "1");

  await page
    .locator(".paper-coach .chat-composer .chat-add-picker input[type=file]")
    .setInputFiles({ name: "notes.md", mimeType: "text/markdown", buffer: Buffer.from("notes") });
  await page.locator(".paper-coach .chat-composer .chat-attachment-chip.ready").waitFor();
  await page.getByRole("textbox", { name: "Message" }).fill("Tighten it.");
  await page.getByRole("button", { name: "Send coach message" }).click();

  const [started] = await page.evaluate(() => window.paperCoachFixture.started);
  assert.equal(started.kind, "paper_coach");
  const { request } = started;
  assert.equal(request.session_id, "s1");
  assert.ok(request.message.startsWith("Tighten it."));
  assert.ok(request.message.includes("The introduction needs a clearer claim."));
  assert.ok(request.message.includes("Name the claim."));
  assert.equal(request.attachment_set_id, "set-1");
  assert.equal(typeof request.attachment_client_id, "string");
  assert.equal(request.attachment_scope_id, uploads[0]);
  // The admitted turn owns its comments and files; the next draft starts empty.
  await page
    .locator(".paper-coach .chat-composer .chat-annotation-count")
    .waitFor({ state: "detached" });
  assert.equal(await page.locator(".paper-coach .chat-composer .chat-attachment-chip").count(), 0);
  assert.equal(await waiting.count(), 0);
});

test("a sent coach turn shows its files and the session's context", async (t) => {
  const sent = {
    ...turn,
    request: {
      ...turn.request,
      attachments: [
        {
          attachment_id: "a1",
          name: "notes.md",
          media_type: "text/markdown",
          size: 5,
          expires_at: new Date(Date.now() + 3_600_000).toISOString(),
        },
      ],
    },
    agent_status: { context_tokens: 40_000, context_window: 200_000 },
  };
  for (const width of [1280, 390]) {
    const { page } = await open(t, {
      width,
      init: `window.paperCoachFixture = { tasks: [${JSON.stringify(sent)}], started: [] };`,
    });
    await coach(page);
    await page
      .locator(".paper-coach .node-chat-line.human .chat-input-attachment", { hasText: "notes.md" })
      .waitFor();
    await page.locator(".paper-panel-tools").getByRole("button", { name: "Context" }).waitFor();
    await fits(page);
  }
});

test("a coach turn carrying only a project reference names its attachment scope", async (t) => {
  const { page } = await open(t, {
    width: 1280,
    init: `window.paperCoachFixture = { tasks: [${JSON.stringify(turn)}], started: [] };`,
  });
  await coach(page);
  const message = page.getByRole("textbox", { name: "Message" });
  await message.click();
  await page.evaluate(async () => {
    const { referenceUrl } = await import("/src/core/projectReferences.ts");
    const link = referenceUrl("p", { kind: "main" }, { kind: "paper", file_id: "d1" });
    const data = new DataTransfer();
    data.setData("text/plain", `See ${link}`);
    document.activeElement.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }),
    );
  });
  await page.locator(".paper-coach .chat-composer .chat-attachment-chips").waitFor();
  await page.getByRole("button", { name: "Send coach message" }).click();
  const [started] = await page.evaluate(() => window.paperCoachFixture.started);
  assert.deepEqual(started.request.references, [{ kind: "paper", file_id: "d1" }]);
  assert.equal(typeof started.request.attachment_scope_id, "string");
  assert.equal(started.request.attachment_set_id, undefined);
});

test("a comment opened from the preview closes when another file is shown", async (t) => {
  const { page } = await open(t, {
    width: 1280,
    configure: ({ tree }) => tree.files.push({ ...tree.files[0], file_id: "f2", path: "other.md" }),
  });
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  const text = page.locator(".paper-right-pane [data-paper-path] p").first();
  await text.waitFor();
  const box = await text.boundingBox();
  await page.mouse.move(box.x + 1, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 1, box.y + box.height / 2, { steps: 4 });
  await page.mouse.up();
  const select = async () => {
    await page.mouse.move(box.x + 1, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width - 1, box.y + box.height / 2, { steps: 4 });
    await page.mouse.up();
    await page.getByRole("button", { name: "Comment", exact: true }).click();
  };
  await page.getByRole("button", { name: "Comment", exact: true }).click();
  const comment = page.getByRole("textbox", { name: "Comment", exact: true });
  await comment.waitFor();
  // Closing it returns focus to the preview it came from, not the hidden Coach.
  await comment.press("Escape");
  await page.waitForFunction(() => document.activeElement?.closest(".paper-right-pane"));
  await select();
  await comment.waitFor();
  await comment.fill("half written");
  await page.locator(".paper-file", { hasText: "other.md" }).click();
  await comment.waitFor({ state: "detached", timeout: 5000 });
  // The typed text waits for the next selection; Escape discards it.
  await text.waitFor();
  await select();
  assert.equal(await comment.inputValue(), "half written");
  await comment.press("Escape");
  await select();
  assert.equal(await comment.inputValue(), "");
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
        const shell = page.frame({ url: /\/files\/f3\/preview\?revision=1$/ });
        await shell.evaluate(() =>
          parent.postMessage(
            { kind: "rcp-artifact-error", message: "Saved image unavailable", count: 1 },
            location.origin,
          ),
        );
        await toolbar.getByRole("status", { name: "Saved image unavailable" }).waitFor();
      }
    }
    const controls = toolbar.locator(":is(button, [role=status]):visible");
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
    await page.screenshot({ path: `/private/tmp/paper-html-controls-390-${state}.png` });
  });

for (const width of [1280, 390])
  test(`a new selection in the HTML page keeps the typed comment and its own mark at ${width}px`, async (t) => {
    const { page, context } = await open(t, {
      width,
      init: `window.paperCoachFixture = { tasks: [${JSON.stringify(turn)}], started: [] };`,
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
    await page.getByRole("button", { name: "Comment on the page" }).click();
    if (width === 390) {
      await target(page.getByRole("button", { name: "Zoom in", exact: true }));
      await target(page.getByRole("button", { name: "Zoom out", exact: true }));
      await target(page.getByRole("button", { name: "Reset preview zoom", exact: true }));
    }
    await fits(page);
    await page.screenshot({ path: `/private/tmp/paper-html-controls-${width}.png` });
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
    const comment = page.getByRole("textbox", { name: "Comment", exact: true });
    await comment.fill("half written");
    await post("Second words");
    await page.getByText("Second words").first().waitFor();
    assert.equal(await comment.inputValue(), "half written");
    assert.equal(await shell.evaluate(() => window.clears ?? 0), 0);
    // A quote longer than a comment allows is cut to the limit, so it stages.
    await shell.evaluate(() =>
      parent.postMessage(
        {
          type: "rcp-artifact-selection",
          version: 1,
          selection: {
            kind: "box",
            rect: { x: 0, y: 0, width: 1, height: 1 },
            viewport: { width: 500, height: 400 },
            elements: Array.from({ length: 8 }, (_, n) => ({
              path: `p:nth-child(${n + 1})`,
              label: "",
              text: `${n}`.repeat(512),
            })),
          },
          description: "",
        },
        location.origin,
      ),
    );
    const quote = page.locator(".chat-annotation-composer blockquote, form blockquote").first();
    await page.waitForFunction(
      () => (document.querySelector("form blockquote")?.textContent.length ?? 0) > 1000,
    );
    const long = await quote.textContent();
    assert.equal(long.length, 4096);
    assert.ok(long.endsWith("…"));
    // Any other way in replaces the open comment the same way: its mark goes, its text stays.
    await coach(page);
    await page.getByRole("button", { name: "Comment on this answer" }).click();
    await page.getByRole("textbox", { name: "Select answer text" }).waitFor();
    await shell.waitForFunction(() => (window.clears ?? 0) === 1);
    assert.equal(await shell.evaluate(() => window.clears ?? 0), 1);
    // A frame selection replacing that keyboard step carries the same text.
    await post("Third words");
    await page.getByText("Third words").first().waitFor();
    assert.equal(await comment.inputValue(), "half written");
  });

const running = {
  ...turn,
  operation_id: "t2",
  request: { message: "Check the claims", session_id: "s1" },
  result: null,
  active: true,
  can_pause: true,
  steer_visible: true,
  can_steer: true,
  steer_turn_id: "turn-1",
  steer_action_label: "Steer",
  steer_unavailable_reason: null,
  steers: [],
};
const steerMessage = (body, operation_id) => ({
  message_id: body.message_id,
  operation_id,
  role: "user",
  text: body.message,
  timestamp: new Date(Date.now() + 1000).toISOString(),
  trigger: "human",
  attachments: [],
  steering: { attempt: 1, turn_id: "turn-1", status: "delivered", label: "Delivered" },
});

test("a running coach turn takes a steer from the composer beside Stop", async (t) => {
  const steers = [];
  const { page, errors } = await open(t, {
    init: `window.paperCoachFixture = { tasks: [${JSON.stringify(turn)}, ${JSON.stringify(running)}], started: [] };`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, method, body, ...rest) => {
        if (path !== "/api/projects/p/tasks/t2/steer") return respond(path, method, body, ...rest);
        steers.push(body);
        return { json: steerMessage(body, "t2") };
      };
    },
  });
  await coach(page);
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
  await page.getByRole("textbox", { name: "Message" }).fill("Focus on section 2.");
  await page.getByRole("button", { name: "Steer", exact: true }).click();
  const steered = page.locator(".paper-coach .node-chat-line.human", {
    has: page.locator(".chat-steering-receipt"),
  });
  await steered.waitFor();
  assert.equal(steers.length, 1);
  const { message_id, ...request } = steers[0];
  assert.match(message_id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(request, {
    attempt: 1,
    expected_turn_id: "turn-1",
    message: "Focus on section 2.",
  });
  assert.equal(await steered.locator(".node-chat-text").textContent(), "Focus on section 2.");
  assert.equal(await steered.evaluate((line) => line.previousElementSibling.dataset.taskId), "t2");
  assert.equal(await page.getByRole("textbox", { name: "Message" }).inputValue(), "");
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
  await fits(page);
  assert.deepEqual(errors, []);
});

test("a settled turn's steers sit between its prompt and reply; an unsteerable turn disables Send", async (t) => {
  const steered = {
    ...turn,
    steers: [steerMessage({ message_id: "m1", message: "Shorter, please." }, "t1")],
  };
  const unsteerable = {
    ...running,
    can_steer: false,
    steer_turn_id: null,
    steer_unavailable_reason: "This runtime cannot be steered.",
  };
  const { page } = await open(t, {
    init: `window.paperCoachFixture = { tasks: [${JSON.stringify(steered)}, ${JSON.stringify(unsteerable)}], started: [] };`,
  });
  await coach(page);
  const lines = page.locator(".paper-coach .node-chat-line[data-task-id=t1]");
  await lines.nth(2).waitFor();
  assert.deepEqual(
    await lines.evaluateAll((items) =>
      items.map((line) => [
        line.classList.contains("human"),
        Boolean(line.querySelector(".chat-steering-receipt")),
      ]),
    ),
    [
      [true, false],
      [true, true],
      [false, false],
    ],
  );
  const send = page.getByRole("button", { name: "Send coach message" });
  await page.getByRole("textbox", { name: "Message" }).fill("More.");
  assert.equal(await send.isDisabled(), true);
  assert.equal(await send.getAttribute("title"), unsteerable.steer_unavailable_reason);
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
});

test("a coach session's turns show though the project task list no longer carries them", async (t) => {
  const { page, errors } = await open(t, {
    init: `window.paperCoachFixture = { tasks: [], started: [] };`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) => {
        if (path === "/api/projects/p/tasks") return { json: [running, turn] };
        if (path.endsWith("/sessions"))
          return {
            json: [
              {
                provider: "codex",
                native_session_id: "s1",
                execution_machine: "local",
                project_id: "p",
                model: "",
                reasoning: "medium",
                created_at: turn.created_at,
                last_resumed_at: turn.created_at,
              },
            ],
          };
        return respond(path, ...rest);
      };
    },
  });
  await coach(page);
  await page.getByRole("button", { name: "Stop coach" }).waitFor();
  const prompts = page.locator(".paper-coach .node-chat-line.human .node-chat-text");
  assert.deepEqual(await prompts.allTextContents(), [
    "Review the introduction",
    "Check the claims",
  ]);
  assert.deepEqual(errors, []);
});

test("the coach reopens where the reader left it, as every agent window does", async (t) => {
  const turns = Array.from({ length: 12 }, (_, index) => ({
    ...turn,
    operation_id: `t${index}`,
    request: { message: `Question ${index}`, session_id: "s1" },
    result: { messages: [`Answer ${index}. `.repeat(40)] },
    created_at: new Date(Date.UTC(2026, 10, 3, 10, index)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 10, 3, 10, index)).toISOString(),
  }));
  const { page, errors } = await open(t, {
    init: `window.paperCoachFixture = { tasks: ${JSON.stringify(turns)}, started: [] };`,
  });
  const lines = page.locator(".paper-coach .node-chat-lines");
  await coach(page);
  await page.waitForFunction(() => {
    const element = document.querySelector(".paper-coach .node-chat-lines");
    return element && element.scrollTop > 0;
  });
  assert.ok(
    await lines.evaluate(
      (element) => element.scrollHeight - element.clientHeight - element.scrollTop < 2,
    ),
  );
  await lines.evaluate((element) => {
    element.scrollTop = 200;
    element.dispatchEvent(new Event("scroll"));
  });
  await page.getByRole("button", { name: "Preview", exact: true }).first().click();
  await coach(page);
  await page.waitForFunction(
    () => document.querySelector(".paper-coach .node-chat-lines")?.scrollTop === 200,
  );
  assert.deepEqual(errors, []);
});

test("returning to Paper reopens the coach chat picked from history", async (t) => {
  const newer = {
    ...turn,
    operation_id: "t2",
    native_session_id: "s2",
    request: { message: "Tighten the abstract", session_id: null },
  };
  const session = (id, title) => ({
    provider: "codex",
    native_session_id: id,
    execution_machine: "local",
    title,
    model: "",
    created_at: turn.created_at,
  });
  const { page, errors } = await open(t, {
    init: `window.paperCoachFixture = { tasks: ${JSON.stringify([newer, turn])}, started: [] };`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) =>
        path.endsWith("/sessions")
          ? { json: [session("s2", "Abstract"), session("s1", "Introduction")] }
          : respond(path, ...rest);
    },
  });
  const prompts = page.locator(".paper-coach .node-chat-line.human .node-chat-text");
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

test("a failed coach turn shows the shared error line, whose Retry starts the turn again", async (t) => {
  const failed = {
    ...turn,
    operation_id: "t9",
    result: null,
    failed: true,
    settled: true,
    error: "The provider quit.",
    can_retry: true,
    can_resume: false,
  };
  const retries = [];
  const { page, errors } = await open(t, {
    init: `window.paperCoachFixture = { tasks: [${JSON.stringify(failed)}], started: [] };`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, method, body, ...rest) => {
        if (path !== "/api/projects/p/tasks/t9/retry") return respond(path, method, body, ...rest);
        retries.push(body);
        return { json: { ...turn, operation_id: "t10", parent_operation_id: "t9", active: true } };
      };
    },
  });
  await coach(page);
  const line = page.locator(".paper-coach .node-chat-line.error");
  assert.equal(await line.locator(".node-chat-text").textContent(), failed.error);
  await Promise.all([
    page.waitForResponse(/\/tasks\/t9\/retry$/),
    line.getByRole("button", { name: "Retry" }).click(),
  ]);
  assert.equal(retries.length, 1);
  await fits(page);
  assert.deepEqual(errors, []);
});

test("a coach turn's artifact offers no comment, since a coach turn carries no artifact context", async (t) => {
  const plot = {
    artifact_id: "plot",
    name: "plot.png",
    view: "image",
    media_type: "image/png",
    size_bytes: 64,
    available: true,
    unavailable_reason: null,
    can_open: true,
    can_download: true,
    can_keep: true,
    can_discuss: true,
  };
  const drawn = {
    ...turn,
    result: { messages: ["![plot](/scratch/turns/t1/artifacts/plot.png)"], artifacts: [plot] },
  };
  const state = {
    pin_branch_id: null,
    pin_branch_name: "main",
    can_pin: false,
    pinned_branch_ids: [],
    artifact_id: "plot",
    name: "plot.png",
    media_type: "image/png",
    view: "image",
    supplier: "turn",
    current_version: "v1",
    version_number: 1,
    can_undo: false,
    live: null,
    editing_operation_id: null,
    can_comment: true,
  };
  let served;
  const stateServed = new Promise((resolve) => (served = resolve));
  const { page } = await open(t, {
    init: `window.paperCoachFixture = { tasks: [${JSON.stringify(drawn)}], started: [] };`,
    configure: (fixture) => {
      const respond = fixture.respond;
      fixture.respond = (path, ...rest) => {
        if (path !== "/api/projects/p/artifacts/plot/state") return respond(path, ...rest);
        served();
        return { json: state };
      };
    },
  });
  await coach(page);
  await page.locator(".paper-coach .chat-inline-artifact").waitFor();
  await stateServed;
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  const comment = page.getByRole("button", { name: /^(Comment on a part|Done commenting)$/ });
  assert.equal(await comment.count(), 0);
});
