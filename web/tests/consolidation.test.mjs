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
  CONSOLIDATION_AUTHORIZATION_DAYS,
  CONSOLIDATION_RECENT_NIGHTS,
  CONSOLIDATION_RENEWAL_WINDOW_DAYS,
  LESSON_TEXT_MAX_CHARS,
  consolidationAttentionCount,
  consolidationAuthorization,
  consolidationCountdown,
  consolidationNeedsRenewal,
  consolidationNightSlots,
  lessonTextIsValid,
  openConsolidationItems,
  timeZoneOptions,
} = await server.ssrLoadModule("/src/projects/consolidation.ts");

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
  assert.equal(consolidationAttentionCount(openConsolidationItems(view), false), 2);
  assert.equal(consolidationAttentionCount(openConsolidationItems(view), true), 3);
  assert.equal(consolidationAttentionCount([], true), 1);
});

test("lesson text must be non-blank and within the code-point limit", () => {
  assert.equal(lessonTextIsValid("   "), false);
  assert.equal(lessonTextIsValid("x".repeat(LESSON_TEXT_MAX_CHARS)), true);
  assert.equal(lessonTextIsValid("x".repeat(LESSON_TEXT_MAX_CHARS + 1)), false);
  // Astral characters are one code point each, as the server counts them.
  assert.equal(lessonTextIsValid("\u{1F600}".repeat(LESSON_TEXT_MAX_CHARS)), true);
});

test("the card's countdown, authorization share, and night strip", () => {
  const at = (ms) => new Date(now + ms).toISOString();
  // Rounded up to the minute, so a pending run never reads as zero.
  assert.deepEqual(consolidationCountdown(at((6 * 60 + 11) * 60_000 + 1), now), {
    hours: 6,
    minutes: 12,
  });
  assert.deepEqual(consolidationCountdown(at(30_000), now), { hours: 0, minutes: 1 });
  assert.equal(consolidationCountdown(at(0), now), null);

  const span = CONSOLIDATION_AUTHORIZATION_DAYS * DAY_MS;
  const authorization = (elapsed, expired = false) =>
    consolidationAuthorization(
      { authorized_at: at(-elapsed), expires_at: at(span - elapsed), expired },
      now,
    );
  assert.deepEqual(authorization(0), {
    daysLeft: CONSOLIDATION_AUTHORIZATION_DAYS,
    fraction: 1,
    expired: false,
  });
  const quarter = authorization(span * 0.75);
  assert.equal(quarter.fraction, 0.25);
  assert.equal(quarter.daysLeft, Math.ceil(CONSOLIDATION_AUTHORIZATION_DAYS / 4));
  assert.deepEqual(authorization(span + DAY_MS), { daysLeft: 0, fraction: 0, expired: true });
  assert.equal(authorization(DAY_MS, true).expired, true);

  const night = (day) => ({ occurrence_date: `2026-09-${day}`, outcome: "succeeded" });
  const short = consolidationNightSlots([night(28), night(29)]);
  assert.equal(short.length, CONSOLIDATION_RECENT_NIGHTS);
  assert.deepEqual(
    short.slice(-2).map((slot) => slot.occurrence_date),
    ["2026-09-28", "2026-09-29"],
  );
  assert.equal(short.filter((slot) => slot === null).length, CONSOLIDATION_RECENT_NIGHTS - 2);
  const long = consolidationNightSlots([20, 21, 22, 23, 24, 25, 26, 27, 28].map(night));
  assert.equal(long[0].occurrence_date, "2026-09-22");
  assert.equal(long.at(-1).occurrence_date, "2026-09-28");
});

test("the time zone picker lists every zone and keeps an unlisted current one", () => {
  const zones = timeZoneOptions("America/New_York");
  assert.ok(zones.length > 100 && zones.includes("Asia/Tokyo"));
  assert.equal(timeZoneOptions("Not/Listed")[0], "Not/Listed");
});
