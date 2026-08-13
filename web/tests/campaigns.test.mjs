import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  loadCampaignMessages,
  loadCampaigns,
  reauthorizeCampaign,
  sendCampaignMessage,
  startCampaign,
  stopCampaign,
} from "../src/api.ts";
import {
  campaignProjection,
  campaignReportPreviewUrl,
  campaignTaskRows,
  isLiveCampaign,
} from "../src/campaigns.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const {
  AutoResearchDialog,
  handleAutoResearchDialogKeyDown,
  makeAutoResearchDialogBackgroundInert,
  restoreAutoResearchDialogFocus,
} = await server.ssrLoadModule("/src/components/AutoResearchDialog.tsx");
const { CampaignRuns } = await server.ssrLoadModule("/src/components/CampaignRuns.tsx");

after(() => server.close());

const campaign = {
  campaign_id: "campaign/alpha",
  project_id: "project one",
  root_operation_id: "turn-root",
  current_orchestrator_task_id: "turn-root",
  current_control_task_id: "turn-root",
  recovery: null,
  status: "running",
  starting_instruction: "Begin with the unresolved **Blocker**.",
  budget: {
    invocation_ceiling: 8,
    invocations_used: 3,
    invocations_remaining: 5,
    report_units_reserved: 1,
    observed_input_tokens: 12_345,
    observed_generated_tokens: 678,
  },
  authorized_by: { space_id: "space", user_id: "human", display_name: "Ada" },
  stop_requested_at: null,
  ending: null,
  error: null,
  created_at: "2026-08-12T08:00:00Z",
  updated_at: "2026-08-12T08:02:00Z",
  ended_at: null,
  tasks: [],
  reports: [
    {
      report_id: "report/exhausted",
      ending: "exhausted",
      created_at: "2026-08-12T08:01:00Z",
    },
  ],
  can_stop: true,
  can_reauthorize: false,
};

const rootTask = {
  operation_id: "turn-root",
  project_id: "project one",
  kind: "campaign",
  status: "running",
  request: { role: "orchestrator" },
  created_at: "2026-08-12T08:00:00Z",
  updated_at: "2026-08-12T08:00:00Z",
  status_message: "Reviewing the graph",
  attempt: 1,
  parent_operation_id: null,
  campaign_id: "campaign/alpha",
  estimate_seconds: 10,
  estimate_samples: 1,
  phase: "running",
  elapsed_seconds: 1,
  progress: 0.3,
  can_pause: false,
  can_resume: false,
  can_retry: false,
};

const workerTask = {
  ...rootTask,
  operation_id: "turn-worker",
  kind: "node_chat",
  request: { role: "worker", control_node_id: "experiment/demo" },
  parent_operation_id: "turn-root",
  status_message: "Running the experiment",
};

campaign.tasks = [rootTask, workerTask];

function projectionSummary(value, tasks = value.tasks) {
  const projection = campaignProjection(value, tasks);
  return {
    health: projection.health,
    label: projection.healthLabel,
    recommendation: projection.recommendation.kind,
    recommendationLabel: projection.recommendation.label,
    control: projection.taskControl?.kind ?? null,
    controlTaskId: projection.taskControl?.task.operation_id ?? null,
  };
}

function assertCampaignProjectionViews(html, healthLabel, recommendationLabel) {
  const healthViews = [
    ...html.matchAll(/<div class="campaign-run-health[^"]*"[^>]*>(.*?)<\/div>/gs),
  ];
  assert.equal(healthViews.length, 1);
  assert.equal(healthViews[0][1], `<strong>${healthLabel}</strong>`);

  const compactRecommendations = [
    ...html.matchAll(/<span class="campaign-run-summary">([^<]+)<\/span>/g),
  ];
  assert.equal(compactRecommendations.length, 1);
  assert.equal(compactRecommendations[0][1], recommendationLabel);

  const recommendationViews = [
    ...html.matchAll(/<div class="campaign-run-recommendation[^"]*">(.*?)<\/div>/gs),
  ];
  assert.equal(recommendationViews.length, 1);
  assert.equal((html.match(/Recommended next step/g) ?? []).length, 1);
  assert.match(recommendationViews[0][1], /<span class="eyebrow">Recommended next step<\/span>/);
  const detailRecommendation = recommendationViews[0][1].match(/<strong>([^<]+)<\/strong>/)?.[1];
  assert.equal(detailRecommendation, compactRecommendations[0][1]);
}

