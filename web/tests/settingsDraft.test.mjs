import assert from "node:assert/strict";
import test from "node:test";

import {
  deserializeSettingsDraft,
  mergeAgentProfiles,
  settingsFingerprint,
} from "../src/projects/settingsDraft.ts";

test("a staged profile keeps its own runtime over the manifest", () => {
  const saved = {
    node_chat: {
      provider: "codex",
      runtime: "exec",
      model: "",
      reasoning: "medium",
      run_on: "local",
    },
  };
  const staged = {
    node_chat: {
      provider: "codex",
      runtime: "app-server",
      model: "gpt-5.6-sol",
      reasoning: "high",
      run_on: "local",
    },
  };

  assert.deepEqual(mergeAgentProfiles(saved, staged), staged);
});

test("a staged profile written before runtime selection is dropped", () => {
  const draft = deserializeSettingsDraft(
    JSON.stringify({
      version: 3,
      scope: ["repo"],
      profiles: {
        // A provider switch with no runtime beside it. Provider and runtime are
        // one choice, and nothing can name the missing half.
        seed: { provider: "claude", model: "", reasoning: "medium", run_on: "local" },
        // An empty runtime is the same incomplete pair spelled differently.
        refresh: {
          provider: "codex",
          runtime: "",
          model: "",
          reasoning: "medium",
          run_on: "local",
        },
        node_chat: {
          provider: "codex",
          runtime: "app-server",
          model: "",
          reasoning: "medium",
          run_on: "local",
        },
      },
    }),
  );

  assert.ok(draft);
  assert.deepEqual(Object.keys(draft.profiles), ["node_chat"]);
});

test("a dropped staged profile leaves the manifest profile intact", () => {
  const saved = {
    seed: {
      provider: "codex",
      runtime: "exec",
      model: "",
      reasoning: "medium",
      run_on: "local",
    },
  };
  const draft = deserializeSettingsDraft(
    JSON.stringify({
      version: 3,
      scope: ["repo"],
      profiles: { seed: { provider: "claude", model: "", reasoning: "medium", run_on: "local" } },
    }),
  );

  assert.ok(draft);
  assert.deepEqual(mergeAgentProfiles(saved, draft.profiles), saved);
});

test("v1 settings drafts migrate the campaign default into the current episode field", () => {
  assert.deepEqual(
    deserializeSettingsDraft(
      JSON.stringify({
        version: 1,
        scope: ["repo"],
        profiles: {},
        campaignInvocationCeiling: 14,
      }),
    ),
    {
      version: 4,
      scope: ["repo"],
      profiles: {},
      autoResearchInvocationCeiling: 14,
    },
  );
});

test("v2 settings drafts accept one operational invocation and reject legacy or invalid fields", () => {
  assert.ok(
    deserializeSettingsDraft(
      JSON.stringify({
        version: 2,
        scope: ["repo"],
        profiles: {},
        autoResearchInvocationCeiling: 1,
      }),
    ),
  );
  assert.equal(
    deserializeSettingsDraft(
      JSON.stringify({
        version: 2,
        scope: ["repo"],
        profiles: {},
        autoResearchInvocationCeiling: 0,
      }),
    ),
    null,
  );
  assert.equal(
    deserializeSettingsDraft(
      JSON.stringify({
        version: 2,
        scope: ["repo"],
        profiles: {},
        campaignInvocationCeiling: 14,
      }),
    ),
    null,
  );
});

test("v3 settings drafts discard previously persisted compute metadata", () => {
  const draft = deserializeSettingsDraft(
    JSON.stringify({
      version: 3,
      scope: ["repo"],
      profiles: {},
      computeConnections: [
        {
          id: "gpu",
          name: "GPU VM",
          kind: "ssh",
          ssh_target: "researcher@gpu.example",
          access_hint: "Use the shared scratch directory",
        },
      ],
    }),
  );

  assert.equal(draft?.version, 4);
  assert.equal(Object.hasOwn(draft, "computeConnections"), false);

  const malformed = deserializeSettingsDraft(
    JSON.stringify({
      version: 3,
      scope: ["repo"],
      profiles: {},
      computeConnections: [{ id: "gpu", name: "GPU VM", kind: "ssh", password: "do-not-retain" }],
    }),
  );
  assert.equal(malformed?.version, 4);
  assert.equal(Object.hasOwn(malformed, "computeConnections"), false);
});

test("a migrated five-profile v1 draft keeps the saved orchestrator profile", () => {
  const runConfig = (model) => ({
    provider: "codex",
    runtime: "exec",
    model,
    reasoning: "medium",
    run_on: "local",
  });
  const saved = {
    seed: runConfig("saved-seed"),
    refresh: runConfig("saved-refresh"),
    node_chat: runConfig("saved-node-chat"),
    project_chat: runConfig("saved-project-chat"),
    paper_coach: runConfig("saved-paper-coach"),
    orchestrator: runConfig("saved-orchestrator"),
  };
  const legacy = deserializeSettingsDraft(
    JSON.stringify({
      version: 1,
      scope: ["repo"],
      profiles: {
        seed: runConfig("draft-seed"),
        refresh: runConfig("draft-refresh"),
        node_chat: runConfig("draft-node-chat"),
        project_chat: runConfig("draft-project-chat"),
        paper_coach: runConfig("draft-paper-coach"),
      },
    }),
  );

  assert.ok(legacy);
  assert.deepEqual(mergeAgentProfiles(saved, legacy.profiles), {
    ...legacy.profiles,
    orchestrator: saved.orchestrator,
  });
});

test("settings compare by value, not by the order the backend listed the keys", () => {
  // Reading a project sorts its field names; a save response returns them in
  // declaration order. Comparing the raw text left the form permanently dirty.
  const read = { skill_defaults: { skill_ids: ["a"], workflow_ids: [] } };
  const saved = { skill_defaults: { workflow_ids: [], skill_ids: ["a"] } };

  assert.notEqual(JSON.stringify(read), JSON.stringify(saved));
  assert.equal(settingsFingerprint(read), settingsFingerprint(saved));
});

test("settings compare keeps list order, which the researcher chose", () => {
  assert.notEqual(
    settingsFingerprint({ scope: ["a", "b"] }),
    settingsFingerprint({ scope: ["b", "a"] }),
  );
});

test("machine settings an older draft staged load without them", () => {
  const draft = deserializeSettingsDraft(
    JSON.stringify({
      version: 4,
      scope: ["repo"],
      profiles: {},
      providerPaths: { local: { codex: "/opt/codex" } },
      machineCompute: { local: { job_manager: null, jobs_root: "/old" } },
      machineComputeEdits: { cluster: { job_manager: "slurm", jobs_root: "" } },
      accessHintEdits: { cluster: "module load cuda" },
    }),
  );
  assert.deepEqual(draft, { version: 4, scope: ["repo"], profiles: {} });
});
