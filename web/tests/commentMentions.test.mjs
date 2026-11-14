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
const { commentMentionRows, relativeTime } = await server.ssrLoadModule(
  "/src/projects/commentMentions.ts",
);
const { parseNotificationLink } = await server.ssrLoadModule("/src/core/notificationLinks.ts");
const { requestCommentThread, takeCommentThreadRequest } = await server.ssrLoadModule(
  "/src/core/commentThreadRequest.ts",
);

const ada = { user_id: "u-ada", display_name: "Ada" };
const bo = { user_id: "u-bo", display_name: "Bo" };

function mention(overrides = {}) {
  return {
    thread_id: "t1",
    post_id: "p2",
    created_at: "2026-11-13T10:00:00Z",
    target_name: "Loss curve",
    thread: {
      graph_target: "branch:b1",
      anchor: { kind: "whole" },
      posts: [
        { post_id: "p1", author: ada, text: "Ask <@member:u-bo> here" },
        { post_id: "p2", author: bo, text: "Reply" },
      ],
    },
    ...overrides,
  };
}

test("a mention row names the mentioning author and links to its thread", () => {
  const [row] = commentMentionRows("p", [mention()]);
  assert.equal(row.author, "Bo");
  assert.equal(row.targetName, "Loss curve");
  assert.equal(row.excerpt, "Ask @Bo here");
  assert.deepEqual(parseNotificationLink(row.link), {
    projectId: "p",
    target: "branch:b1",
    kind: "comment",
    itemId: "t1",
  });
});

test("a quoted anchor is the excerpt, and a row without its post is skipped", () => {
  const quoted = mention();
  quoted.thread.anchor = { kind: "text", text: "the quote", surrounding_text: "" };
  assert.equal(commentMentionRows("p", [quoted])[0].excerpt, "the quote");
  assert.deepEqual(commentMentionRows("p", [mention({ post_id: "gone" })]), []);
});

test("relative time counts back from now in the largest whole unit", () => {
  const now = new Date("2026-11-13T12:00:00Z");
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" });
  assert.equal(relativeTime("2026-11-13T10:00:00Z", now), format.format(-2, "hour"));
  assert.equal(relativeTime("2026-11-10T12:00:00Z", now), format.format(-3, "day"));
  assert.equal(relativeTime("2026-11-13T11:59:50Z", now), format.format(0, "minute"));
});

test("a thread request is taken once, and only by its own target", () => {
  requestCommentThread({ projectId: "p", targetKind: "paper", targetId: "f1", threadId: "t1" });
  assert.equal(takeCommentThreadRequest("p", "paper", "f2"), null);
  assert.equal(takeCommentThreadRequest("q", "paper", "f1"), null);
  assert.equal(takeCommentThreadRequest("p", "paper", "f1"), "t1");
  assert.equal(takeCommentThreadRequest("p", "paper", "f1"), null);
});
