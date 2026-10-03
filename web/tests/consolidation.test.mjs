import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const {
  CONSOLIDATION_RENEWAL_WINDOW_DAYS,
  LESSON_TEXT_MAX_CHARS,
  consolidationNeedsRenewal,
  lessonTextIsValid,
  openConsolidationItems,
} = await server.ssrLoadModule("/src/consolidation.ts");

const DAY_MS = 24 * 60 * 60 * 1000;
const now = Date.parse("2026-10-03T12:00:00Z");
const schedule = (expiresInMs, expired = false) => ({
  expires_at: new Date(now + expiresInMs).toISOString(),
  expired,
});

test("renewal is asked for an expired schedule or one inside the window", () => {
  const window = CONSOLIDATION_RENEWAL_WINDOW_DAYS * DAY_MS;
  assert.equal(consolidationNeedsRenewal(null, now), false);
  assert.equal(consolidationNeedsRenewal(schedule(window + DAY_MS), now), false);
  assert.equal(consolidationNeedsRenewal(schedule(window - 1), now), true);
  assert.equal(consolidationNeedsRenewal(schedule(-DAY_MS), now), true);
  // The server's expired flag wins even when the clocks disagree.
  assert.equal(consolidationNeedsRenewal(schedule(window + DAY_MS, true), now), true);
});

test("only open consolidation rows count toward the Inbox", () => {
  const view = {
    schedule: null,
    inbox: [
      { run_id: "a", state: "open" },
      { run_id: "b", state: "kept" },
      { run_id: "c", state: "open" },
      { run_id: "d", state: "dismissed" },
    ],
  };
  assert.deepEqual(
    openConsolidationItems(view).map((item) => item.run_id),
    ["a", "c"],
  );
  assert.deepEqual(openConsolidationItems(null), []);
});

test("lesson text must be non-blank and within the code-point limit", () => {
  assert.equal(lessonTextIsValid("   "), false);
  assert.equal(lessonTextIsValid("x".repeat(LESSON_TEXT_MAX_CHARS)), true);
  assert.equal(lessonTextIsValid("x".repeat(LESSON_TEXT_MAX_CHARS + 1)), false);
  // Astral characters are one code point each, as the server counts them.
  assert.equal(lessonTextIsValid("\u{1F600}".repeat(LESSON_TEXT_MAX_CHARS)), true);
});
