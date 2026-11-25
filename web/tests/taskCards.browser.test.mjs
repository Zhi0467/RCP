import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";
import {
  NODE_ID,
  artifact,
  dockRun,
  endedPresentation,
  planQuestion,
  serveTaskCards,
  taskNode,
} from "./fixtures/taskCardsApi.mjs";

const SHOTS = process.env.TASK_CARD_SHOTS;
if (SHOTS) mkdirSync(SHOTS, { recursive: true });

const WIDE = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };
const CARD = '[data-task-card="q1"]';

async function withPage(viewport, run, options = {}) {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport, ...options });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const url = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/taskCardsChat.html`;
    await run(page, url);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
}

const shot = async (page, name) => {
  if (!SHOTS) return;
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter((animation) => animation.effect.getComputedTiming().endTime !== Infinity)
        .map((animation) => animation.finished.catch(() => undefined)),
    ),
  );
  await page.screenshot({ path: `${SHOTS}/${name}-${page.viewportSize().width}.png` });
};

async function until(page, done) {
  const deadline = Date.now() + 10_000;
  while (!done()) {
    assert.ok(Date.now() < deadline, "The fixture backend was never called");
    await page.waitForTimeout(20);
  }
}

const graphOf = (node = taskNode(), revision = 7) => ({ revision, nodes: { [NODE_ID]: node } });

const noSidewaysScroll = async (page) =>
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth),
    page.viewportSize().width,
  );

const paneButton = (page) => page.locator(".agent-pane-button");

function parkedCard(state, sha = "sha-2") {
  state.questions = [
    planQuestion(
      {},
      {
        ...state.questions[0].card,
        state: "parked",
        pinned_sha256: sha,
        shelved_at: new Date().toISOString(),
        revised_at: new Date().toISOString(),
        revised_by: { kind: "member", name: "Alex", user_id: "alex" },
      },
    ),
  ];
}

test("a plan card sits in its turn; a Design edit Syncs from its base revision before Start", async () => {
  for (const viewport of [WIDE, PHONE])
    await withPage(viewport, async (page, url) => {
      const state = {
        questions: [planQuestion()],
        graph: graphOf(),
        onSync: (body) => {
          state.graph = graphOf(
            taskNode({ updated_rev: 4, design: body.nodes[0].changes.design }),
            8,
          );
          return { body: { graph: state.graph } };
        },
        onDecision: () => ({ body: { outcome: "approved" } }),
      };
      const log = await serveTaskCards(page, state);
      await page.goto(url);
      const card = page.locator(CARD);
      await card.getByRole("button", { name: "Start", exact: true }).waitFor();
      assert.equal(
        await page.locator(`.agent-chat-view-line[data-task-id="turn-1"] ${CARD}`).count(),
        1,
      );
      assert.equal(await page.locator(`.approval-dock ${CARD}`).count(), 0);
      const placed = await page.evaluate((selector) => {
        const line = document.querySelector('.agent-chat-view-line.agent[data-task-id="turn-1"]');
        const outcome = line.querySelector(".chat-turn-outcome");
        const box = document.querySelector(selector).getBoundingClientRect();
        return {
          afterAnswer:
            box.top >= line.querySelector(".chat-markdown p").getBoundingClientRect().bottom,
          beforeOutcome: !outcome || box.bottom <= outcome.getBoundingClientRect().top,
        };
      }, CARD);
      assert.deepEqual(placed, { afterAnswer: true, beforeOutcome: true });

      await card.getByRole("button", { name: "Edit design", exact: true }).click();
      await card.getByRole("textbox", { name: "Design", exact: true }).fill("A tighter design");
      await shot(page, "1-turn-edit-design");
      await noSidewaysScroll(page);
      const before = log.order.length;
      await card.getByRole("button", { name: "Start", exact: true }).click();
      await until(page, () => log.decisions.length > 0);
      assert.deepEqual(log.syncs, [
        {
          base_revision: 7,
          nodes: [
            { node_id: NODE_ID, base_updated_rev: 3, changes: { design: "A tighter design" } },
          ],
        },
      ]);
      const steps = log.order.slice(before);
      assert.ok(steps.indexOf("read") < steps.indexOf("sync"));
      assert.ok(steps.indexOf("sync") < steps.indexOf("decision:approve"));
      assert.equal(log.decisions[0].decision, "approve");
      assert.equal(log.decisions[0].start.node_rev, 4);
      assert.equal(log.decisions[0].pinned_sha256, "sha-1");
    });
});

test("a Sync refused only because the graph moved is sent once more from a new read", async () => {
  await withPage(WIDE, async (page, url) => {
    const state = {
      questions: [planQuestion()],
      graph: graphOf(),
      onSync: (body) => {
        if (body.base_revision === 7) {
          state.graph = graphOf(taskNode(), 9);
          return { status: 409, body: { detail: "moved" } };
        }
        state.graph = graphOf(taskNode({ updated_rev: 10 }), 10);
        return { body: { graph: state.graph } };
      },
      onDecision: () => ({ body: { outcome: "approved" } }),
    };
    const log = await serveTaskCards(page, state);
    await page.goto(url);
    const card = page.locator(CARD);
    await card.getByRole("button", { name: "Edit done when", exact: true }).click();
    await card.getByRole("textbox", { name: "Done when", exact: true }).fill("One\n\nTwo  ");
    await card.getByRole("button", { name: "Start", exact: true }).click();
    await until(page, () => log.decisions.length > 0);
    assert.deepEqual(
      log.syncs.map((sync) => sync.base_revision),
      [7, 9],
    );
    assert.deepEqual(log.syncs[1].nodes[0].changes, { completion_criteria: ["One", "Two"] });
    assert.equal(log.decisions[0].start.node_rev, 10);
  });
});

test("staged changes refuse Start before any Sync or tap", async () => {
  await withPage(WIDE, async (page, url) => {
    const state = {
      questions: [planQuestion()],
      graph: graphOf(),
      onSync: () => ({ body: { graph: state.graph } }),
      onDecision: () => ({ body: { outcome: "approved" } }),
    };
    const log = await serveTaskCards(page, state);
    await page.goto(url);
    const card = page.locator(CARD);
    await card.getByRole("button", { name: "Edit design", exact: true }).click();
    await card.getByRole("textbox", { name: "Design", exact: true }).fill("A tighter design");
    await page.evaluate(() => localStorage.setItem(window.stagedKey, "{}"));
    await card.getByRole("button", { name: "Start", exact: true }).click();
    await card.locator(".approval-error").waitFor();
    assert.deepEqual([log.syncs.length, log.decisions.length], [0, 0]);
    assert.equal(await card.getByRole("button", { name: "Start", exact: true }).isDisabled(), true);
    assert.equal(
      await card.getByRole("button", { name: "Run in this chat", exact: true }).isDisabled(),
      true,
    );
  });
});

test("a node that moved under a Design edit keeps the card pending and drops the edit", async () => {
  await withPage(WIDE, async (page, url) => {
    const state = {
      questions: [planQuestion()],
      graph: graphOf(),
      onSync: () => {
        state.graph = graphOf(taskNode({ updated_rev: 5, design: "The agent's newer design" }), 8);
        return { status: 409, body: { detail: "moved" } };
      },
      onDecision: () => ({ body: { outcome: "approved" } }),
    };
    const log = await serveTaskCards(page, state);
    await page.goto(url);
    const card = page.locator(CARD);
    await card.getByRole("button", { name: "Edit design", exact: true }).click();
    await card.getByRole("textbox", { name: "Design", exact: true }).fill("A tighter design");
    await card.getByRole("button", { name: "Start", exact: true }).click();
    await card.getByText("The agent's newer design").waitFor();
    assert.equal(log.syncs.length, 1);
    assert.equal(log.decisions.length, 0);
    assert.equal(await card.getByRole("textbox", { name: "Design", exact: true }).count(), 0);
    assert.equal(await card.getByText("A tighter design").count(), 0);
    assert.equal(await card.getByRole("button", { name: "Start", exact: true }).isEnabled(), true);
  });
});

test("Park folds the card into the pane, and Delete takes it out", async () => {
  await withPage(WIDE, async (page, url) => {
    const state = {
      questions: [planQuestion()],
      graph: graphOf(),
      onSync: () => ({ body: { graph: state.graph } }),
      onDecision: (body) => {
        if (body.decision === "park") {
          parkedCard(state);
          return { body: { outcome: "parked", pinned_sha256: "sha-2" } };
        }
        state.questions = [
          planQuestion(
            { state: "answered", answer: "declined" },
            { ...state.questions[0].card, result: { outcome: "declined" } },
          ),
        ];
        return { body: { outcome: "declined" } };
      },
    };
    const log = await serveTaskCards(page, state);
    await page.goto(url);
    const card = page.locator(CARD);
    await card.getByRole("button", { name: "Edit handoff", exact: true }).click();
    await card.getByRole("textbox", { name: "Handoff", exact: true }).fill("A shorter pilot");
    assert.equal((await paneButton(page).textContent()).trim(), "0");
    await card.getByRole("button", { name: "Park for later", exact: true }).click();
    await page.locator(".task-flight").waitFor({ state: "attached" });
    const flight = await page.evaluate(() =>
      document
        .querySelector(".task-flight")
        .getAnimations()[0]
        .effect.getKeyframes()
        .map((frame) => frame.transform),
    );
    assert.ok(flight.every((transform) => transform && transform !== "none"));
    await page.locator(`${CARD}.folded`).waitFor();
    await page.locator(".task-flight").waitFor({ state: "detached" });
    assert.equal(log.decisions[0].start.goal, "A shorter pilot");
    await page.waitForFunction(
      () => document.querySelector(".agent-pane-button").textContent.trim() === "1",
    );
    await paneButton(page).click();
    const pane = page.getByRole("complementary");
    const row = pane.getByRole("list", { name: "Parked" }).locator(".task-row");
    assert.equal(await row.count(), 1);

    await card.locator("button.approval-head").click();
    await card.getByRole("button", { name: "Start", exact: true }).waitFor();
    assert.equal(await card.locator(".plan-note").count(), 0);
    assert.equal(await card.getByRole("button", { name: "Save", exact: true }).count(), 0);
    assert.equal(
      await card.getByRole("button", { name: "Run in this chat", exact: true }).count(),
      1,
    );
    await shot(page, "pane-parked");
    await card.getByRole("button", { name: "Delete", exact: true }).click();
    await row.waitFor({ state: "detached" });
    assert.deepEqual(
      log.decisions.map((sent) => [sent.decision, sent.pinned_sha256]),
      [
        ["park", "sha-1"],
        ["decline", "sha-2"],
      ],
    );
    assert.equal((await paneButton(page).textContent()).trim(), "0");
    assert.equal(await page.locator(`${CARD} [role=textbox]`).count(), 0);
  });
});

test("an agent revision drops the member's draft, notes who changed it, and moves the card", async () => {
  for (const viewport of [WIDE, PHONE])
    await withPage(viewport, async (page, url) => {
      const state = {
        questions: [planQuestion()],
        graph: graphOf(),
        onSync: () => ({ body: { graph: state.graph } }),
        onDecision: () => {
          state.questions = [
            planQuestion(
              {},
              {
                state: "parked",
                shelved_at: new Date().toISOString(),
                pinned_sha256: "sha-9",
                action: { ...state.questions[0].card.action, goal: "The agent's revised handoff" },
                revised_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
                revised_by: { kind: "agent", name: "Alice" },
                revised_operation_id: "turn-2",
              },
            ),
          ];
          return { status: 409, body: { detail: { code: "plan_revised" } } };
        },
      };
      await serveTaskCards(page, state);
      await page.goto(url);
      const card = page.locator(CARD);
      await card.getByRole("button", { name: "Edit handoff", exact: true }).click();
      await card.getByRole("textbox", { name: "Handoff", exact: true }).fill("My own draft");
      await card.getByRole("button", { name: "Start", exact: true }).click();
      const moved = page.locator(`.agent-chat-view-line[data-task-id="turn-2"] ${CARD}`);
      await moved.waitFor();
      await moved.locator("button.approval-head").click();
      await moved.getByText("The agent's revised handoff").waitFor();
      assert.equal(await moved.getByText("My own draft").count(), 0);
      assert.match(await moved.locator(".plan-note").textContent(), /Alice/);
      assert.equal(await moved.locator(".plan-note time").count(), 1);
      assert.equal(
        await moved.getByRole("button", { name: "Start", exact: true }).isEnabled(),
        true,
      );
      await moved.locator(".plan-note").scrollIntoViewIfNeeded();
      await shot(page, "2-parked-agent-changed");
      await noSidewaysScroll(page);
    });
});

test("Run in this chat sends the card's fields and the card then links its turn", async () => {
  await withPage(WIDE, async (page, url) => {
    const asked = { operation_id: "later-15" };
    const state = {
      questions: [planQuestion(asked)],
      graph: graphOf(),
      onSync: () => ({ body: { graph: state.graph } }),
      onDecision: () => {
        state.questions = [
          planQuestion(
            { ...asked, state: "answered", answer: "run_here" },
            { state: "ran_here", result: { outcome: "run_here" }, run_here_operation_id: "turn-1" },
          ),
        ];
        return { body: { outcome: "run_here" } };
      },
    };
    const log = await serveTaskCards(page, state);
    await page.goto(`${url}?long`);
    const card = page.locator(CARD);
    await card.getByRole("button", { name: "Run in this chat", exact: true }).click();
    await card.getByRole("button", { name: "Open turn", exact: true }).click();
    assert.equal(log.decisions[0].decision, "run_here");
    assert.equal(log.decisions[0].start.node_rev, 3);
    await page.waitForFunction(() => {
      const box = document
        .querySelector('.agent-chat-view-line[data-task-id="turn-1"]')
        .getBoundingClientRect();
      return box.bottom > 0 && box.top < window.innerHeight;
    });
    assert.equal(await card.getByRole("button", { name: "Start", exact: true }).count(), 0);
  });
});

test("a long handoff folds to six lines, and its editor grows to a bound then scrolls", async () => {
  for (const viewport of [WIDE, PHONE])
    await withPage(viewport, async (page, url) => {
      const goal = Array.from({ length: 40 }, (_, index) => `Step ${index + 1} of the handoff`);
      const state = {
        questions: [
          planQuestion({}, { action: { ...planQuestion().card.action, goal: goal.join("\n") } }),
        ],
        graph: graphOf(),
        onSync: () => ({ body: { graph: state.graph } }),
        onDecision: () => ({ body: { outcome: "approved" } }),
      };
      await serveTaskCards(page, state);
      await page.goto(url);
      const card = page.locator(CARD);
      const text = card.locator(".plan-handoff");
      const more = card.locator(".plan-more");
      await more.waitFor();
      const lines = (locator) =>
        locator.evaluate((element) =>
          Math.round(element.clientHeight / parseFloat(getComputedStyle(element).lineHeight)),
        );
      assert.equal(await lines(text), 6);
      await more.click();
      assert.equal(await lines(text), 40);
      await more.click();
      assert.equal(await lines(text), 6);
      await card.getByRole("button", { name: "Edit handoff", exact: true }).click();
      const editor = card.getByRole("textbox", { name: "Handoff", exact: true });
      const shown = await lines(editor);
      assert.ok(shown >= 15 && shown <= 17, `The editor shows about 16 lines, got ${shown}`);
      assert.ok(await editor.evaluate((element) => element.scrollHeight > element.clientHeight));
      await editor.fill("Short");
      assert.ok((await lines(editor)) <= 3);
      await noSidewaysScroll(page);
    });
});

test("the pane button shows with nothing to list, and the empty pane is only its header", async () => {
  await withPage(WIDE, async (page, url) => {
    await serveTaskCards(page, { questions: [], graph: graphOf() });
    await page.goto(url);
    await paneButton(page).waitFor();
    assert.equal((await paneButton(page).textContent()).trim(), "0");
    await paneButton(page).click();
    const pane = page.getByRole("complementary");
    await pane.waitFor();
    assert.equal(await pane.locator(".agent-pane-section").count(), 0);
    assert.equal(await page.locator(".dock-runs").count(), 0);
  });
});

const started = (episodeId = "episode/one") =>
  planQuestion(
    { state: "answered", answer: "approved" },
    {
      state: "running",
      result: { outcome: "approved" },
      started_episode_id: episodeId,
      current_episode_id: episodeId,
    },
  );

const fullPane = () => ({
  questions: [
    started(),
    planQuestion(
      { question_id: "q2", operation_id: "turn-2" },
      { state: "parked", shelved_at: new Date().toISOString(), pinned_sha256: "sha-p" },
    ),
  ],
  episodes: [
    dockRun("episode/one"),
    dockRun(
      "episode/node",
      {
        phase: "needs_you",
        title: "Thinking-only steering baseline with a very long Experiment title",
        label: "Needs action",
        state_line: "Needs action · turn 6 of 30",
        tone: "actionable",
        needs_you: true,
        recommendation: "continue",
        actions: [
          {
            kind: "continue",
            episode_id: "episode/node",
            operation_id: null,
            report_episode_id: null,
            node_id: null,
          },
        ],
      },
      { run: { source: "node" } },
    ),
    dockRun("episode/done", endedPresentation("Draft-side adaptation follow-up"), {
      episode: { ended_at: new Date(Date.now() - 3600_000).toISOString(), status: "completed" },
    }),
  ],
  artifacts: [
    artifact("report", "Thinking-only steering report", "text/html", "html"),
    artifact("plot", "acceptance-by-arm.png", "image/png", "image"),
  ],
  graph: graphOf(),
  onSync: () => ({ body: {} }),
  onDecision: () => ({ body: { outcome: "declined" } }),
});

test("a started card shows its live run and then its outcome, through a reload, as the pane does", async () => {
  for (const viewport of [WIDE, PHONE])
    await withPage(viewport, async (page, url) => {
      const state = fullPane();
      await serveTaskCards(page, state);
      await page.goto(url);
      const card = page.locator(CARD);
      const badge = card.locator(".question-state");
      await badge.waitFor();
      await page.waitForFunction(
        (selector) => document.querySelector(selector).dataset.tone === "running",
        CARD,
      );
      const live = state.episodes[0].episode.presentation;
      assert.equal(await badge.textContent(), live.state_line);
      assert.equal(await badge.isVisible(), true);
      assert.equal(await card.getByRole("button", { name: "Start", exact: true }).count(), 0);
      assert.equal(
        await card.getByRole("link", { name: "Open agent", exact: true }).getAttribute("href"),
        state.episodes[0].link,
      );
      await shot(page, "6-running");
      await noSidewaysScroll(page);
      await card.getByRole("button", { name: "Show handoff" }).click();
      await card.getByText(state.questions[0].card.action.goal).waitFor();
      await paneButton(page).click();
      const pane = page.getByRole("complementary");
      const row = pane.getByRole("list", { name: "Running" }).locator(".task-row").first();
      assert.equal(await row.locator(".task-state").textContent(), live.label);
      assert.equal(await row.locator(".task-state").getAttribute("data-tone"), live.tone);

      state.questions[0] = started("episode/done");
      state.episodes = [state.episodes[2]];
      await page.reload();
      await page.waitForFunction(
        (selector) => document.querySelector(selector)?.dataset.tone === "completed",
        CARD,
      );
      assert.equal(
        await card.locator(".question-state").textContent(),
        state.episodes[0].episode.presentation.state_line,
      );
      assert.equal(await card.getByRole("button", { name: "Open report", exact: true }).count(), 1);
      assert.equal(await card.getByRole("link", { name: "Open agent", exact: true }).count(), 0);
      await shot(page, "7-ended");
      await noSidewaysScroll(page);
    });
});

test("a started card's first paint is its run, and the chat stays at its bottom while runs load", async () => {
  for (const viewport of [WIDE, PHONE])
    await withPage(viewport, async (page, url) => {
      const state = fullPane();
      state.questions = [started()];
      await serveTaskCards(page, state);
      let releaseCards;
      const cardsHeld = new Promise((resolve) => (releaseCards = resolve));
      await page.route("**/api/projects/p/chats/chat/cards", async (route) => {
        await cardsHeld;
        await route.fallback();
      });
      await page.addInitScript((selector) => {
        window.cardBadges = [];
        new MutationObserver(() => {
          const badge = document.querySelector(`${selector} .question-state`)?.textContent;
          if (badge && window.cardBadges.at(-1) !== badge) window.cardBadges.push(badge);
        }).observe(document, { subtree: true, childList: true, characterData: true });
      }, CARD);
      await page.goto(`${url}?long`);
      const lines = page.locator(".agent-chat-view-lines");
      await page.locator('.agent-chat-view-line[data-task-id="later-15"]').first().waitFor();
      await page.waitForTimeout(200);
      releaseCards();
      await page.locator(CARD).waitFor();
      await page.waitForTimeout(200);
      assert.deepEqual(await page.evaluate(() => window.cardBadges), [
        state.episodes[0].episode.presentation.state_line,
      ]);
      const gap = await lines.evaluate(
        (element) => element.scrollHeight - element.clientHeight - element.scrollTop,
      );
      assert.ok(gap < 2, `the chat left its bottom by ${gap}px`);
    });
});

test("the pane lists four sections that fold and stay folded, at phone width too", async () => {
  for (const viewport of [WIDE, PHONE])
    await withPage(viewport, async (page, url) => {
      const state = fullPane();
      const log = await serveTaskCards(page, state);
      await page.goto(`${url}?long`);
      await page.waitForFunction(
        () => document.querySelector(".agent-pane-button")?.textContent.trim() === "5",
      );
      await paneButton(page).click();
      const pane = page.getByRole("complementary");
      await pane.waitFor();
      assert.deepEqual(
        await pane
          .locator(".agent-pane-fold")
          .evaluateAll((folds) => folds.map((fold) => fold.getAttribute("aria-label"))),
        ["Running", "Parked", "Ended", "Artifacts"],
      );
      assert.deepEqual(
        await Promise.all(
          ["Running", "Parked", "Ended", "Artifacts"].map((name) =>
            pane.getByRole("list", { name }).locator(":scope > li").count(),
          ),
        ),
        [2, 1, 1, 2],
      );
      assert.equal(await page.locator(".dock-runs").count(), 0);
      assert.equal(await page.locator(".approval-dock .experiment-continue-button").count(), 1);
      assert.equal(await pane.getByRole("button", { name: /^Copy (link|reference)$/ }).count(), 2);
      assert.equal(await pane.getByRole("link", { name: /^Download / }).count(), 2);
      const box = await pane.boundingBox();
      if (viewport === PHONE) assert.deepEqual([box.x, box.width], [0, 390]);
      else assert.ok(box.x > 900 && box.x + box.width === 1440);
      await noSidewaysScroll(page);
      assert.deepEqual(
        await pane.evaluate((element) =>
          [...element.querySelectorAll("*")]
            .filter(
              (child) =>
                child.getBoundingClientRect().right > element.getBoundingClientRect().right + 1,
            )
            .map((child) => child.className),
        ),
        [],
      );
      await shot(page, "8-pane-sections");

      const running = pane.getByRole("list", { name: "Running" });
      await running
        .locator(".task-row")
        .first()
        .getByRole("button", { name: /actions$/ })
        .click();
      await page.getByRole("menuitem", { name: "Open agent" }).waitFor();
      await shot(page, "10-pane-menu");
      await page.getByRole("menuitem", { name: "Stop" }).click();
      await running.locator(".task-row-error").waitFor();
      assert.equal(log.stops.length, 1);
      assert.ok(log.stops[0].endsWith("/episodes/episode/one/stop"));

      await pane.getByRole("button", { name: "Ended", exact: true }).click();
      assert.equal(await pane.getByRole("list", { name: "Ended" }).count(), 0);
      assert.equal(
        (
          await pane
            .getByRole("button", { name: "Ended", exact: true })
            .locator(".agent-pane-count")
            .textContent()
        ).trim(),
        "1",
      );
      await page.reload();
      await paneButton(page).click();
      await pane.getByRole("list", { name: "Running" }).waitFor();
      assert.equal(await pane.getByRole("list", { name: "Ended" }).count(), 0);
      assert.equal(
        await pane
          .getByRole("button", { name: "Ended", exact: true })
          .getAttribute("aria-expanded"),
        "false",
      );
    });
});

test("a pane row scrolls the chat to its card, and a node-started run opens in place", async () => {
  await withPage(WIDE, async (page, url) => {
    const state = fullPane();
    await serveTaskCards(page, state);
    await page.goto(`${url}?long`);
    await page.locator(CARD).waitFor();
    const lines = page.locator(".agent-chat-view-lines");
    await lines.evaluate((element) => (element.scrollTop = element.scrollHeight));
    const hash = await page.evaluate(() => location.hash);
    const inView = () =>
      page.evaluate((selector) => {
        const box = document.querySelector(selector).getBoundingClientRect();
        return box.bottom > 0 && box.top < window.innerHeight;
      }, CARD);
    assert.equal(await inView(), false);
    await paneButton(page).click();
    const pane = page.getByRole("complementary");
    const rows = pane.getByRole("list", { name: "Running" }).locator(".task-row");
    await rows.first().locator(".task-row-main").click();
    await page.waitForFunction((selector) => {
      const box = document.querySelector(selector).getBoundingClientRect();
      return box.top >= 0 && box.bottom <= window.innerHeight;
    }, CARD);
    assert.equal(await rows.first().locator(".approval-card").count(), 0);

    await rows.nth(1).locator(".task-row-main").click();
    const opened = rows.nth(1).locator(".approval-card");
    await opened.waitFor();
    assert.equal(
      await opened.getByRole("link", { name: "Open agent", exact: true }).getAttribute("href"),
      state.episodes[1].link,
    );
    await shot(page, "pane-node-run-open");
    await rows.nth(1).locator(".task-row-main").click();
    await opened.waitFor({ state: "detached" });
    assert.equal(await page.evaluate(() => location.hash), hash);
    await pane.waitFor();
  });
});

test("reduced motion parks with a fade and no flight", async () => {
  await withPage(
    PHONE,
    async (page, url) => {
      const state = {
        questions: [planQuestion()],
        graph: graphOf(),
        onSync: () => ({ body: { graph: state.graph } }),
        onDecision: () => {
          parkedCard(state);
          return { body: { outcome: "parked", pinned_sha256: "sha-2" } };
        },
      };
      await serveTaskCards(page, state);
      await page.goto(url);
      const card = page.locator(CARD);
      await card.getByRole("button", { name: "Park for later", exact: true }).click();
      await page.locator(".task-flight").waitFor({ state: "attached" });
      const frames = await page.evaluate(() =>
        document.querySelector(".task-flight").getAnimations()[0].effect.getKeyframes(),
      );
      assert.ok(frames.every((frame) => !frame.transform || frame.transform === "none"));
      assert.notEqual(frames[0].opacity, frames.at(-1).opacity);
      await page.locator(`${CARD}.folded`).waitFor();
      await noSidewaysScroll(page);
    },
    { reducedMotion: "reduce" },
  );
});
