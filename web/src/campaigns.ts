import type { AgentTask, Campaign, CampaignEnding, CampaignStatus } from "./types";

const LIVE_CAMPAIGN_STATUSES = new Set<CampaignStatus>([
  "queued",
  "running",
  "stopping",
  "wrapping_up",
  "needs_action",
]);

const CAMPAIGN_HEALTH_LABELS: Record<CampaignHealth, string> = {
  starting: "Starting",
  active: "Active",
  recovering: "Recovering",
  needs_action: "Needs action",
  stopping: "Stopping gracefully",
  writing_report: "Writing report",
  completed: "Completed",
  stopped: "Stopped",
  failed: "Failed",
};

export type CampaignTaskRole = "orchestrator" | "worker" | "wake" | "report";

export interface CampaignTaskRow {
  task: AgentTask;
  role: CampaignTaskRole;
  depth: number;
}

export type CampaignHealth =
  | "starting"
  | "active"
  | "recovering"
  | "needs_action"
  | "stopping"
  | "writing_report"
  | "completed"
  | "stopped"
  | "failed";

export type CampaignRecommendationKind =
  "continue" | "wait" | "resume" | "retry" | "reauthorize" | "open_report" | "review" | "none";

export interface CampaignRecommendation {
  kind: CampaignRecommendationKind;
  label: string;
  task: AgentTask | null;
  reportId: string | null;
}

export interface CampaignTaskControl {
  kind: "pause" | "resume" | "retry";
  task: AgentTask;
}

type CampaignRecoveryControl = CampaignTaskControl & { kind: "resume" | "retry" };

export interface CampaignProjection {
  health: CampaignHealth;
  healthLabel: string;
  recommendation: CampaignRecommendation;
  taskControl: CampaignTaskControl | null;
}

interface PreviewTarget {
  opener: Window | null;
  location: { replace(url: string): void };
  close(): void;
}

export function isLiveCampaign(campaign: Campaign): boolean {
  return LIVE_CAMPAIGN_STATUSES.has(campaign.status);
}

export function currentCampaignControlTask(
  campaign: Campaign,
  tasks: AgentTask[] = campaign.tasks,
): AgentTask | null {
  if (!campaign.current_control_task_id) return null;
  return tasks.find((task) => task.operation_id === campaign.current_control_task_id) ?? null;
}

export function campaignProjection(
  campaign: Campaign,
  tasks: AgentTask[] = campaign.tasks,
): CampaignProjection {
  const task = currentCampaignControlTask(campaign, tasks);
  if (
    campaign.status === "succeeded" ||
    campaign.status === "stopped" ||
    campaign.status === "failed"
  ) {
    const report = campaign.reports.at(-1);
    return projectCampaign(
      campaign.status === "succeeded" ? "completed" : campaign.status,
      report
        ? recommendation("open_report", "Open the concluding report", task, report.report_id)
        : recommendation(
            campaign.status === "failed" ? "review" : "none",
            campaign.status === "succeeded"
              ? "No further action is needed"
              : campaign.status === "stopped"
                ? "No further action is available"
                : "Review the campaign failure",
            task,
          ),
    );
  }
  if (campaign.recovery?.status === "pending") {
    return projectCampaign(
      "recovering",
      recommendation(
        "wait",
        campaign.recovery.purpose === "report_admission"
          ? "Wait for automatic report recovery"
          : "Wait for automatic turn recovery",
        task,
      ),
    );
  }
  if (
    campaign.status === "needs_action" &&
    campaign.ending === "exhausted" &&
    campaign.can_reauthorize
  ) {
    return projectCampaign(
      "needs_action",
      recommendation("reauthorize", "Add invocations to continue", task),
    );
  }
  const recoveryControl = campaignRecoveryControl(task);
  if (recoveryControl) {
    return projectCampaign(
      "needs_action",
      recommendation(
        recoveryControl.kind,
        recoveryControl.kind === "resume" ? "Resume the current turn" : "Retry the current turn",
        recoveryControl.task,
      ),
      recoveryControl,
    );
  }
  if (campaign.status === "stopping") {
    return projectCampaign(
      "stopping",
      recommendation("wait", "Wait for the current turn to finish", task),
    );
  }
  if (
    task &&
    (task.status === "paused" || task.status === "interrupted" || task.status === "failed")
  ) {
    return projectCampaign(
      "needs_action",
      recommendation("review", "Review the blocked turn", task),
    );
  }
  if (campaign.status === "wrapping_up") {
    return projectCampaign(
      "writing_report",
      recommendation("wait", "Wait for the concluding report", task),
      pauseControl(task),
    );
  }
  if (campaign.status === "queued" || task?.status === "queued") {
    return projectCampaign(
      "starting",
      recommendation("wait", "Wait for auto-research to start", task),
    );
  }
  if (campaign.status === "needs_action") {
    return projectCampaign(
      "needs_action",
      recommendation("review", "Review the campaign state", task),
    );
  }
  if (task?.status === "pausing") {
    return projectCampaign(
      "active",
      recommendation("wait", "Wait for the current turn to pause", task),
    );
  }
  return projectCampaign(
    "active",
    recommendation("continue", "Let auto-research continue", task),
    pauseControl(task),
  );
}

