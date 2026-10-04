import assert from "node:assert/strict";
import test from "node:test";
import {
  canSubmitQuestion,
  questionIsOpen,
  questionTranscript,
  toggleQuestionChoice,
} from "../src/questions.ts";

const question = {
  question_id: "q",
  state: "pending",
  can_answer: true,
  withdrawn_readonly: false,
  choices: ["a", "b"],
  multiple: false,
  created_at: "2026-10-01T12:01:00Z",
};

test("answer eligibility respects the server offer, lifecycle, and selected choices", () => {
  assert.equal(questionIsOpen(question), true);
  assert.equal(questionIsOpen({ ...question, state: "parked" }), true);
  assert.equal(canSubmitQuestion(question, "", []), false);
  assert.equal(canSubmitQuestion(question, "free text", []), true);
  assert.equal(canSubmitQuestion(question, "", ["a"]), true);
  assert.equal(canSubmitQuestion(question, "", ["a", "b"]), false);
  assert.equal(canSubmitQuestion({ ...question, multiple: true }, "", ["a", "b"]), true);
  for (const selection of [["other"], ["a", "a"]])
    assert.equal(canSubmitQuestion({ ...question, multiple: true }, "", selection), false);
  for (const change of [
    { can_answer: false },
    { withdrawn_readonly: true },
    { state: "answered" },
  ]) {
    assert.equal(canSubmitQuestion({ ...question, ...change }, "text", []), false);
  }
});

test("multiple selection toggles without mutating the prior selection", () => {
  const selection = ["a"];
  assert.deepEqual(toggleQuestionChoice(selection, "b"), ["a", "b"]);
  assert.deepEqual(toggleQuestionChoice(selection, "a"), []);
  assert.deepEqual(selection, ["a"]);
});

test("resolved and withdrawn cards retain their chronological transcript position", () => {
  const lines = [
    { id: "before", timestamp: "2026-10-01T12:00:00Z" },
    { id: "after", timestamp: "2026-10-01T12:02:00Z" },
  ];
  for (const change of [{ state: "answered" }, { withdrawn_readonly: true }]) {
    assert.deepEqual(
      questionTranscript(lines, [{ ...question, ...change }]).map((item) =>
        item.kind === "line" ? item.line.id : item.question.question_id,
      ),
      ["before", "q", "after"],
    );
  }
  assert.equal(questionTranscript(lines, [question]).length, 2);
});

test("transcript lines keep their order even without parseable timestamps", () => {
  const lines = [
    { id: "late", timestamp: "2026-10-01T12:05:00Z" },
    { id: "untimed", timestamp: "" },
    { id: "early", timestamp: "2026-10-01T12:00:00Z" },
  ];
  assert.deepEqual(
    questionTranscript(lines, []).map((item) => item.line.id),
    ["late", "untimed", "early"],
  );
});