test("authorization asks for one invocation budget and an optional starting instruction", () => {
  const html = renderToStaticMarkup(
    React.createElement(AutoResearchDialog, {
      open: true,
      busy: false,
      error: null,
      initialInvocationCeiling: 10,
      onClose() {},
      onAuthorize() {},
    }),
  );
  assert.match(html, /Authorize auto-research/);
  assert.match(html, /role="dialog" aria-modal="true"/);
  assert.match(html, /Invocation budget/);
  assert.match(html, /1 reserved for the report/);
  assert.match(html, /type="number" min="2" step="1"/);
  assert.match(html, /value="10"/);
  assert.match(html, /Starting instruction \(optional\)/);
  assert.equal(html.match(/<textarea/g)?.length, 1);
  assert.doesNotMatch(html, /placeholder=/);
});

test("authorization traps and recaptures focus while Escape respects a busy start", () => {
  const focused = [];
  const first = focusTarget("first", focused);
  const middle = focusTarget("middle", focused);
  const last = focusTarget("last", focused);
  const dialog = {
    contains(element) {
      return [first, middle, last].includes(element);
    },
    focus() {
      focused.push("dialog");
    },
    querySelectorAll() {
      return [first, middle, last];
    },
  };
  let closeCount = 0;

  const forward = keyEvent("Tab");
  assert.equal(
    handleAutoResearchDialogKeyDown(forward, dialog, last, false, () => closeCount++),
    true,
  );
  assert.equal(forward.prevented, true);
  assert.deepEqual(focused, ["first"]);

  const backward = keyEvent("Tab", true);
  assert.equal(
    handleAutoResearchDialogKeyDown(backward, dialog, first, false, () => closeCount++),
    true,
  );
  assert.deepEqual(focused, ["first", "last"]);

  const recaptured = keyEvent("Tab");
  assert.equal(
    handleAutoResearchDialogKeyDown(recaptured, dialog, {}, false, () => closeCount++),
    true,
  );
  assert.deepEqual(focused, ["first", "last", "first"]);

  const internal = keyEvent("Tab");
  assert.equal(
    handleAutoResearchDialogKeyDown(internal, dialog, middle, false, () => closeCount++),
    false,
  );
  assert.equal(internal.prevented, false);

  const busyEscape = keyEvent("Escape");
  assert.equal(
    handleAutoResearchDialogKeyDown(busyEscape, dialog, middle, true, () => closeCount++),
    false,
  );
  assert.equal(busyEscape.prevented, false);
  assert.equal(closeCount, 0);

  const escape = keyEvent("Escape");
  assert.equal(
    handleAutoResearchDialogKeyDown(escape, dialog, middle, false, () => closeCount++),
    true,
  );
  assert.equal(escape.prevented, true);
  assert.equal(closeCount, 1);
});

test("authorization inerts the background and restores its prior focus and inert state", () => {
  const background = treeElement(false);
  const dialog = treeElement(false);
  const backdrop = treeElement(false, [dialog]);
  const alreadyInert = treeElement(true);
  treeElement(false, [background, backdrop, alreadyInert]);

  const restoreBackground = makeAutoResearchDialogBackgroundInert(dialog);
  assert.equal(background.inert, true);
  assert.equal(alreadyInert.inert, true);
  assert.equal(backdrop.inert, false);
  assert.equal(dialog.inert, false);

  restoreBackground();
  assert.equal(background.inert, false);
  assert.equal(alreadyInert.inert, true);

  const focused = [];
  restoreAutoResearchDialogFocus({
    isConnected: true,
    focus() {
      focused.push("trigger");
    },
  });
  restoreAutoResearchDialogFocus({
    isConnected: false,
    focus() {
      focused.push("detached");
    },
  });
  assert.deepEqual(focused, ["trigger"]);
});

