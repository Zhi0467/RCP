import test from "node:test";
import assert from "node:assert/strict";
import { bulletinThreads, bulletinAuthor } from "../src/chat/bulletinModel.ts";

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
