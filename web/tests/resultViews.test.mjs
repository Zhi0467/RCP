import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { artifactContextDraft, parseArtifactContextPayload } = await server.ssrLoadModule(
  "/src/components/NodeChat.tsx",
);
const {
  handleAutoResearchDialogKeyDown,
  makeAutoResearchDialogBackgroundInert,
  restoreAutoResearchDialogFocus,
} = await server.ssrLoadModule("/src/components/AutoResearchDialog.tsx");
const nodeChatSource = await readFile(
  new URL("../src/components/NodeChat.tsx", import.meta.url),
  "utf8",
);
const appSource = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

after(() => server.close());

const payload = {
  type: "rcp-artifact-context",
  version: 1,
  project_id: "project",
  chat_id: "chat",
  operation_id: "operation",
  artifact_id: "0123456789abcdef01234567",
  artifact_name: "curves.html",
  media_type: "text/html",
  source: "task",
  episode_id: null,
  selections: [
    {
      kind: "text",
      text: "the final spike",
      surrounding_text: "loss rises around the final spike",
      comment: "Why does this happen?",
    },
    {
      kind: "box",
      rect: { x: 0.5, y: 0.2, width: 0.25, height: 0.3 },
      viewport: { width: 1200, height: 800 },
      labels: "seed three",
      comment: "Compare this with seed one.",
    },
  ],
};

test("artifact selections decode as bounded context for exactly one originating chat", () => {
  assert.deepEqual(parseArtifactContextPayload(payload), payload);
  assert.equal(parseArtifactContextPayload({ ...payload, artifact_id: "bad" }), null);
  assert.equal(parseArtifactContextPayload({ ...payload, selections: [] }), null);
  assert.equal(
    parseArtifactContextPayload({
      ...payload,
      selections: [{ ...payload.selections[0], comment: "x".repeat(2049) }],
    }),
    null,
  );
  assert.equal(
    parseArtifactContextPayload({
      ...payload,
      selections: [
        {
          ...payload.selections[1],
          rect: { x: 0.9, y: 0.2, width: 0.25, height: 0.3 },
        },
      ],
    }),
    null,
  );
  assert.equal(
    parseArtifactContextPayload({ ...payload, source: "episode_report", episode_id: null }),
    null,
  );
});

test("artifact selection comments assemble into a visible annotation-style draft", () => {
  const draft = artifactContextDraft(payload);
  assert.match(draft, /Selected text: the final spike/);
  assert.match(draft, /Why does this happen\?/);
  assert.match(draft, /Boxed region: seed three/);
  assert.match(draft, /Compare this with seed one\./);
  assert.match(draft, /:rcp-artifact-selection\{index="1"\}/);
  assert.match(draft, /:rcp-artifact-selection\{index="2"\}/);
});