test("the campaign parent owns the only meter, nested worker, mail, stop, and report controls", () => {
  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [campaign],
      tasks: [rootTask, workerTask],
      messagesByCampaign: {
        "campaign/alpha": [
          {
            message_id: "message-1",
            campaign_id: "campaign/alpha",
            sender_role: "worker",
            sender_task_id: "turn-worker",
            authorized_by: null,
            recipient_task_id: "turn-root",
            control_node_id: "experiment/demo",
            body: "The run is still active.",
            created_at: "2026-08-12T08:03:00Z",
            delivered_at: "2026-08-12T08:03:01Z",
            delivery_operation_id: "delivery-1",
          },
          {
            message_id: "message-2",
            campaign_id: "campaign/alpha",
            sender_role: "human",
            sender_task_id: null,
            authorized_by: {
              space_id: "space",
              user_id: "later-human",
              display_name: "Grace",
            },
            recipient_task_id: "turn-root",
            control_node_id: null,
            body: "Please check the conflicting result.",
            created_at: "2026-08-12T08:04:00Z",
            delivered_at: "2026-08-12T08:04:01Z",
            delivery_operation_id: "delivery-2",
          },
          {
            message_id: "message-legacy",
            campaign_id: "campaign/alpha",
            sender_role: "human",
            sender_task_id: null,
            authorized_by: null,
            recipient_task_id: "turn-root",
            control_node_id: null,
            body: "Legacy steering message.",
            created_at: "2026-08-12T08:05:00Z",
            delivered_at: null,
            delivery_operation_id: null,
          },
        ],
      },
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );
  assert.equal(html.match(/role="meter"/g)?.length, 1);
  const toggle = html.match(/<button class="campaign-run-toggle"[^>]*>(.*?)<\/button>/s);
  assert.ok(toggle);
  assert.doesNotMatch(toggle[1], /role="meter"/);
  assert.match(
    html,
    /role="meter" aria-label="3 of 8 campaign invocations used; 1 reserved for the report;/,
  );
  assert.match(html, /3 \/ 8 invocations/);
  assert.match(html, /12,345 input/);
  assert.match(html, /678 generated/);
  assert.match(html, /status-pill active">Active/);
  assertCampaignProjectionViews(html, "Active", "Let auto-research continue");
  assert.match(html, /campaign-task depth-1/);
  assert.match(html, />Worker</);
  assert.match(html, /experiment\/demo/);
  assert.match(html, /<strong>Worker · experiment\/demo<\/strong>/);
  assert.match(html, /<strong>Grace<\/strong>/);
  assert.match(html, /<strong>Unattributed<\/strong>/);
  assert.doesNotMatch(html, /<strong>Ada<\/strong>/);
  assert.match(html, /Message orchestrator/);
  assert.doesNotMatch(html, /Message worker/);
  assert.match(html, />Stop</);
  assert.match(html, /Open Exhausted report/);
  assert.match(
    html,
    /href="\/api\/projects\/project%20one\/campaigns\/campaign%2Falpha\/reports\/report%2Fexhausted\/preview"/,
  );
  // A sandboxed report, and a 404 for one that aged out, both belong in their own
  // tab rather than replacing the app the human is working in.
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test("same-ending reports have distinct visible timestamps and accessible names", () => {
  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [
        {
          ...campaign,
          reports: [
            campaign.reports[0],
            {
              ...campaign.reports[0],
              report_id: "report/exhausted-again",
              created_at: "2026-08-12T08:01:30Z",
            },
          ],
        },
      ],
      tasks: [rootTask],
      messagesByCampaign: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );
  const names = [...html.matchAll(/aria-label="(Open Exhausted report from [^"]+)"/g)].map(
    (match) => match[1],
  );
  const visibleTimes = [
    ...html.matchAll(/<time dateTime="2026-08-12T08:01:[^"]+">([^<]+)<\/time>/g),
  ].map((match) => match[1]);
  assert.equal(names.length, 2);
  assert.notEqual(names[0], names[1]);
  assert.equal(visibleTimes.length, 2);
  assert.notEqual(visibleTimes[0], visibleTimes[1]);
});

test("Needs action exposes only additive reauthorization", () => {
  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [
        {
          ...campaign,
          status: "needs_action",
          ending: "exhausted",
          can_stop: false,
          can_reauthorize: true,
        },
      ],
      tasks: [rootTask],
      messagesByCampaign: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );
  assertCampaignProjectionViews(html, "Needs action", "Add invocations to continue");
  assert.match(html, /aria-label="Additional campaign invocations"/);
  assert.match(html, />Reauthorize</);
  assert.doesNotMatch(html, /Message orchestrator/);
});

