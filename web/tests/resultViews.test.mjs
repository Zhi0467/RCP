import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { withExperimentControlAnswers, withTurnAnswers } from "./taskAnswers.mjs";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const {
  NodeChat,
  parseResultViewTarget,
  resultViewGestureDraft,
  resultViewGestureFromFrame,
  resultViewRequestForTarget,
  resultViewTargetStorageKey,
  resultViewTargetValidationError,
  serializeResultViewTarget,
} = await server.ssrLoadModule("/src/components/NodeChat.tsx");
const { ExperimentRunDetail } = await server.ssrLoadModule(
  "/src/components/ExperimentRunDetail.tsx",
);
const {
  resultViewLoadIsCurrent,
  resultViewSelectionIsCurrent,
  resultViewSelectionKey,
  shouldLoadVisibleChatTranscript,
  visibleChatTranscriptIds,
  visibleUnreadChatId,
} = await server.ssrLoadModule("/src/App.tsx");
const { keepResultView, loadResultViews, resultViewPreviewUrl } =
  await server.ssrLoadModule("/src/api.ts");
const nodeChatSource = await readFile(
  new URL("../src/components/NodeChat.tsx", import.meta.url),
  "utf8",
);
const appSource = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const episodeDialogsSource = await readFile(
  new URL("../src/hooks/useEpisodeDialogs.ts", import.meta.url),
  "utf8",
);

after(() => server.close());

const profile = {
  provider: "codex",
  model: "gpt-codex",
  reasoning: "medium",
  run_on: "local",
  permissions: {},
};

const project = {
  id: "project/one",
  name: "Project",
  agent_profiles: { node_chat: profile, project_chat: profile, paper_coach: profile },
  provider_readiness: {
    local: {
      codex: {
        provider: "codex",
        label: "Codex",
        installed: true,
        authenticated: true,
        binary_path: "/usr/bin/codex",
        path_state: "resolved",
        models: [],
      },
    },
  },
  repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
  project_truth_scope: ["repo"],
  state_repository: "repo",
  machines: [{ alias: "local", host: null }],
};

const experiment = {
  id: "experiment/result-view",
  type: "experiment",
  title: "Result-view experiment",
  standing: "asserted",
  created_rev: 1,
  updated_rev: 1,
  source_refs: [],
  extension_fields: {},
  status: "completed",
  current_summary: "The run completed.",
  invocation_ceiling: 2,
  completion_criteria: [],
  attempts: [],
};

function descriptor(fields = {}) {
  return {
    view_id: "0123456789abcdef01234567",
    chat_id: "run-chat",
    experiment_id: experiment.id,
    name: "Loss curves",
    media_type: "text/html",
    state: "temporary",
    created_at: "2026-08-12T08:00:00Z",
    updated_at: "2026-08-12T08:01:00Z",
    expires_at: "2026-08-19T08:00:00Z",
    kept_filename: null,
    kept_at: null,
    can_revise: true,
    ...fields,
  };
}

const chatProps = {
  project,
  node: experiment,
  nodes: { [experiment.id]: experiment },
  runScope: ["repo"],
  tasks: [],
  watchers: [],
  historyMessages: [],
  chatId: "run-chat",
  presentation: "workspace",
  fixedConversation: true,
  onStartTask() {},
  onInspectTask() {},
  onOpenInbox() {},
  onRepairGraphUpdate() {},
  onNewSession() {},
  onClose() {},
  onResumeTask() {},
  onRetryTask() {},
};

