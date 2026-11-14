import assert from "node:assert/strict";
import test from "node:test";
import { findAnchor, findText, locateThread } from "../src/comments/anchors.ts";
import { railState, anchorQuote } from "../src/comments/model.ts";
import {
  commentMentionCandidates,
  encodeCommentMentions,
  decodeCommentMentions,
} from "../src/comments/mentions.ts";
import { createCommentsClient } from "../src/comments/api.ts";

const rect = { x: 0.1, y: 0.2, width: 0.3, height: 0.4 };
const bounds = { x: 100, y: 200, width: 300, height: 400 };
const context = {
  text: "old quote new quote end",
  element: (path) => (path === "#chart" ? bounds : null),
  image: bounds,
  page: (page) => (page === 2 ? { bounds, text: "new quote" } : null),
  textRect: (start, end, page) => ({ x: start, y: page ?? 0, width: end - start, height: 10 }),
  sameVersion: true,
};
const thread = (id, state = "open", text = "hello") => ({
  thread_id: id,
  state,
  can_send: true,
  anchor: { kind: "text", text: "quote", comment: "" },
  posts: [{ text }],
});

test("text context disambiguates duplicates then falls back after surrounding edits", () => {
  assert.deepEqual(findText(context.text, "quote", "new quote end"), { start: 14, end: 19 });
  assert.deepEqual(findText(context.text, "quote", "changed"), { start: 4, end: 9 });
  assert.equal(findText(context.text, "gone"), null);
});
test("anchor kinds refind using current geometry and version", () => {
  assert.deepEqual(findAnchor({ kind: "element", path: "#chart" }, context), bounds);
  assert.deepEqual(
    findAnchor({ kind: "box", rect, elements: [{ path: "#chart", region: rect }] }, context),
    { x: 130, y: 280, width: 90, height: 160 },
  );
  assert.deepEqual(findAnchor({ kind: "box", rect, elements: [] }, context), {
    x: 130,
    y: 280,
    width: 90,
    height: 160,
  });
  assert.equal(findAnchor({ kind: "element", path: "#gone" }, context), null);
  assert.deepEqual(findAnchor({ kind: "pdf", page: 2, rect, text: "quote" }, context), {
    x: 130,
    y: 280,
    width: 90,
    height: 160,
  });
  assert.deepEqual(
    findAnchor({ kind: "pdf", page: 2, rect, text: "quote" }, { ...context, sameVersion: false }),
    { x: 4, y: 2, width: 5, height: 10 },
  );
  assert.equal(
    findAnchor({ kind: "pdf", page: 2, rect, text: "gone" }, { ...context, sameVersion: false }),
    null,
  );
});
test("locateThread scrolls and flashes only found anchors", () => {
  const calls = [];
  const locator = {
    ...context,
    scroll: (value) => calls.push(["scroll", value]),
    flash: (value) => calls.push(["flash", value]),
  };
  assert.equal(locateThread(thread("one"), locator), true);
  assert.deepEqual(
    calls.map(([action]) => action),
    ["scroll", "flash"],
  );
  assert.equal(
    locateThread({ ...thread("two"), anchor: { kind: "element", path: "missing" } }, locator),
    false,
  );
});
test("rail excludes resolved and unsendable ticks and checks full selected text plus quote", () => {
  const items = [
    thread("a"),
    { ...thread("b", "resolved"), sent_operation_id: "op" },
    { ...thread("c"), can_send: false },
  ];
  const state = railState(items, new Set(["a", "b", "c", "missing"]));
  assert.deepEqual([...state.ticked], ["a"]);
  assert.equal(state.resolved[0].sent_operation_id, "op");
  assert.equal(state.sendLength, 10);
  assert.equal(state.canSend, true);
  assert.equal(railState([thread("a", "open", "x".repeat(31996))], new Set(["a"])).canSend, false);
  assert.equal(anchorQuote({ kind: "whole" }), "");
});
test("mention picker puts one agent first but only encodes members", () => {
  const candidates = commentMentionCandidates([{ user_id: "m", display_name: "Ada" }], true);
  assert.equal(candidates[0].mention.kind, "agent");
  assert.equal(commentMentionCandidates([], false).length, 0);
  assert.equal(encodeCommentMentions("Hi @Ada", candidates), "Hi <@member:m>");
  assert.equal(decodeCommentMentions("Hi <@member:m>", candidates), "Hi @Ada");
});
test("API sends route bodies with stable caller keys and propagates failures", async () => {
  const calls = [];
  const client = createCommentsClient("p/id", async (url, options) => {
    calls.push([url, options]);
    return new Response(JSON.stringify({ thread_id: "t" }), { status: 200 });
  });
  await client.list({ kind: "artifact", id: "a b" });
  assert.equal(
    calls[0][0],
    "/api/projects/p%2Fid/comment-threads?target_kind=artifact&target_id=a+b",
  );
  await client.sendArtifact("a", ["t"], false, "key");
  assert.equal(calls[1][1].headers["Idempotency-Key"], "key");
  assert.deepEqual(JSON.parse(calls[1][1].body), { thread_ids: ["t"], fresh_session: false });
  const failing = createCommentsClient(
    "p",
    async () => new Response(JSON.stringify({ detail: "Denied" }), { status: 403 }),
  );
  await assert.rejects(failing.get("t"), /Denied/);
});

