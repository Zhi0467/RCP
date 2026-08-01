import assert from "node:assert/strict";
import test from "node:test";

import {
  deserializeSettingsDraft,
  machineProviderPathUpdates,
  machineProviderPathsFrom,
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

test("settings drafts round trip staged provider paths", () => {
  const draft = {
    version: 1,
    scope: ["repo"],
    profiles: {},
    providerPaths: { local: { codex: "/opt/codex" } },
  };
  assert.deepEqual(deserializeSettingsDraft(serializeSettingsDraft(draft)), draft);
});