test("strict result-view gestures reconstruct only the exact bounded frame message", () => {
  const frame = {};
  const value = {
    type: "rcp-result-view-gesture",
    version: 1,
    gesture: "box",
    description: "steps 8,000 through 9,000 of seed three",
  };
  const reconstructed = resultViewGestureFromFrame({ source: frame, data: value }, frame);
  assert.deepEqual(reconstructed, value);
  assert.notStrictEqual(reconstructed, value);

  assert.equal(resultViewGestureFromFrame({ source: {}, data: value }, frame), null);
  assert.equal(
    resultViewGestureFromFrame({ source: frame, data: { ...value, extra: true } }, frame),
    null,
  );
  assert.equal(
    resultViewGestureFromFrame({ source: frame, data: { ...value, version: 2 } }, frame),
    null,
  );
  assert.equal(
    resultViewGestureFromFrame({ source: frame, data: { ...value, gesture: "click" } }, frame),
    null,
  );
  assert.equal(
    resultViewGestureFromFrame({ source: frame, data: { ...value, description: "  " } }, frame),
    null,
  );
  assert.equal(
    resultViewGestureFromFrame(
      { source: frame, data: { ...value, description: "é".repeat(1025) } },
      frame,
    ),
    null,
  );
});

test("a gesture appends a visible draft and the listener focuses without dispatching", () => {
  assert.equal(
    resultViewGestureDraft("Please inspect this.", "Loss curves", {
      type: "rcp-result-view-gesture",
      version: 1,
      gesture: "underscore",
      description: "samples 4 and 7",
    }),
    "Please inspect this.\n\nUnderscored selection in Loss curves: samples 4 and 7",
  );
  const listener = nodeChatSource.slice(
    nodeChatSource.indexOf("const receiveGesture"),
    nodeChatSource.indexOf('window.addEventListener("message", receiveGesture)'),
  );
  assert.match(listener, /selectMode\("work"\)/);
  assert.match(
    listener,
    /persistResultViewTarget\(\{ action: "revise", view_id: view\.view_id \}\)/,
  );
  assert.match(listener, /setMessage\(next\)/);
  assert.match(nodeChatSource, /if \(message\) writeStorage\(draftKey, message\)/);
  assert.match(listener, /textareaRef\.current\?\.focus\(\)/);
  assert.match(listener, /setSelectionRange\(next\.length, next\.length\)/);
  assert.doesNotMatch(listener, /send\(|onStartTask/);
});

test("typed create and revision requests are explicit Work-only targets", () => {
  const view = descriptor();
  assert.deepEqual(resultViewRequestForTarget("work", { action: "create" }, [view]), {
    action: "create",
  });
  assert.deepEqual(
    resultViewRequestForTarget("work", { action: "revise", view_id: view.view_id }, [view]),
    { action: "revise", view_id: view.view_id },
  );
  assert.equal(
    resultViewRequestForTarget("discuss", { action: "revise", view_id: view.view_id }, [view]),
    undefined,
  );
  assert.equal(
    resultViewRequestForTarget("work", { action: "revise", view_id: "missing" }, [view]),
    undefined,
  );
  const sendPath = nodeChatSource.slice(
    nodeChatSource.indexOf("const send = async"),
    nodeChatSource.indexOf("const repairGraphUpdate"),
  );
  assert.match(sendPath, /resultViewRequestForTarget\(mode, resultViewTarget, resultViews\)/);
  assert.match(
    sendPath,
    /\.\.\.\(resultViewRequest \? \{ result_view: resultViewRequest \} : \{\}\)/,
  );
});

test("result-view create and revision targets round-trip strictly per conversation", () => {
  const view = descriptor();
  const create = { action: "create" };
  const revise = { action: "revise", view_id: view.view_id };

  assert.deepEqual(parseResultViewTarget(serializeResultViewTarget(create)), create);
  assert.deepEqual(parseResultViewTarget(serializeResultViewTarget(revise)), revise);
  assert.equal(serializeResultViewTarget({ action: "none" }), null);
  assert.deepEqual(parseResultViewTarget('{"action":"revise","view_id":"short"}'), {
    action: "none",
  });
  assert.deepEqual(
    parseResultViewTarget(`{"action":"revise","view_id":"${view.view_id}","unexpected":true}`),
    { action: "none" },
  );
  assert.deepEqual(parseResultViewTarget('{"action":"create","view_id":"ignored"}'), {
    action: "none",
  });
  assert.deepEqual(parseResultViewTarget("not-json"), { action: "none" });
  assert.equal(
    resultViewTargetStorageKey("project/one", "chat/one"),
    "rcp:result-view-target:project%2Fone:chat%2Fone",
  );
  assert.notEqual(
    resultViewTargetStorageKey("project/one", "chat/one"),
    resultViewTargetStorageKey("project/one", "chat/two"),
  );
  assert.notEqual(
    resultViewTargetStorageKey("project:a", "chat"),
    resultViewTargetStorageKey("project", "a:chat"),
  );
});

test("a remounted Work composer restores its explicit result-view target", () => {
  const view = descriptor();
  const targetKey = resultViewTargetStorageKey(project.id, chatProps.chatId);
  const previousStorage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem(key) {
      if (key === targetKey) {
        return serializeResultViewTarget({ action: "revise", view_id: view.view_id });
      }
      if (key.includes("rcp:chat-mode:")) return "work";
      return null;
    },
    setItem() {},
    removeItem() {},
  };
  try {
    const pendingHtml = renderToStaticMarkup(
      React.createElement(NodeChat, {
        ...chatProps,
        resultViews: undefined,
        resultViewsError: "The view list is unavailable.",
      }),
    );
    assert.match(pendingHtml, /Selected view \(loading\)/);
    assert.match(pendingHtml, /Result views are still loading/);
    assert.match(pendingHtml, /The view list is unavailable/);

    const html = renderToStaticMarkup(
      React.createElement(NodeChat, { ...chatProps, resultViews: [view] }),
    );
    assert.match(
      html,
      new RegExp(`<option value="revise:${view.view_id}" selected="">Loss curves</option>`),
    );
  } finally {
    if (previousStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previousStorage;
  }
});

test("unavailable targets block dispatch and Discuss visibly clears the persisted target", () => {
  const view = descriptor();
  assert.equal(
    resultViewTargetValidationError("work", { action: "revise", view_id: view.view_id }, undefined),
    "Result views are still loading.",
  );
  assert.match(
    resultViewTargetValidationError(
      "work",
      { action: "revise", view_id: view.view_id },
      [view, descriptor({ can_revise: false })].slice(1),
    ),
    /can no longer be revised/,
  );
  assert.match(
    resultViewTargetValidationError(
      "work",
      { action: "revise", view_id: "89abcdef0123456701234567" },
      [view],
    ),
    /no longer available/,
  );

  const sendPath = nodeChatSource.slice(
    nodeChatSource.indexOf("const send = async"),
    nodeChatSource.indexOf("const repairGraphUpdate"),
  );
  assert.ok(
    sendPath.indexOf('resultViewTarget.action !== "none"') < sendPath.indexOf("await onStartTask"),
  );
  assert.match(sendPath, /setSubmitError\(resultViewTargetError/);
  const selectModePath = nodeChatSource.slice(
    nodeChatSource.indexOf("const selectMode"),
    nodeChatSource.indexOf("const toggleMode"),
  );
  assert.match(
    selectModePath,
    /if \(next === "discuss"\)[\s\S]*persistResultViewTarget\(\{ action: "none" \}\)/,
  );
  const chooseTargetPath = nodeChatSource.slice(
    nodeChatSource.indexOf("const chooseResultViewTarget"),
    nodeChatSource.indexOf("const keepResultViewCard"),
  );
  assert.match(chooseTargetPath, /persistResultViewTarget\(\{ action: "none" \}\)/);
  assert.match(
    sendPath,
    /if \(resultViewRequest\) persistResultViewTarget\(\{ action: "none" \}\)/,
  );
});

test("run conversation renders stable sandboxed cards and exactly one composer", () => {
  const views = [
    descriptor(),
    descriptor({
      view_id: "89abcdef0123456701234567",
      name: "Failure samples",
      state: "kept",
      kept_filename: "failure-samples-project-26-08-12.html",
      kept_at: "2026-08-12T08:03:00Z",
    }),
  ];
  const conversation = React.createElement(NodeChat, {
    ...chatProps,
    resultViews: views,
    resultViewsError: "The latest view list could not be refreshed.",
    onKeepResultView() {},
  });
  const html = renderToStaticMarkup(
    React.createElement(ExperimentRunDetail, {
      run: {
        node: experiment,
        control: withExperimentControlAnswers({
          ready: true,
          reasons: [],
          graph_reasons: [],
          invocations_used: 1,
          invocation_ceiling: 2,
          invocations_remaining: 1,
          episode_id: "episode-1",
          paused: true,
          active: false,
          governing_decisions: [],
          decision_drift: [],
          operational: withTurnAnswers({
            task_active: false,
            detached_work_active: false,
            watcher_degraded: false,
            watcher_completion_pending: false,
            episode_exited: true,
            episode_live: false,
            stop_requested: false,
            stop_settled: false,
            chat_id: null,
            current_operation_id: null,
            current_status: null,
            current_phase: null,
            current_status_message: null,
            current_last_activity_at: null,
            current_invocation: null,
            session: {
              provider: null,
              model: null,
              reasoning: null,
              run_on: null,
              execution_host: null,
              run_truth_scope: null,
              native_session_bound: false,
              diagnostic: null,
            },
          }),
          health: "completed",
          recommendation: "none",
          run_section: "completed",
        }),
        taskGroup: null,
        currentTask: null,
        watchers: [],
        currentWatchers: [],
        watcherItems: [],
        health: "completed",
      },
      runBusy: false,
      runDisabled: false,
      stopBusy: false,
      recoveryBusy: false,
      conversation,
      onRun() {},
      onStopLoop() {},
      onRecover() {},
      onSwitchProvider() {},
    }),
  );

  assert.match(html, /aria-label="Run conversation"[\s\S]*aria-label="Result views"/);
  assert.equal(html.match(/class="result-view-card /g)?.length, 2);
  assert.equal(html.match(/sandbox="allow-scripts"/g)?.length, 2);
  assert.equal(html.match(/>Keep<\/button>/g)?.length, 1);
  assert.match(html, />temporary</);
  assert.match(html, />kept</);
  assert.match(html, /The latest view list could not be refreshed/);
  assert.match(html, /<option value="create">New view<\/option>/);
  assert.match(html, /<option value="revise:[^"]+">Loss curves<\/option>/);
  assert.equal(html.match(/class="chat-composer/g)?.length, 1);
  assert.doesNotMatch(html, /New session/);
  assert.doesNotMatch(html, /<nav|>Open<|>Download</);
  assert.match(nodeChatSource, /key=\{view\.view_id\}/);
  assert.doesNotMatch(nodeChatSource, /key=\{`\$\{view\.view_id\}:\$\{view\.updated_at\}`\}/);
  const keepPath = nodeChatSource.slice(
    nodeChatSource.indexOf("const keepResultViewCard"),
    nodeChatSource.indexOf("const stopDictation"),
  );
  assert.match(keepPath, /await onKeepResultView\(viewId\)/);
  assert.match(keepPath, /withMapValue\(current, viewId/);
  assert.match(
    nodeChatSource,
    /\{keepError && \([\s\S]*className="result-view-error" role="alert"/,
  );
});

test("preview and Keep API bindings use the result-view endpoints and version URL", async () => {
  assert.equal(
    resultViewPreviewUrl("project/one", descriptor()),
    "/api/projects/project%2Fone/result-views/0123456789abcdef01234567/preview?updated_at=2026-08-12T08%3A01%3A00Z",
  );
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, init) => {
    calls.push([String(path), init?.method ?? "GET"]);
    return new Response(
      JSON.stringify(init?.method === "POST" ? descriptor({ state: "kept" }) : []),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  };
  try {
    await loadResultViews("/api/projects/project%2Fone", "experiment/result", "chat one");
    await keepResultView("/api/projects/project%2Fone", "view/one");
  } finally {
    globalThis.fetch = previousFetch;
  }
  assert.deepEqual(calls, [
    [
      "/api/projects/project%2Fone/result-views?experiment_id=experiment%2Fresult&chat_id=chat+one",
      "GET",
    ],
    ["/api/projects/project%2Fone/result-views/view%2Fone/keep", "POST"],
  ]);
});

test("result-view loads and Keep are fenced to the exact selected tuple generation", () => {
  const first = resultViewSelectionKey("project", "experiment:a", "chat:b");
  const delimiterCollision = resultViewSelectionKey("project", "experiment", "a:chat:b");
  assert.notEqual(first, delimiterCollision);
  assert.equal(resultViewSelectionKey("project", "experiment", null), null);
  assert.equal(resultViewSelectionIsCurrent(first, 4, first, 4), true);
  assert.equal(resultViewSelectionIsCurrent(first, 4, first, 5), false);
  assert.equal(resultViewSelectionIsCurrent(first, 4, delimiterCollision, 4), false);
  assert.equal(resultViewLoadIsCurrent(first, 4, 8, first, 4, 8), true);
  assert.equal(resultViewLoadIsCurrent(first, 4, 8, first, 4, 9), false);
  assert.equal(resultViewLoadIsCurrent(first, 4, 8, first, 5, 8), false);
  assert.equal(resultViewLoadIsCurrent(first, 4, 8, delimiterCollision, 4, 8), false);

  const refreshPath = episodeDialogsSource.slice(
    episodeDialogsSource.indexOf("const refreshResultViews"),
    episodeDialogsSource.indexOf("const keepSelectedResultView"),
  );
  assert.match(refreshPath, /const loadGeneration = \+\+resultViewLoadGeneration\.current/);
  assert.match(refreshPath, /resultViewLoadIsCurrent\(/);

  const keepPath = episodeDialogsSource.slice(
    episodeDialogsSource.indexOf("const keepSelectedResultView"),
    episodeDialogsSource.indexOf("const selectedResultViews"),
  );
  assert.ok(keepPath.indexOf("const selectionKey") < keepPath.indexOf("await keepResultView"));
  assert.match(keepPath, /resultViewSelectionIsCurrent\(/);
  assert.match(keepPath, /resultViewLoadGeneration\.current \+= 1/);
  assert.doesNotMatch(keepPath, /refreshResultViews/);
  assert.match(keepPath, /view\.view_id === kept\.view_id \? kept : view/);
});

test("Runs makes its embedded chat transcript visible and deduplicates a floating copy", () => {
  assert.deepEqual(visibleChatTranscriptIds("execution", "other-chat", "run-chat", "run-chat"), [
    "run-chat",
  ]);
  assert.equal(visibleUnreadChatId("execution", "other-chat", "run-chat"), "run-chat");
  assert.deepEqual(visibleChatTranscriptIds("chats", "selected-chat", null, null), [
    "selected-chat",
  ]);
  assert.equal(shouldLoadVisibleChatTranscript("run-chat", [], "run-chat"), true);
  assert.equal(shouldLoadVisibleChatTranscript("draft-chat", [], "run-chat"), false);
  assert.equal(
    shouldLoadVisibleChatTranscript("summary-chat", [{ chat_id: "summary-chat" }], "run-chat"),
    true,
  );
  assert.match(
    appSource,
    /\{floatingChat && floatingChat\.chatId !== selectedExperimentChatId && \(/,
  );
  assert.match(
    appSource,
    /floatingChat\?\.chatId === selectedExperimentChatId\)[\s\S]*setFloatingChat\(null\)/,
  );
});
