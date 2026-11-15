import assert from "node:assert/strict";
import { test } from "node:test";
import { memberPaperChats } from "../src/paper/coachChats.ts";

const chat = (chat_id, updated_at, focus = "paper") => ({ chat_id, updated_at, focus });
const turn = (chat_id, user_id) => ({ request: { chat_id }, authorized_by: { user_id } });

test("the Coach lists the member's unarchived Paper chats, newest first", () => {
  const summaries = [
    chat("old", "2026-01-01"),
    chat("new", "2026-01-03"),
    chat("plain", "2026-01-04", null),
    chat("archived", "2026-01-05"),
    chat("theirs", "2026-01-06"),
  ];
  const tasks = [
    turn("old", "me"),
    turn("new", "me"),
    turn("plain", "me"),
    turn("archived", "me"),
    turn("theirs", "them"),
  ];
  const listed = memberPaperChats(summaries, ["archived"], tasks, "me");
  assert.deepEqual(
    listed.map((item) => item.chat_id),
    ["new", "old"],
  );
  assert.deepEqual(
    memberPaperChats(summaries, [], tasks, null).map((item) => item.chat_id),
    ["theirs", "archived", "new", "old"],
  );
});
