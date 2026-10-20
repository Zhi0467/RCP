import test from "node:test";
import assert from "node:assert/strict";
import {
  bulletinThreads,
  bulletinAuthor,
  bulletinSegments,
  bulletinMentionCandidates,
  bulletinEncodeMentions,
  bulletinDecodeMentions,
  bulletinTrigger,
  bulletinFilter,
  bulletinInsert,
  bulletinPostState,
  bulletinIsOwnPost,
  bulletinChannelTarget,
  bulletinTargetKey,
  bulletinKeyTarget,
  bulletinAgentChoices,
  bulletinChannelCandidates,
  bulletinMoveDraft,
  bulletinMentionKind,
  bulletinActorKind,
  bulletinSuggestPlacement,
  BULLETIN_SUGGEST_MAX_HEIGHT,
} from "../src/chat/bulletinModel.ts";

test("threads group replies under parents and retain server author snapshots", () => {
  const post = (id, reply_to = null) => ({
    post_id: id,
    reply_to,
    author: { kind: "member", display_name: "Member" },
  });
  const posts = [post("reply", "root"), post("root")];
  const threads = bulletinThreads(posts);
  assert.deepEqual(
    threads.map(({ post }) => post.post_id),
    ["root"],
  );
  assert.deepEqual(
    threads[0].replies.map((post) => post.post_id),
    ["reply"],
  );
  assert.equal(bulletinAuthor(posts[0]), "Member");
  assert.equal(
    bulletinAuthor({ author: { kind: "agent", actor_label: "Worker", operation_id: "op" } }),
    "Worker",
  );
  const named = { author: { kind: "agent", actor_id: "chat:c", actor_label: "Old" } };
  assert.equal(bulletinAuthor(named, new Map([["chat:c", "Renamed"]])), "Renamed");
});

test("text splits into prose, node links, and known or recorded mentions", () => {
  const labels = new Map([["<@member:u1>", "Ada"]]);
  const segments = bulletinSegments(
    "see #exp/fork-b. ask <@member:u1> and <@agent:worker:9>, not <@agent:ghost> or #exp/gone",
    labels,
    new Set(["exp/fork-b"]),
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
  assert.equal(segments[6].text.includes("#exp/gone"), true);
});

test("composer names round-trip to stored tokens with unique labels", () => {
  const candidates = bulletinMentionCandidates(
    [
      { user_id: "u1", display_name: "Ada" },
      { user_id: "u2", display_name: "Ada" },
    ],
    [{ id: "worker:1", kind: "worker_node", name: "", target_key: "main", state: "working" }],
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

test("channels map to graph targets", () => {
  assert.deepEqual(bulletinChannelTarget({ branch_id: "c" }), { kind: "branch", branch_id: "c" });
  assert.equal(bulletinTargetKey(bulletinChannelTarget({ branch_id: null })), "main");
  for (const target of [{ kind: "main" }, { kind: "branch", branch_id: "fork:a" }])
    assert.deepEqual(bulletinKeyTarget(bulletinTargetKey(target)), target);
});

test("agent choices span every channel, shown channel first, labels unique project-wide", () => {
  const agent = (id, target_key, name) => ({ id, target_key, name, kind: "human_chat_work" });
  const roster = [
    agent("chat:a", "main", "running-the"),
    agent("chat:b", "branch:x", "running-the"),
    agent("chat:c", "branch:x", "running-the"),
  ];
  const members = [{ user_id: "u1", display_name: "Ada" }];
  const choices = bulletinAgentChoices(members, roster, "branch:x");
  assert.deepEqual(
    choices.map((choice) => choice.actor.id),
    ["chat:b", "chat:c", "chat:a"],
  );
  // The shown channel's first agent keeps its bare name; every label is distinct.
  assert.equal(choices[0].label, roster[1].name);
  assert.equal(new Set(choices.map((choice) => choice.label)).size, 3);
  // Each label encodes to its own agent from this channel's composer, another channel's included.
  const candidates = bulletinChannelCandidates(members, roster, "branch:x");
  for (const choice of choices)
    assert.equal(bulletinEncodeMentions(`@${choice.label}`, candidates), choice.token);
});

test("mention rows tag only autonomous agents with their kind", () => {
  for (const kind of ["human_chat_work", "human_chat_discuss"])
    assert.equal(bulletinMentionKind({ kind }), null);
  for (const kind of ["auto_research_root", "worker_node", "worker_project", "experiment_loop"])
    assert.equal(bulletinMentionKind({ kind }), bulletinActorKind({ kind }));
});

test("a draft moved to another channel keeps each mention's recipient", () => {
  const agent = (id, target_key, name) => ({ id, target_key, name, kind: "human_chat_work" });
  const roster = [agent("chat:a", "main", "Helper"), agent("chat:b", "branch:x", "Helper")];
  const main = bulletinMentionCandidates([], roster.slice(0, 1));
  const branch = bulletinMentionCandidates([], roster.slice(1));
  const moved = bulletinMoveDraft("ask @Helper", main, branch);
  assert.equal(bulletinEncodeMentions(moved, branch), "ask <@agent:chat:a>");
});

test("a draft rebased across a rename keeps each mention's recipient", () => {
  const agent = (id, name) => ({ id, target_key: "main", name, kind: "human_chat_work" });
  const before = bulletinMentionCandidates([], [agent("chat:a", "Twin"), agent("chat:b", "Twin")]);
  const after = bulletinMentionCandidates([], [agent("chat:a", "Solo"), agent("chat:b", "Twin")]);
  const first = before.find((item) => item.token === "<@agent:chat:a>");
  const draft = `ask @${first.label}`;
  // Without the rebase the renamed agent's old label now names the other agent.
  assert.equal(bulletinEncodeMentions(draft, after), "ask <@agent:chat:b>");
  const rebased = bulletinMoveDraft(draft, before, after);
  assert.equal(bulletinEncodeMentions(rebased, after), "ask <@agent:chat:a>");
});

test("encoding keeps stored tokens whole even when a label matches inside them", () => {
  const candidates = bulletinMentionCandidates([{ user_id: "u1", display_name: "agent" }], []);
  assert.equal(
    bulletinEncodeMentions("ask <@agent:chat:b> and @agent", candidates),
    "ask <@agent:chat:b> and <@member:u1>",
  );
});

test("suggestions open where the scroll area has room and never past its edge", () => {
  const area = { top: 100, bottom: 900 };
  // A composer just under the area's top opens downward, capped to the room below.
  const nearTop = bulletinSuggestPlacement({ top: 150, bottom: 190 }, area);
  assert.equal(nearTop.side, "below");
  assert.ok(nearTop.maxHeight <= area.bottom - 190);
  // Full room above keeps the usual upward list at its full height.
  assert.deepEqual(bulletinSuggestPlacement({ top: 800, bottom: 840 }, area), {
    side: "above",
    maxHeight: BULLETIN_SUGGEST_MAX_HEIGHT,
  });
  // Cramped on both sides: the larger side wins and the list fits inside it.
  const cramped = bulletinSuggestPlacement({ top: 160, bottom: 200 }, { top: 100, bottom: 240 });
  assert.ok(cramped.maxHeight <= 60 && cramped.maxHeight > 0);
});