test("a raw running campaign with a failed retryable control projects only parent recovery", () => {
  const failedRoot = {
    ...rootTask,
    status: "failed",
    can_retry: true,
    status_message: "Provider connection failed",
  };
  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [{ ...campaign, tasks: [failedRoot] }],
      tasks: [failedRoot],
      messagesByCampaign: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );

  assert.match(html, /class="campaign-run needs_action"/);
  assertCampaignProjectionViews(html, "Needs action", "Retry the current turn");
  assert.match(html, /<button class="button compact secondary" type="button">.*Retry<\/button>/s);
  assert.doesNotMatch(html, /status-pill running/);
  assert.match(html, /status-pill failed">Failed/);
});

test("a healthy pausable campaign recommends continuing while Pause stays a control", () => {
  const pausable = { ...rootTask, can_pause: true };
  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [{ ...campaign, tasks: [pausable] }],
      tasks: [pausable],
      messagesByCampaign: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );

  assert.match(html, /class="campaign-run active"/);
  assert.match(html, /status-pill active">Active/);
  assertCampaignProjectionViews(html, "Active", "Let auto-research continue");
  assert.match(html, /<button class="button compact secondary" type="button">.*Pause<\/button>/s);
  assert.match(html, />Stop<\/button>/);
  assert.doesNotMatch(html, /Pause the orchestrator/);
});

test("campaign helpers retain one live lineage and encode the report URL", () => {
  assert.equal(isLiveCampaign(campaign), true);
  assert.equal(isLiveCampaign({ ...campaign, status: "succeeded" }), false);
  assert.deepEqual(
    campaignTaskRows(campaign, [workerTask, rootTask]).map(({ task, role, depth }) => [
      task.operation_id,
      role,
      depth,
    ]),
    [
      ["turn-root", "orchestrator", 0],
      ["turn-worker", "worker", 1],
    ],
  );
  assert.equal(
    campaignReportPreviewUrl("project one", "campaign/alpha", "report/exhausted"),
    "/api/projects/project%20one/campaigns/campaign%2Falpha/reports/report%2Fexhausted/preview",
  );
});

test("campaign health and recommendation follow durable control precedence", () => {
  assert.deepEqual(projectionSummary(campaign, [{ ...rootTask, can_pause: true }]), {
    health: "active",
    label: "Active",
    recommendation: "continue",
    recommendationLabel: "Let auto-research continue",
    control: "pause",
    controlTaskId: rootTask.operation_id,
  });
  assert.deepEqual(
    projectionSummary(campaign, [{ ...rootTask, status: "pausing", can_pause: false }]),
    {
      health: "active",
      label: "Active",
      recommendation: "wait",
      recommendationLabel: "Wait for the current turn to pause",
      control: null,
      controlTaskId: null,
    },
  );
  assert.deepEqual(
    projectionSummary(campaign, [{ ...rootTask, status: "paused", can_resume: true }]),
    {
      health: "needs_action",
      label: "Needs action",
      recommendation: "resume",
      recommendationLabel: "Resume the current turn",
      control: "resume",
      controlTaskId: rootTask.operation_id,
    },
  );
  assert.deepEqual(
    projectionSummary(campaign, [{ ...rootTask, status: "failed", can_retry: true }]),
    {
      health: "needs_action",
      label: "Needs action",
      recommendation: "retry",
      recommendationLabel: "Retry the current turn",
      control: "retry",
      controlTaskId: rootTask.operation_id,
    },
  );
  assert.equal(
    projectionSummary(campaign, [
      { ...rootTask, status: "interrupted", can_resume: true, can_retry: true },
    ]).control,
    "retry",
  );
  assert.deepEqual(
    projectionSummary(
      { ...campaign, status: "needs_action", ending: "exhausted", can_reauthorize: true },
      [rootTask],
    ),
    {
      health: "needs_action",
      label: "Needs action",
      recommendation: "reauthorize",
      recommendationLabel: "Add invocations to continue",
      control: null,
      controlTaskId: null,
    },
  );
  assert.deepEqual(projectionSummary({ ...campaign, status: "succeeded" }, [rootTask]), {
    health: "completed",
    label: "Completed",
    recommendation: "open_report",
    recommendationLabel: "Open the concluding report",
    control: null,
    controlTaskId: null,
  });
  assert.deepEqual(
    projectionSummary({ ...campaign, status: "queued" }, [{ ...rootTask, status: "queued" }]),
    {
      health: "starting",
      label: "Starting",
      recommendation: "wait",
      recommendationLabel: "Wait for auto-research to start",
      control: null,
      controlTaskId: null,
    },
  );
  assert.deepEqual(projectionSummary({ ...campaign, status: "stopping" }, [rootTask]), {
    health: "stopping",
    label: "Stopping gracefully",
    recommendation: "wait",
    recommendationLabel: "Wait for the current turn to finish",
    control: null,
    controlTaskId: null,
  });
  assert.deepEqual(
    projectionSummary(
      {
        ...campaign,
        status: "stopping",
        recovery: {
          purpose: "task",
          status: "pending",
          retry_mode: "exact",
          operation_id: rootTask.operation_id,
          attempts: 1,
          max_attempts: 8,
          next_attempt_at: "2026-08-12T08:10:00Z",
        },
      },
      [{ ...rootTask, status: "paused", can_resume: true }],
    ),
    {
      health: "recovering",
      label: "Recovering",
      recommendation: "wait",
      recommendationLabel: "Wait for automatic turn recovery",
      control: null,
      controlTaskId: null,
    },
  );
  assert.deepEqual(
    projectionSummary(
      {
        ...campaign,
        status: "wrapping_up",
        current_control_task_id: null,
        recovery: null,
      },
      [rootTask],
    ),
    {
      health: "writing_report",
      label: "Writing report",
      recommendation: "wait",
      recommendationLabel: "Wait for the concluding report",
      control: null,
      controlTaskId: null,
    },
  );
  assert.deepEqual(projectionSummary({ ...campaign, status: "failed", reports: [] }, [rootTask]), {
    health: "failed",
    label: "Failed",
    recommendation: "review",
    recommendationLabel: "Review the campaign failure",
    control: null,
    controlTaskId: null,
  });
});