test("HTML boxes follow the element after a scroll instead of the original viewport", () => {
  const anchor = { kind: "box", rect, elements: [{ path: "#chart", region: rect }] };
  const moved = { ...context, element: () => ({ ...bounds, y: bounds.y - 150 }) };
  assert.deepEqual(findAnchor(anchor, moved), { x: 130, y: 130, width: 90, height: 160 });
});
test("send budget includes typed text and permits the exact limit", () => {
  const item = thread("a", "open", "x".repeat(31994));
  assert.equal(railState([item], new Set(["a"]), "!").canSend, true);
  assert.equal(railState([item], new Set(["a"]), "!!").canSend, false);
  assert.equal(railState([item], new Set()).canSend, false);
});
test("all mutation entry points preserve their contract", async () => {
  const calls = [];
  const client = createCommentsClient("p", async (url, options) => {
    calls.push({ url, ...options, body: options.body && JSON.parse(options.body) });
    return new Response("{}", { status: 200 });
  });
  const target = { kind: "paper", id: "file" };
  const anchor = { kind: "whole", comment: "" };
  await client.create(target, anchor, "note", "version");
  await client.reply("t", "reply");
  await client.editPost("post", "edited");
  await client.deletePost("post");
  await client.resolve("t");
  await client.reopen("t");
  await client.startTask({ message: "task" }, ["t"], "new-turn-key");
  await client.steer("op", "", ["t"], "steer-key");
  assert.deepEqual(
    calls.map(({ method }) => method),
    ["POST", "POST", "PATCH", "DELETE", "POST", "POST", "POST", "POST"],
  );
  assert.deepEqual(calls[0].body, { target, anchor, text: "note", drawn_version: "version" });
  assert.equal(calls[4].url, "/api/projects/p/comment-threads/t/resolve");
  assert.equal(calls[5].url, "/api/projects/p/comment-threads/t/reopen");
  assert.deepEqual(calls[6].body, { message: "task", comment_thread_ids: ["t"] });
  assert.equal(calls[6].headers["Idempotency-Key"], "new-turn-key");
  assert.deepEqual(calls[7].body, {
    message: "",
    comment_thread_ids: ["t"],
    message_id: "steer-key",
  });
});

test("rail respects the send selection count and never encodes an agent token", () => {
  const items = [thread("a"), thread("b")];
  assert.equal(railState(items, new Set(["a", "b"]), "", 1).canSend, false);
  const candidates = commentMentionCandidates([], true);
  assert.equal(encodeCommentMentions("@Agent review", candidates), "@Agent review");
});

test("clients unwrap the backend thread and mention response envelopes", async () => {
  const value = thread("saved");
  const client = createCommentsClient(
    "p",
    async (url) =>
      new Response(
        JSON.stringify(
          url.endsWith("comment-mentions")
            ? { mentions: [{ thread_id: "saved" }] }
            : url.includes("target_kind")
              ? { threads: [value] }
              : { thread: value },
        ),
        { status: 200 },
      ),
  );
  const target = { kind: "artifact", id: "a" };
  assert.deepEqual(await client.list(target), { threads: [value] });
  assert.deepEqual(await client.get("saved"), value);
  assert.deepEqual(await client.create(target, { kind: "whole", comment: "" }, "hello"), value);
  assert.deepEqual(await client.reply("saved", "reply"), value);
  assert.deepEqual(await client.editPost("post", "edit"), value);
  assert.deepEqual(await client.resolve("saved"), value);
  assert.deepEqual(await client.reopen("saved"), value);
  assert.deepEqual(await client.mentions(), [{ thread_id: "saved" }]);
});
test("PDF anchors without a rectangle refind their quote on the same page", () => {
  assert.deepEqual(findAnchor({ kind: "pdf", page: 2, rect: null, text: "quote" }, context), {
    x: 4,
    y: 2,
    width: 5,
    height: 10,
  });
  assert.equal(findAnchor({ kind: "pdf", page: 2, rect: null, text: "" }, context), null);
});
