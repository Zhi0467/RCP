import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isActiveTask, projectActivityTask } from "../agentTasks";
import { api } from "../api";
import type { AgentTask } from "../types";

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

export function cloneAgentTasksSnapshot(snapshot: AgentTasksSnapshot): AgentTasksSnapshot {
  return {
    retryTask: snapshot.retryTask,
    tasks: [...snapshot.tasks],
    taskInspectorId: snapshot.taskInspectorId,
    inspectedTask: snapshot.inspectedTask,
    activityTaskId: snapshot.activityTaskId,
  };
}

export function reconcileKnownActiveTasks(
  knownActive: Map<string, AgentTask>,
  current: AgentTask[],
): AgentTask[] {
  const terminal = current.filter(
    (task) => knownActive.has(task.operation_id) && !isActiveTask(task),
  );
  for (const task of terminal) knownActive.delete(task.operation_id);
  for (const task of current) {
    if (isActiveTask(task)) knownActive.set(task.operation_id, task);
  }
  return terminal;
}

export function useAgentTasks({ projectId, reportError }: UseAgentTasksOptions) {
  const [retryTask, setRetryTask] = useState<AgentTask | null>(null);
  const [taskStarting, setTaskStarting] = useState(false);
  const [taskActionId, setTaskActionId] = useState<string | null>(null);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [taskInspectorId, setTaskInspectorId] = useState<string | null>(null);
  const [inspectedTask, setInspectedTask] = useState<AgentTask | null>(null);
  const [taskInspectorLoading, setTaskInspectorLoading] = useState(false);
  const [activityTaskId, setActivityTaskId] = useState<string | null>(null);
  const taskStartLock = useRef(false);
  const knownActiveTasks = useRef(new Map<string, AgentTask>());

  const rememberActiveTasks = useCallback((nextTasks: AgentTask[]) => {
    for (const task of nextTasks) {
      if (isActiveTask(task)) knownActiveTasks.current.set(task.operation_id, task);
    }
  }, []);

  const activeTask = useMemo(() => tasks.find(isActiveTask) ?? null, [tasks]);
  const activityTask = projectActivityTask(tasks, activityTaskId);

  useEffect(() => {
    if (activityTask && (isActiveTask(activityTask) || activityTask.paused)) {
      setActivityTaskId(activityTask.operation_id);
    }
  }, [activityTask]);

  const inspectorSummary = tasks.find((task) => task.operation_id === taskInspectorId);
  const inspectorVersion = inspectorSummary?.updated_at;
  useEffect(() => {
    if (!inspectorSummary) return;
    setInspectedTask((current) =>
      current?.operation_id === inspectorSummary.operation_id
        ? { ...current, ...inspectorSummary, events: current.events }
        : current,
    );
  }, [inspectorSummary]);

  useEffect(() => {
    if (!projectId || !taskInspectorId) {
      setInspectedTask(null);
      return;
    }
    let cancelled = false;
    setTaskInspectorLoading(true);
    api<AgentTask>(`/api/projects/${encodeURIComponent(projectId)}/tasks/${taskInspectorId}`)
      .then((task) => {
        if (!cancelled) setInspectedTask(task);
      })
      .catch((error) => {
        if (!cancelled) reportError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!cancelled) setTaskInspectorLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [inspectorVersion, projectId, taskInspectorId]);

  const replaceTasks = useCallback(
    (nextTasks: AgentTask[]) => {
      rememberActiveTasks(nextTasks);
      setTasks(nextTasks);
    },
    [rememberActiveTasks],
  );

  const upsertTask = useCallback(
    (task: AgentTask) => {
      rememberActiveTasks([task]);
      setTasks((current) => [
        task,
        ...current.filter((item) => item.operation_id !== task.operation_id),
      ]);
    },
    [rememberActiveTasks],
  );

  const recordStartedTask = useCallback(
    (task: AgentTask) => {
      rememberActiveTasks([task]);
      setTasks((current) => [
        task,
        ...current.filter((item) => item.operation_id !== task.operation_id),
      ]);
      setActivityTaskId(task.operation_id);
    },
    [rememberActiveTasks],
  );

  const consumeTerminalTasks = useCallback(
    (nextTasks: AgentTask[]) => reconcileKnownActiveTasks(knownActiveTasks.current, nextTasks),
    [],
  );

  const presentTask = useCallback((task: AgentTask) => {
    setActivityTaskId(task.operation_id);
    setTaskInspectorId(task.operation_id);
    setInspectedTask(task);
  }, []);

  const selectTaskInspector = useCallback((operationId: string | null) => {
    setTaskInspectorId(operationId);
  }, []);

  const chooseRetryTask = useCallback((task: AgentTask) => {
    setRetryTask(task);
  }, []);

  const closeRetryTask = useCallback(() => {
    setRetryTask(null);
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

  const beginTaskAction = useCallback(
    (operationId: string) => {
      if (taskActionId) return null;
      setTaskActionId(operationId);
      return () => setTaskActionId(null);
    },
    [taskActionId],
  );

  const beginTaskRepair = useCallback(
    (operationId: string) => {
      if (taskStartLock.current || taskStarting || taskActionId) return null;
      taskStartLock.current = true;
      setTaskStarting(true);
      setTaskActionId(operationId);
      return () => {
        taskStartLock.current = false;
        setTaskStarting(false);
        setTaskActionId(null);
      };
    },
    [taskActionId, taskStarting],
  );

  const resetProjectTasks = useCallback(() => {
    knownActiveTasks.current.clear();
    setRetryTask(null);
    setTasks([]);
    setTaskInspectorId(null);
    setInspectedTask(null);
    setActivityTaskId(null);
  }, []);

  const restoreProjectTasks = useCallback(
    (snapshot: AgentTasksSnapshot) => {
      knownActiveTasks.current.clear();
      rememberActiveTasks(snapshot.tasks);
      setRetryTask(snapshot.retryTask);
      setTasks([...snapshot.tasks]);
      setTaskInspectorId(snapshot.taskInspectorId);
      setInspectedTask(snapshot.inspectedTask);
      setActivityTaskId(snapshot.activityTaskId);
    },
    [rememberActiveTasks],
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
    replaceTasks,
    consumeTerminalTasks,
    upsertTask,
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
