import assert from "node:assert/strict";
import test from "node:test";

import {
  defaultModelSelection,
  firstModel,
  modelChange,
  modelOptions,
  modelsFor,
  providerChange,
  providerOptions,
  reasoningFor,
  reasoningOptions,
} from "../src/providers.ts";

/** Shaped like a real `codex debug models` probe: efforts differ per model. */
const CODEX = {
  provider: "codex",
  label: "Codex",
  installed: true,
  authenticated: true,
  models: [
    {
      id: "gpt-5.6-sol",
      label: "GPT-5.6-Sol",
      reasoning: ["low", "high", "ultra"],
      default_reasoning: "low",
    },
    { id: "gpt-5.5", label: "GPT-5.5", reasoning: ["low", "high"], default_reasoning: "medium" },
  ],
};
const CLAUDE = {
  provider: "claude",
  label: "Claude",
  installed: true,
  authenticated: true,
  models: [{ id: "opus", label: "Opus", reasoning: ["low", "max"], default_reasoning: "medium" }],
};

test("every provider the backend probed is offered, under its own label", () => {
  const options = providerOptions([CODEX, CLAUDE], "codex");
  assert.deepEqual(options, [
    { id: "codex", label: "Codex" },
    { id: "claude", label: "Claude" },
  ]);
});

test("reasoning narrows to the selected model rather than the provider", () => {
  assert.deepEqual(reasoningFor(CODEX.models, "gpt-5.6-sol"), ["low", "high", "ultra"]);
  assert.deepEqual(reasoningFor(CODEX.models, "gpt-5.5"), ["low", "high"]);
});

test("an unknown model offers every effort any of the provider's models accepts", () => {
  assert.deepEqual(reasoningFor(CODEX.models, ""), ["low", "high", "ultra"]);
});

test("moving to a model that rejects the current effort falls back to its default", () => {
  // `ultra` is real on sol and absent on 5.5; carrying it over would be
  // rejected at the API, which is the bug this whole path exists to prevent.
  assert.deepEqual(modelChange(CODEX.models, "gpt-5.5", "ultra"), {
    model: "gpt-5.5",
    reasoning: "medium",
  });
});

test("an effort the new model still accepts is left alone", () => {
  assert.deepEqual(modelChange(CODEX.models, "gpt-5.5", "high"), { model: "gpt-5.5" });
});

test("a saved value the provider no longer offers stays selectable", () => {
  // An unreachable CLI reports no models at all. Silently dropping the saved
  // model would rewrite the manifest choice the human made.
  assert.deepEqual(modelOptions([], "gpt-5.4"), [{ id: "gpt-5.4", label: "gpt-5.4" }]);
  assert.deepEqual(reasoningOptions(CODEX.models, "gpt-5.5", "minimal").at(-1), {
    id: "minimal",
    label: "minimal",
  });
});

test("only catalogued models are offered; there is no provider-default entry", () => {
  assert.deepEqual(modelOptions(CLAUDE.models, ""), [{ id: "opus", label: "Opus" }]);
  assert.deepEqual(modelOptions(CODEX.models, ""), [
    { id: "gpt-5.6-sol", label: "GPT-5.6-Sol" },
    { id: "gpt-5.5", label: "GPT-5.5" },
  ]);
});

test("an empty model becomes a durable selection of the catalog head", () => {
  // The backend fills the same head for an unnamed model, so selecting it into
  // state keeps what the picker shows and what runs identical, and saves it.
  assert.equal(firstModel(CODEX.models), "gpt-5.6-sol");
  assert.deepEqual(defaultModelSelection(CODEX.models, "", "high"), { model: "gpt-5.6-sol" });
  // An effort the head rejects is reconciled to the head's own default.
  assert.deepEqual(defaultModelSelection(CODEX.models, "", "medium"), {
    model: "gpt-5.6-sol",
    reasoning: "low",
  });
  // A named model, or no catalog yet, selects nothing and invents nothing.
  assert.equal(defaultModelSelection(CODEX.models, "gpt-5.5", "high"), null);
  assert.equal(defaultModelSelection([], "", "high"), null);
});

test("an unknown provider contributes no models instead of throwing", () => {
  assert.deepEqual(modelsFor([CODEX, CLAUDE], "gemini"), []);
  assert.deepEqual(modelsFor([CODEX, CLAUDE], "claude"), CLAUDE.models);
});

test("switching provider picks that provider's first catalogued model", () => {
  // Offering `gpt-5.5` under Claude would be a value Claude rejects; the model
  // choice belongs to the provider, so it becomes Claude's first model.
  // `max` is shared, so only the model changes.
  assert.deepEqual(providerChange(CLAUDE.models, "claude", "max"), {
    provider: "claude",
    model: "opus",
  });
});

test("an effort the new model does not accept falls back to that model's default", () => {
  assert.deepEqual(providerChange(CLAUDE.models, "claude", "ultra"), {
    provider: "claude",
    model: "opus",
    reasoning: "medium",
  });
});

test("switching to a provider with no known catalog leaves the model empty", () => {
  assert.deepEqual(providerChange([], "gemini", "high"), { provider: "gemini", model: "" });
});
