import assert from "node:assert/strict";
import test from "node:test";

import {
  deserializeSettingsDraft,
  machineProviderPathUpdates,
  machineProviderPathsFrom,
  mergeAgentProfiles,
  mergeMachineProviderPaths,
  serializeSettingsDraft,
} from "../src/settingsDraft.ts";

test("machine provider paths preserve recorded values and emit only edits", () => {
  const saved = machineProviderPathsFrom([
    { alias: "local", host: "", provider_paths: { codex: "/opt/codex", claude: "/opt/claude" } },
    { alias: "cluster", host: "cluster", provider_paths: {} },
  ]);
  assert.deepEqual(machineProviderPathUpdates(saved, saved), undefined);
  assert.deepEqual(
    machineProviderPathUpdates(saved, {
      ...saved,
      local: { ...saved.local, codex: "" },
      cluster: { codex: "/usr/local/bin/codex" },
    }),
    {
      local: { codex: "" },
      cluster: { codex: "/usr/local/bin/codex" },
    },
  );
});

test("an older staged path set keeps provider records added since it was written", () => {
  assert.deepEqual(
    mergeMachineProviderPaths(
      { local: { codex: "/new/codex", claude: "/new/claude" } },
      { local: { codex: "/staged/codex" } },
    ),
    { local: { codex: "/staged/codex", claude: "/new/claude" } },
  );
});

test("an older staged profile does not erase a runtime added to the manifest", () => {
  const saved = {
    node_chat: {
      provider: "codex",
      runtime: "app-server",
      model: "",
      reasoning: "medium",
      run_on: "local",
    },
  };
  const staged = {
    node_chat: {
      provider: "codex",
      model: "gpt-5.6-sol",
      reasoning: "high",
      run_on: "local",
    },
  };

  assert.deepEqual(mergeAgentProfiles(saved, staged), {
    node_chat: { ...staged.node_chat, runtime: "app-server" },
  });
});

test("an older staged provider switch does not keep the other provider's runtime", () => {
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
    node_chat: { provider: "claude", model: "", reasoning: "medium", run_on: "local" },
  };

  assert.deepEqual(mergeAgentProfiles(saved, staged), {
    node_chat: { ...staged.node_chat, runtime: "" },
  });
});

test("settings drafts round trip staged provider paths", () => {
  const draft = {
    version: 2,
    scope: ["repo"],
    profiles: {},
    autoResearchInvocationCeiling: 14,
    providerPaths: { local: { codex: "/opt/codex" } },
  };
  assert.deepEqual(deserializeSettingsDraft(serializeSettingsDraft(draft)), draft);
});

test("v1 settings drafts migrate the campaign default into the v2 episode field", () => {
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
      version: 2,
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

test("a migrated five-profile v1 draft keeps the saved orchestrator profile", () => {
  const runConfig = (model) => ({
    provider: "codex",
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
