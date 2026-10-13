import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isActiveTask, projectActivityTask } from "./agentTasks";
import { useResource } from "../core/useResource";
import {
  selectTasks,
  taskProgressProfile,
  taskResourceProfile,
  taskStatusProfile,
} from "./taskResources";
import type { AgentTask } from "../core/types";

export interface AgentTasksSnapshot {
  retryTask: AgentTask | null;
  tasks: AgentTask[];
  taskInspectorId: string | null;
  inspectedTask: AgentTask | null;
  activityTaskId: string | null;
}

interface UseAgentTasksOptions {
  projectId: string | null;
  reportError: (message: string) => void;
}

export function cloneAgentTasksSnapshot(snapshot: AgentTasksSnapshot): {
  retryTaskId: string | null;
  taskInspectorId: string | null;
  activityTaskId: string | null;
} {
  return {
    retryTaskId: snapshot.retryTask?.operation_id ?? null,
    taskInspectorId: snapshot.taskInspectorId,
    activityTaskId: snapshot.activityTaskId,
  };
}

export function useAgentTasks({ projectId }: UseAgentTasksOptions) {
  const owner = `${projectId}:task-view`;
  const status = useResource(owner, projectId ? taskStatusProfile(projectId) : null);
  const progress = useResource(owner, projectId ? taskProgressProfile(projectId) : null);
  const tasks = useMemo(
    () => selectTasks(status.value, progress.value),
    [status.value, progress.value],
  );
  const [retryTaskId, setRetryTaskId] = useState<string | null>(null);
  const retryTask = tasks.find((task) => task.operation_id === retryTaskId) ?? null;
  const [taskStarting, setTaskStarting] = useState(false);
  const [taskActionId, setTaskActionId] = useState<string | null>(null);
  const [taskInspectorId, setTaskInspectorId] = useState<string | null>(null);
  const detail = useResource(
    owner,
    projectId && taskInspectorId ? taskResourceProfile(projectId, taskInspectorId) : null,
  );
  const inspectorSummary = tasks.find((task) => task.operation_id === taskInspectorId);
  const inspectedTask = useMemo(
    () =>
      detail.value
        ? { ...detail.value, ...inspectorSummary, events: detail.value.events }
        : (inspectorSummary ?? null),
    [detail.value, inspectorSummary],
  );
  const taskInspectorLoading = !!taskInspectorId && detail.loading;
  const [activityTaskId, setActivityTaskId] = useState<string | null>(null);
  const taskStartLock = useRef(false);
  const taskActionLock = useRef(false);
  const activeTask = useMemo(() => tasks.find(isActiveTask) ?? null, [tasks]);
  const activityTask = projectActivityTask(tasks, activityTaskId);

  useEffect(() => {
    if (activityTask && (isActiveTask(activityTask) || activityTask.paused)) {
      setActivityTaskId(activityTask.operation_id);
    }
  }, [activityTask]);

  const recordStartedTask = useCallback((task: AgentTask) => {
    setActivityTaskId(task.operation_id);
  }, []);

  const presentTask = useCallback((task: AgentTask) => {
    setActivityTaskId(task.operation_id);
    setTaskInspectorId(task.operation_id);
  }, []);

  const selectTaskInspector = useCallback((operationId: string | null) => {
    setTaskInspectorId(operationId);
  }, []);

  const chooseRetryTask = useCallback((task: AgentTask) => {
    setRetryTaskId(task.operation_id);
  }, []);

  const closeRetryTask = useCallback(() => {
    setRetryTaskId(null);
  }, []);

  const beginTaskStart = useCallback(() => {
    if (taskStartLock.current || taskStarting) return null;
    taskStartLock.current = true;
    setTaskStarting(true);
    return () => {
      taskStartLock.current = false;
      setTaskStarting(false);
    };
  }, [taskStarting]);

  const beginTaskAction = useCallback((operationId: string) => {
    if (taskActionLock.current) return null;
    taskActionLock.current = true;
    setTaskActionId(operationId);
    return () => {
      taskActionLock.current = false;
      setTaskActionId(null);
    };
  }, []);

  const beginTaskRepair = useCallback(
    (operationId: string) => {
      if (taskStartLock.current || taskStarting || taskActionLock.current) return null;
      taskActionLock.current = true;
      taskStartLock.current = true;
      setTaskStarting(true);
      setTaskActionId(operationId);
      return () => {
        taskStartLock.current = false;
        taskActionLock.current = false;
        setTaskStarting(false);
        setTaskActionId(null);
      };
    },
    [taskStarting],
  );

  const resetProjectTasks = useCallback(() => {
    setRetryTaskId(null);
    setTaskInspectorId(null);
    setActivityTaskId(null);
  }, []);

  const restoreProjectTasks = useCallback(
    (snapshot: ReturnType<typeof cloneAgentTasksSnapshot>) => {
      setRetryTaskId(snapshot.retryTaskId);
      setTaskInspectorId(snapshot.taskInspectorId);
      setActivityTaskId(snapshot.activityTaskId);
    },
    [],
  );

  const snapshot = useMemo<AgentTasksSnapshot>(
    () => ({
      retryTask,
      tasks,
      taskInspectorId,
      inspectedTask,
      activityTaskId,
    }),
    [activityTaskId, inspectedTask, retryTask, taskInspectorId, tasks],
  );

  return {
    snapshot,
    taskStarting,
    taskActionId,
    taskInspectorLoading,
    activeTask,
    activityTask,
    recordStartedTask,
    presentTask,
    selectTaskInspector,
    chooseRetryTask,
    closeRetryTask,
    beginTaskStart,
    beginTaskAction,
    beginTaskRepair,
    resetProjectTasks,
    restoreProjectTasks,
  };
}
