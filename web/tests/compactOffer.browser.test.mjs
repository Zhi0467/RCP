import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

const SHOTS = process.env.RCP_COMPACT_OFFER_SHOTS;

let server, browser;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

async function mountIdleChat(page) {
  await page.route("**/api/**", (route) => route.fulfill({ json: [] }));
  await page.goto(
    `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/artifacts.html`,
  );
  await page.evaluate(async () => {
    const { default: React } = await import("/node_modules/.vite/deps/react.js");
    const {
      default: { createRoot },
    } = await import("/node_modules/.vite/deps/react-dom_client.js");
    const { AgentChatView } = await import("/src/chat/AgentChatView.tsx");
    const { OpenMachineSettingsContext } = await import("/src/chat/CompactOfferDialog.tsx");
    const { RefreshRuntimeContext } = await import("/src/core/useResource.ts");
    const { runtime } = await import("/tests/fixtures/resourceRuntime.ts");
    const profile = {
      provider: "claude",
      model: "",
      reasoning: "",
      run_on: "local",
      permissions: {},
    };
    const ended = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();
    const task = {
      operation_id: "idle-turn",
      kind: "project_chat",
      status: "succeeded",
      active: false,
      finished: true,
      settled: true,
      failed: false,
      paused: false,
      native_session_id: "session-1",
      current_chat_session_id: "session-1",
      runtime_id: "claude.stream-json.v1",
      can_compact_first: true,
      created_at: ended,
      updated_at: ended,
      finished_at: ended,
      attempt: 1,
      request: {
        chat_id: "idle-chat",
        chat_scope: "project",
        mode: "work",
        provider: "claude",
        session_id: "session-1",
        message: "Run the ablation sweep.",
      },
      result: { messages: ["Done."] },
      agent_status: {
        context_tokens: 238000,
        context_window: 1000000,
        autocompact_tokens: null,
        compacting: false,
        compactions: [],
        subagents: [],
        messages: [],
      },
    };
    window.compactOfferSent = [];
    window.machineSettingsOpened = 0;
    const container = document.createElement("main");
    container.style.height = "100vh";
    container.style.display = "flex";
    container.style.flexDirection = "column";
    document.body.prepend(container);
    createRoot(container).render(
      React.createElement(
        OpenMachineSettingsContext.Provider,
        { value: () => (window.machineSettingsOpened += 1) },
        React.createElement(
          RefreshRuntimeContext.Provider,
          { value: runtime },
          React.createElement(AgentChatView, {
            project: {
              id: "project",
              name: "Project",
              agent_profiles: { node_chat: profile, project_chat: profile },
              repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
              project_truth_scope: ["repo"],
              state_repository: "repo",
              machines: [{ alias: "local", host: null }],
              provider_readiness: {
                local: {
                  claude: { provider: "claude", installed: true, authenticated: true, models: [] },
                },
              },
            },
            graphTarget: { kind: "main" },
            node: null,
            tasks: [task],
            chatId: "idle-chat",
            presentation: "workspace",
            historyMessages: [],
            onRefreshTask: async () => ({ result: { artifacts: [] } }),
            onStartTask: async (kind, request) => {
              window.compactOfferSent.push(request);
              return { ...task, operation_id: `sent-${window.compactOfferSent.length}` };
            },
            onInspectTask() {},
            onOpenInbox() {},
            onRepairGraphUpdate: async () => {},
            onNewSession() {},
            onClose() {},
            onResumeTask() {},
            onRetryTask() {},
          }),
        ),
      ),
    );
  });
  const field = page.getByRole("textbox", { name: "Message" });
  await field.fill("Now rerun seed 2 with the warmup fixed.");
  return field;
}

const sent = (page) => page.evaluate(() => window.compactOfferSent);

for (const width of [1000, 390])
  test(`an idle large session asks before Send and sends the chosen way at ${width}px`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 760 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      const field = await mountIdleChat(page);
      await field.press("Enter");
      const ask = page.getByRole("alertdialog", { name: "Compact before sending?" });
      await ask.waitFor();
      assert.deepEqual(await sent(page), []);
      const box = await ask.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width, "the ask fits the viewport");
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/compact-offer-${width}.png` });

      await page.keyboard.press("Escape");
      assert.equal(await ask.count(), 0);
      assert.deepEqual(await sent(page), []);
      assert.equal(await field.inputValue(), "Now rerun seed 2 with the warmup fixed.");

      await field.press("Enter");
      await ask.getByRole("button", { name: "Compact and send" }).click();
      await page.waitForFunction(() => window.compactOfferSent.length === 1);
      const [compacted] = await sent(page);
      assert.equal(compacted.compact_first, true);
      assert.equal(compacted.message, "Now rerun seed 2 with the warmup fixed.");
      assert.deepEqual(errors, []);
    } finally {
      await page.close();
    }
  });

test("Send as is sends without compacting, and the limits link opens machine settings", async () => {
  const page = await browser.newPage({ viewport: { width: 1000, height: 760 } });
  try {
    const field = await mountIdleChat(page);
    await field.press("Enter");
    const ask = page.getByRole("alertdialog", { name: "Compact before sending?" });
    await ask.getByRole("button", { name: "Auto-compact limits" }).click();
    assert.equal(await page.evaluate(() => window.machineSettingsOpened), 1);
    assert.equal(await ask.count(), 0);

    await field.press("Enter");
    await ask.getByRole("button", { name: "Send as is" }).click();
    await page.waitForFunction(() => window.compactOfferSent.length === 1);
    const [plain] = await sent(page);
    assert.equal(plain.compact_first, undefined);
  } finally {
    await page.close();
  }
});
