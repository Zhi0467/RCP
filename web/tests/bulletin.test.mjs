import test from "node:test";
import assert from "node:assert/strict";
import {
  bulletinThreads,
  bulletinAuthor,
  bulletinReplyContext,
  bulletinSegments,
  bulletinMentionCandidates,
  bulletinEncodeMentions,
  bulletinDecodeMentions,
  bulletinTrigger,
  bulletinFilter,
  bulletinInsert,
  bulletinPostState,
  bulletinIsOwnPost,
  bulletinOrderChannels,
  bulletinChannelTarget,
  bulletinTargetKey,
} from "../src/chat/bulletinModel.ts";

test("threads retain out-of-page parents and server author snapshots", () => {
  const post = (id, reply_to = null) => ({
    post_id: id,
    reply_to,
    author: { kind: "member", display_name: "Member" },
  });
  const posts = [post("reply", "root"), post("orphan", "earlier"), post("root")];
  const threads = bulletinThreads(posts);
  assert.deepEqual(
    threads.map(({ post }) => post.post_id),
    ["orphan", "root"],
  );
  assert.deepEqual(
    threads[1].replies.map((post) => post.post_id),
    ["reply"],
  );
  assert.equal(bulletinAuthor(posts[0]), "Member");
  assert.equal(
    bulletinAuthor({ author: { kind: "agent", actor_label: "Worker", operation_id: "op" } }),
    "Worker",
  );
});

test("reply context uses the parent snapshot across pages", () => {
  const parent = {
    post_id: "root",
    author: { kind: "member", display_name: "Member" },
    text: "First line\nSecond line",
  };
  const reply = { post_id: "reply", reply_to: "root" };
  const inline = bulletinReplyContext(reply, [parent]);
  assert.equal(inline.author, parent.author.display_name);
  assert.equal(inline.excerpt.includes("\n"), false);
  assert.deepEqual(bulletinReplyContext({ ...reply, reply_parent: parent }, []), inline);
  assert.equal(bulletinReplyContext(reply, []), null);
});

test("text splits into prose, node links, and known or recorded mentions", () => {
  const labels = new Map([["<@member:u1>", "Ada"]]);
  const segments = bulletinSegments(
    "see #exp/fork-b. ask <@member:u1> and <@agent:worker:9>, not <@agent:ghost>",
    labels,
    [{ kind: "agent", id: "worker:9" }],
  );
  assert.deepEqual(
    segments.map((segment) => segment.kind),
    ["text", "node", "text", "mention", "text", "mention", "text"],
  );
  assert.equal(segments[1].id, "exp/fork-b");
  assert.equal(segments[2].text.startsWith("."), true);
  assert.equal(segments[3].label, "Ada");
  assert.deepEqual(segments[5].mention, { kind: "agent", id: "worker:9" });
  assert.equal(segments[6].text.includes("<@agent:ghost>"), true);
});

test("composer names round-trip to stored tokens with unique labels", () => {
  const candidates = bulletinMentionCandidates(
    [
      { user_id: "u1", display_name: "Ada" },
      { user_id: "u2", display_name: "Ada" },
    ],
    [{ id: "worker:1", kind: "worker_node", instruction_title: "", state: "working" }],
  );
  assert.equal(new Set(candidates.map((item) => item.label)).size, candidates.length);
  const agent = candidates[2];
  const second = candidates[1];
  const draft = `hi @${second.label} and @${agent.label}; mail a@b`;
  const stored = bulletinEncodeMentions(draft, candidates);
  assert.equal(stored.includes("<@member:u2>"), true);
  assert.equal(stored.includes("<@agent:worker:1>"), true);
  assert.equal(stored.includes("<@member:u1>"), false);
  assert.equal(stored.endsWith("a@b"), true);
  assert.equal(bulletinDecodeMentions(stored, candidates), draft);
  assert.equal(bulletinDecodeMentions("<@member:gone>", candidates), "<@member:gone>");
});

test("autocomplete triggers at word starts, filters, and inserts", () => {
  assert.equal(bulletinTrigger("mail a@b", 8), null);
  const at = bulletinTrigger("hey @no", 7);
  assert.deepEqual(at, { char: "@", query: "no", start: 4 });
  assert.equal(bulletinTrigger("x #exp/a", 8).char, "#");
  const items = ["Zed", "Node worker", "Anode"];
  assert.deepEqual(
    bulletinFilter(items, "no", (item) => [item]),
    ["Node worker", "Anode"],
  );
  assert.deepEqual(
    bulletinFilter(items, "w", (item) => [item]),
    ["Node worker"],
  );
  const inserted = bulletinInsert("hey @no rest", at, 7, "@Node worker");
  assert.equal(inserted.text, "hey @Node worker rest");
  assert.equal(inserted.caret, "hey @Node worker ".length);
});

test("post state and authorship gate edit and delete", () => {
  assert.equal(bulletinPostState({ edited_at: "t", deleted_at: "t" }), "deleted");
  assert.equal(bulletinPostState({ edited_at: "t", deleted_at: null }), "edited");
  assert.equal(bulletinPostState({}), "plain");
  const mine = { author: { kind: "member", user_id: "u1" } };
  assert.equal(bulletinIsOwnPost(mine, "u1"), true);
  assert.equal(bulletinIsOwnPost(mine, "u2"), false);
  assert.equal(bulletinIsOwnPost(mine, null), false);
  assert.equal(bulletinIsOwnPost({ author: { kind: "agent", user_id: "u1" } }, "u1"), false);
});

test("channels order main first, then newest branch, then name", () => {
  const channel = (target_key, branch_id, name, last_post_at) => ({
    target_key,
    branch_id,
    name,
    last_post_at,
    unread: 0,
  });
  const ordered = bulletinOrderChannels([
    channel("branch:b", "b", "beta", null),
    channel("branch:a", "a", "alpha", null),
    channel("branch:c", "c", "gamma", "2026-10-02T00:00:00Z"),
    channel("main", null, "main", "2026-10-01T00:00:00Z"),
  ]);
  assert.deepEqual(
    ordered.map((item) => item.target_key),
    ["main", "branch:c", "branch:a", "branch:b"],
  );
  assert.deepEqual(bulletinChannelTarget(ordered[1]), { kind: "branch", branch_id: "c" });
  assert.equal(bulletinTargetKey(bulletinChannelTarget(ordered[0])), "main");
});