test("stopping projects graceful wait without retaining an invalid Stop control", () => {
  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [{ ...campaign, status: "stopping", can_stop: true }],
      tasks: [rootTask],
      messagesByCampaign: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );

  assertCampaignProjectionViews(html, "Stopping gracefully", "Wait for the current turn to finish");
  assert.doesNotMatch(html, />Stop<\/button>/);
});

test("stopping with a paused exact control projects parent Resume instead of graceful wait", () => {
  const pausedWorker = {
    ...workerTask,
    status: "paused",
    can_resume: true,
    can_retry: true,
    status_message: "Paused at its checkpoint",
  };
  const stopping = {
    ...campaign,
    status: "stopping",
    current_control_task_id: pausedWorker.operation_id,
    can_stop: false,
    tasks: [rootTask, pausedWorker],
  };
  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [stopping],
      tasks: [rootTask, pausedWorker],
      messagesByCampaign: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );

  assertCampaignProjectionViews(html, "Needs action", "Resume the current turn");
  assert.match(html, /<button class="button compact primary" type="button">.*Resume<\/button>/s);
  assert.match(html, /status-pill paused">Paused/);
  assert.doesNotMatch(html, /Stopping gracefully|status-pill stopping|>Stop<\/button>/);
});

test("wrap-up waits without a report task and targets only the latest report attempt", () => {
  const waiting = {
    ...campaign,
    status: "wrapping_up",
    ending: "failed",
    can_stop: false,
    current_orchestrator_task_id: "turn-root",
    current_control_task_id: null,
    recovery: {
      purpose: "report_admission",
      status: "pending",
      retry_mode: "report_admission",
      operation_id: null,
      attempts: 0,
      max_attempts: 8,
      next_attempt_at: "2026-08-12T08:10:00Z",
    },
  };
  assert.deepEqual(projectionSummary(waiting, [{ ...rootTask, status: "failed" }]), {
    health: "recovering",
    label: "Recovering",
    recommendation: "wait",
    recommendationLabel: "Wait for automatic report recovery",
    control: null,
    controlTaskId: null,
  });

  const reportTask = {
    ...rootTask,
    operation_id: "turn-report-2",
    parent_operation_id: "turn-report-1",
    request: { role: "report", ending: "failed" },
    status: "failed",
    can_retry: true,
    status_message: "Report provider failed",
  };
  const reporting = {
    ...waiting,
    current_control_task_id: reportTask.operation_id,
    recovery: {
      ...waiting.recovery,
      purpose: "task",
      status: "blocked",
      retry_mode: "exact",
      operation_id: reportTask.operation_id,
    },
  };
  const projection = campaignProjection(reporting, [
    { ...rootTask, status: "failed", can_retry: true },
    reportTask,
  ]);
  assert.equal(projection.health, "needs_action");
  assert.equal(projection.recommendation.kind, "retry");
  assert.equal(projection.taskControl?.task.operation_id, reportTask.operation_id);

  const html = renderToStaticMarkup(
    React.createElement(CampaignRuns, {
      campaigns: [waiting],
      tasks: [{ ...rootTask, status: "failed", can_retry: true }],
      messagesByCampaign: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOpenReport() {},
      async onOperateTask() {},
    }),
  );
  assertCampaignProjectionViews(html, "Recovering", "Wait for automatic report recovery");
  assert.doesNotMatch(html, />Retry</);
});