test("the unified artifact handoff does not switch mode or dispatch automatically", () => {
  const handoff = nodeChatSource.slice(
    nodeChatSource.indexOf("const accept = (raw: unknown)"),
    nodeChatSource.indexOf("const stored = readStorage(artifactContextKey)"),
  );
  assert.match(handoff, /setArtifactContext/);
  assert.match(handoff, /setMessage/);
  assert.doesNotMatch(handoff, /selectMode\("work"\)/);
  assert.doesNotMatch(handoff, /onStartTask|send\(/);
  assert.match(nodeChatSource, /artifact_context: artifactContext/);
});

test("Experiment run conversations no longer receive special result-view props", () => {
  const selectedConversation = appSource.slice(
    appSource.indexOf("const selectedExperimentConversation"),
    appSource.indexOf("return (", appSource.indexOf("const selectedExperimentConversation")),
  );
  assert.doesNotMatch(selectedConversation, /resultViews|onKeepResultView/);
  assert.match(nodeChatSource, /artifact\.artifact_id, "viewer"/);
  assert.match(nodeChatSource, /artifact\.media_type !== "text\/html"/);
});

test("artifact cards consume backend decisions and do not preflight disabled routes", () => {
  const artifactActions = nodeChatSource.slice(
    nodeChatSource.indexOf("const openArtifact = async"),
    nodeChatSource.indexOf("const openRepositoryFile = async"),
  );
  assert.match(artifactActions, /if \(!artifact\.can_open\) return/);
  assert.match(artifactActions, /if \(!artifact\.can_download\) return/);
  assert.doesNotMatch(artifactActions, /method: "HEAD"|resourceIsAvailable/);
  assert.match(nodeChatSource, /!artifact\.available && artifact\.unavailable_reason/);
  assert.match(nodeChatSource, /artifact\.can_open &&/);
  assert.match(nodeChatSource, /artifact\.can_download &&/);
  assert.match(nodeChatSource, /!sourceArtifact\.can_discuss/);
});

test("artifact revision disposition refreshes the backend-owned source task", () => {
  const decision = nodeChatSource.slice(
    nodeChatSource.indexOf("const decideRevision = async"),
    nodeChatSource.indexOf("const openRepositoryFile = async"),
  );
  assert.match(decision, /await decideArtifactRevision/);
  assert.match(decision, /await onRefreshTask\(review\.taskId\)/);
  assert.match(decision, /refreshedArtifact\.revision_candidate\.diagnostic/);
  assert.doesNotMatch(nodeChatSource, /settledRevisionIds/);
  assert.match(nodeChatSource, /revisionReviewCandidate\.can_accept/);
  assert.match(nodeChatSource, /revisionReviewCandidate\.can_reject/);
  assert.match(nodeChatSource, /versionedArtifactContentUrl/);
  assert.match(appSource, /upsertTask\(next\)/);
});

test("artifact revision review remains available when its preview is unavailable", () => {
  const artifactCard = nodeChatSource.slice(
    nodeChatSource.indexOf("line.artifacts?.map"),
    nodeChatSource.indexOf('line.role === "agent"', nodeChatSource.indexOf("line.artifacts?.map")),
  );
  assert.match(artifactCard, /unavailable && <strong>/);
  assert.match(artifactCard, /\(!unavailable \|\| revisionCandidate\) && \(/);
  assert.match(artifactCard, /!unavailable && artifact\.can_open/);
  assert.match(artifactCard, /revisionCandidate && \([\s\S]*Review revision/);
});

test("artifact revision comparison frames stay opaque but run artifact scripts", () => {
  const compare = nodeChatSource.slice(
    nodeChatSource.indexOf('className="artifact-revision-compare"'),
    nodeChatSource.indexOf("revisionReviewCandidate.diagnostic &&"),
  );
  assert.equal(compare.match(/<iframe/g)?.length, 2);
  assert.equal(compare.match(/sandbox="allow-scripts"/g)?.length, 2);
  assert.doesNotMatch(compare, /allow-same-origin/);
});

test("artifact revision review wires the proven keyboard modal lifecycle", () => {
  assert.match(nodeChatSource, /ref=\{revisionDialogRef\}/);
  assert.match(nodeChatSource, /ref=\{revisionCloseRef\}/);
  assert.match(nodeChatSource, /makeAutoResearchDialogBackgroundInert/);
  assert.match(nodeChatSource, /handleAutoResearchDialogKeyDown/);
  assert.match(nodeChatSource, /restoreAutoResearchDialogFocus/);
  assert.match(nodeChatSource, /tabIndex=\{-1\}/);

  const focused = [];
  const first = focusTarget("first", focused);
  const last = focusTarget("last", focused);
  const dialog = {
    focus() {
      focused.push("dialog");
    },
    contains(element) {
      return element === first || element === last;
    },
    querySelectorAll() {
      return [first, last];
    },
  };
  const tab = keyEvent("Tab", false);
  assert.equal(
    handleAutoResearchDialogKeyDown(tab, dialog, last, false, () => {}),
    true,
  );
  assert.equal(tab.prevented, true);
  assert.deepEqual(focused, ["first"]);

  let closed = false;
  const busyEscape = keyEvent("Escape", false);
  assert.equal(
    handleAutoResearchDialogKeyDown(busyEscape, dialog, first, true, () => {
      closed = true;
    }),
    false,
  );
  assert.equal(closed, false);
  const escape = keyEvent("Escape", false);
  assert.equal(
    handleAutoResearchDialogKeyDown(escape, dialog, first, false, () => {
      closed = true;
    }),
    true,
  );
  assert.equal(closed, true);
});

test("artifact revision modal inerts background and restores its trigger", () => {
  const background = treeElement(false);
  const dialog = treeElement(false);
  const body = treeElement(false, [background, dialog]);
  dialog.parentElement = body;
  const restore = makeAutoResearchDialogBackgroundInert(dialog);
  assert.equal(background.inert, true);
  restore();
  assert.equal(background.inert, false);

  let restored = false;
  restoreAutoResearchDialogFocus({
    isConnected: true,
    focus() {
      restored = true;
    },
  });
  assert.equal(restored, true);
});

function keyEvent(key, shiftKey) {
  return {
    key,
    shiftKey,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
}

function focusTarget(name, focused) {
  return {
    tabIndex: 0,
    focus() {
      focused.push(name);
    },
    getAttribute() {
      return null;
    },
    hasAttribute() {
      return false;
    },
  };
}

function treeElement(inert, children = []) {
  const element = { inert, children, parentElement: null };
  for (const child of children) child.parentElement = element;
  return element;
}