function projectCampaign(
  health: CampaignHealth,
  recommendation: CampaignRecommendation,
  taskControl: CampaignTaskControl | null = null,
): CampaignProjection {
  return {
    health,
    healthLabel: CAMPAIGN_HEALTH_LABELS[health],
    recommendation,
    taskControl,
  };
}

function recommendation(
  kind: CampaignRecommendationKind,
  label: string,
  task: AgentTask | null,
  reportId: string | null = null,
): CampaignRecommendation {
  return { kind, label, task, reportId };
}

function campaignRecoveryControl(task: AgentTask | null): CampaignRecoveryControl | null {
  if (!task) return null;
  if (task.status === "paused") {
    if (task.can_resume) return { kind: "resume", task };
    if (task.can_retry) return { kind: "retry", task };
  }
  if (task.status === "interrupted" || task.status === "failed") {
    if (task.can_retry) return { kind: "retry", task };
    if (task.can_resume) return { kind: "resume", task };
  }
  return null;
}

function pauseControl(task: AgentTask | null): CampaignTaskControl | null {
  return task?.status === "running" && task.can_pause ? { kind: "pause", task } : null;
}

export function campaignEndingLabel(ending: CampaignEnding): string {
  switch (ending) {
    case "completed":
      return "Completed";
    case "exhausted":
      return "Exhausted";
    case "stopped":
      return "Stopped";
    case "failed":
      return "Failed";
  }
}

export function campaignReportPreviewUrl(
  projectId: string,
  campaignId: string,
  reportId: string,
): string {
  return `/api/projects/${encodeURIComponent(projectId)}/campaigns/${encodeURIComponent(campaignId)}/reports/${encodeURIComponent(reportId)}/preview`;
}

export async function openCampaignReportPreview(
  url: string,
  openTarget: () => PreviewTarget | null = () => window.open("about:blank", "_blank"),
  fetcher: typeof fetch = fetch,
): Promise<void> {
  const target = openTarget();
  if (!target) throw new Error("The campaign report could not be opened.");
  target.opener = null;
  try {
    const response = await fetcher(url, { method: "HEAD" });
    if (!response.ok) throw new Error("The campaign report is unavailable.");
    target.location.replace(url);
  } catch {
    target.close();
    throw new Error("The campaign report is unavailable.");
  }
}

export function campaignTaskRows(campaign: Campaign, tasks: AgentTask[]): CampaignTaskRow[] {
  const campaignTasks = tasks
    .filter((task) => task.campaign_id === campaign.campaign_id)
    .sort(compareTaskTime);
  const byId = new Map(campaignTasks.map((task) => [task.operation_id, task]));
  return campaignTasks.map((task) => ({
    task,
    role: campaignTaskRole(campaign, task),
    depth: campaignTaskDepth(task, byId),
  }));
}

export function campaignTaskRole(campaign: Campaign, task: AgentTask): CampaignTaskRole {
  const declared = task.request.role ?? task.request.campaign_role ?? task.request.invocation_role;
  if (declared === "report") return "report";
  if (task.request.campaign_phase === "report" || task.request.report_ending) return "report";
  if (
    task.request.wake_cause ||
    task.request.trigger === "watcher" ||
    task.request.continuation_cause === "graph_condition" ||
    task.request.continuation_cause === "message"
  ) {
    return "wake";
  }
  if (declared === "worker") return "worker";
  if (task.operation_id === campaign.root_operation_id) return "orchestrator";
  if (declared === "orchestrator" || declared === "wake") return declared;
  if (task.kind !== "campaign" || task.request.control_node_id || task.request.node_id) {
    return "worker";
  }
  return "orchestrator";
}

export function campaignTaskRoleLabel(role: CampaignTaskRole): string {
  switch (role) {
    case "orchestrator":
      return "Orchestrator";
    case "worker":
      return "Worker";
    case "wake":
      return "Wake";
    case "report":
      return "Report";
  }
}

export function formatTokenCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function campaignTaskDepth(task: AgentTask, byId: ReadonlyMap<string, AgentTask>): number {
  let depth = 0;
  let parentId = task.parent_operation_id;
  const seen = new Set<string>();
  while (parentId && byId.has(parentId) && !seen.has(parentId)) {
    seen.add(parentId);
    depth += 1;
    parentId = byId.get(parentId)?.parent_operation_id ?? null;
  }
  return Math.min(depth, 4);
}

function compareTaskTime(left: AgentTask, right: AgentTask): number {
  return (
    comparableTime(left.created_at) - comparableTime(right.created_at) ||
    left.operation_id.localeCompare(right.operation_id)
  );
}

function comparableTime(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}
