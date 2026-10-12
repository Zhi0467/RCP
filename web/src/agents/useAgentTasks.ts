import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isActiveTask, projectActivityTask } from "./agentTasks";
import { api } from "../core/api";
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

/**
 * A task list read numbered `read` when it was requested replaces the list,
 * except for tasks this page recorded after that request: the read was answered
 * before they existed (or changed), so the recorded version stays.
 */
export function mergeTaskListRead(
  current: readonly AgentTask[],
  listed: readonly AgentTask[],
  localWrites: ReadonlyMap<string, number>,
  read: number,
): AgentTask[] {
  const newer = new Map(
    current
      .filter((task) => (localWrites.get(task.operation_id) ?? 0) > read)
      .map((task) => [task.operation_id, task]),
  );
  if (newer.size === 0) return [...listed];
  const merged = listed.map((task) => newer.get(task.operation_id) ?? task);
  const listedIds = new Set(listed.map((task) => task.operation_id));
  return [...[...newer.values()].filter((task) => !listedIds.has(task.operation_id)), ...merged];
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
  // One sequence numbers list reads when requested and local task records when
  // made. Reads answer out of order on a slow link; only the newest requested
  // read applies, and it never drops a record made after it was requested.
  const taskSequence = useRef(0);
  const appliedTaskRead = useRef(0);
  const localTaskWrites = useRef(new Map<string, number>());

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
  }, [inspectorVersion, projectId, reportError, taskInspectorId]);

  /** Number a task list read before requesting it; pass the number to `replaceTasks`. */
  const beginTasksRead = useCallback(() => ++taskSequence.current, []);

  /** Apply a list read unless a newer one already applied; false when discarded. */
  const replaceTasks = useCallback(
    (nextTasks: AgentTask[], read: number): boolean => {
      if (read < appliedTaskRead.current) return false;
      appliedTaskRead.current = read;
      for (const [operationId, written] of localTaskWrites.current) {
        if (written <= read) localTaskWrites.current.delete(operationId);
      }
      const localWrites = new Map(localTaskWrites.current);
      rememberActiveTasks(nextTasks);
      setTasks((current) => mergeTaskListRead(current, nextTasks, localWrites, read));
      return true;
    },
    [rememberActiveTasks],
  );

  const upsertTask = useCallback(
    (task: AgentTask) => {
      localTaskWrites.current.set(task.operation_id, ++taskSequence.current);
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
      upsertTask(task);
      setActivityTaskId(task.operation_id);
    },
    [upsertTask],
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

  // Reads requested for the previous project state never apply to the new one.
  const discardPendingTaskReads = useCallback(() => {
    appliedTaskRead.current = ++taskSequence.current;
    localTaskWrites.current.clear();
  }, []);

  const resetProjectTasks = useCallback(() => {
    discardPendingTaskReads();
    knownActiveTasks.current.clear();
    setRetryTask(null);
    setTasks([]);
    setTaskInspectorId(null);
    setInspectedTask(null);
    setActivityTaskId(null);
  }, [discardPendingTaskReads]);

  const restoreProjectTasks = useCallback(
    (snapshot: AgentTasksSnapshot) => {
      discardPendingTaskReads();
      knownActiveTasks.current.clear();
      rememberActiveTasks(snapshot.tasks);
      setRetryTask(snapshot.retryTask);
      setTasks([...snapshot.tasks]);
      setTaskInspectorId(snapshot.taskInspectorId);
      setInspectedTask(snapshot.inspectedTask);
      setActivityTaskId(snapshot.activityTaskId);
    },
    [discardPendingTaskReads, rememberActiveTasks],
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
    beginTasksRead,
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
