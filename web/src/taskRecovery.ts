import { isActiveTask } from "./agentTasks";
import type {
  AgentExecutionProfile,
  AgentRunConfig,
  AgentTask,
  Episode,
  ExperimentControlState,
  ProjectSnapshot,
} from "./types";

export function terminalTaskNeedsAuthoritativeProjectReload(task: AgentTask): boolean {
  return (
    task.kind === "branch_merge" ||
    Boolean(task.applied_revision) ||
    task.request.patch_kind === "experiment_loop"
  );
}

export function experimentControlsNeedWrapupPolling(
  controls: Readonly<Record<string, Pick<ExperimentControlState, "health">>>,
): boolean {
  return Object.values(controls).some((control) => control.health === "wrapping_up");
}

export function activeBranchMergeTask(episode: Episode): AgentTask | null {
  const operationId = episode.graph_branch?.active_merge_task_id;
  if (!operationId) return null;
  return (
    episode.tasks.find(
      (task) =>
        task.operation_id === operationId && task.kind === "branch_merge" && isActiveTask(task),
    ) ?? null
  );
}

export function taskActionNeedsAuthoritativeProjectReload(
  task: AgentTask,
  action: "pause" | "resume" | "retry",
): boolean {
  return task.request.patch_kind === "experiment_loop" && action !== "pause";
}

export function taskRetryConfig(task: AgentTask, project: ProjectSnapshot): AgentRunConfig {
  const profile = project.agent_profiles[retryProfileKind(task)];
  return {
    provider: task.request.provider || profile.provider,
    model: task.request.model ?? profile.model,
    reasoning: task.request.reasoning || profile.reasoning,
    run_on: task.request.run_on || profile.run_on,
  };
}

/** The profile whose defaults fill a retry the task's own request left unset. */
export function retryProfileKind(task: AgentTask): AgentExecutionProfile {
  if (task.kind === "seed" || task.kind === "refresh") return task.kind;
  if (task.kind === "auto_research") return "orchestrator";
  if (isExperimentLoopRecovery(task)) return "node_chat";
  return task.kind === "project_chat" || task.kind === "paper_coach" ? task.kind : "node_chat";
}

export function isExperimentLoopRecovery(task: AgentTask): boolean {
  return task.request.patch_kind === "experiment_loop";
}

/**
 * A turn bound to an episode keeps the machine its watchers and stage live on;
 * a standalone turn may move to a reachable one. Everything else is rebindable
 * on every recovery.
 */
export function taskRetryRequestBody(
  task: AgentTask,
  config: AgentRunConfig,
): AgentRunConfig | Omit<AgentRunConfig, "run_on"> {
  const rebound = {
    provider: config.provider,
    model: config.model,
    reasoning: config.reasoning,
  };
  return task.episode_id ? rebound : { ...rebound, run_on: config.run_on };
}