test("the campaign parent resumes the exact paused worker that blocks wrap-up", () => {
  const pausedWorker = {
    ...workerTask,
    status: "paused",
    can_resume: true,
    can_retry: true,
    status_message: "Paused at its checkpoint",
  };
  const waiting = {
    ...campaign,
    status: "wrapping_up",
    ending: "completed",
    can_stop: false,
    current_control_task_id: pausedWorker.operation_id,
    tasks: [rootTask, pausedWorker],
  };

  const projection = campaignProjection(waiting, [rootTask, pausedWorker]);

  assert.equal(projection.health, "needs_action");
  assert.equal(projection.recommendation.kind, "resume");
  assert.equal(projection.taskControl?.task.operation_id, pausedWorker.operation_id);
});

test("a paid worker continuation is visibly classified as a wake", () => {
  const workerWake = {
    ...workerTask,
    operation_id: "turn-worker-wake",
    kind: "campaign",
    request: {
      role: "worker",
      control_node_id: "experiment/demo",
      wake_cause: "message",
    },
    parent_operation_id: "turn-worker",
  };

  assert.deepEqual(
    campaignTaskRows(campaign, [rootTask, workerTask, workerWake]).map(({ task, role }) => [
      task.operation_id,
      role,
    ]),
    [
      ["turn-root", "orchestrator"],
      ["turn-worker", "worker"],
      ["turn-worker-wake", "wake"],
    ],
  );
});

test("campaign API calls keep every endpoint and mutation body isolated", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, init = {}) => {
    requests.push({ path, method: init.method ?? "GET", body: init.body ?? null });
    const payload = path.endsWith("/messages")
      ? init.method === "POST"
        ? { message_id: "message" }
        : []
      : path.endsWith("/campaigns") && !init.method
        ? []
        : campaign;
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await loadCampaigns("/api/projects/demo");
    await startCampaign("/api/projects/demo", {
      invocation_ceiling: 8,
      starting_instruction: "Start here",
    });
    await stopCampaign("/api/projects/demo", "campaign/alpha");
    await reauthorizeCampaign("/api/projects/demo", "campaign/alpha", 4);
    await loadCampaignMessages("/api/projects/demo", "campaign/alpha");
    await sendCampaignMessage("/api/projects/demo", "campaign/alpha", "Check the blocker");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(requests, [
    { path: "/api/projects/demo/campaigns", method: "GET", body: null },
    {
      path: "/api/projects/demo/campaigns",
      method: "POST",
      body: JSON.stringify({ invocation_ceiling: 8, starting_instruction: "Start here" }),
    },
    {
      path: "/api/projects/demo/campaigns/campaign%2Falpha/stop",
      method: "POST",
      body: null,
    },
    {
      path: "/api/projects/demo/campaigns/campaign%2Falpha/reauthorize",
      method: "POST",
      body: JSON.stringify({ additional_invocations: 4 }),
    },
    {
      path: "/api/projects/demo/campaigns/campaign%2Falpha/messages",
      method: "GET",
      body: null,
    },
    {
      path: "/api/projects/demo/campaigns/campaign%2Falpha/messages",
      method: "POST",
      body: JSON.stringify({ body: "Check the blocker" }),
    },
  ]);
});

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

function keyEvent(key, shiftKey = false) {
  return {
    key,
    shiftKey,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
}

function treeElement(inert, children = []) {
  const element = { inert, children, parentElement: null };
  for (const child of children) child.parentElement = element;
  return element;
}
