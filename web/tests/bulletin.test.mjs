import test from "node:test";
import assert from "node:assert/strict";
import {
  bulletinThreads,
  bulletinAuthor,
  bulletinReplyContext,
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
