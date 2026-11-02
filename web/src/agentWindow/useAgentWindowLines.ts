import { useMemo } from "react";
import {
  chatTasksMissingFromHistory,
  isActiveTask,
  orderTranscriptLines,
  reconcileChatHistoryArtifacts,
  reconstructTaskTranscript,
  withoutRepeatedTurnPrompts,
} from "../agents/agentTasks";
import { chatStatusTasks, compactionAnchors } from "../chat/agentStatusModel";
import type {
  AgentTask,
  ChatAttachmentDescriptor,
  ChatMessage,
  ConversationMode,
} from "../core/types";

export interface PendingAgentWindowTurn {
  clientId: string;
  text: string;
  timestamp: string;
  mode: ConversationMode;
  attachments: ChatAttachmentDescriptor[];
  error?: string;
}

/** Whether history already carries the in-flight store overlay. */
export function pendingTurnShownByHistory(
  clientId: string,
  messages: readonly { operation_id?: string | null }[],
): boolean {
  return messages.some((message) => message.operation_id === clientId);
}

const NO_PARENTS: ReadonlyMap<string, string | null> = new Map();

export function useAgentWindowLines({
  messages,
  tasks,
  pendingTurn = null,
  parents = NO_PARENTS,
}: {
  messages: ChatMessage[];
  tasks: AgentTask[];
  pendingTurn?: PendingAgentWindowTurn | null;
  /** Retry and Resume links of attempts not among `tasks`. */
  parents?: ReadonlyMap<string, string | null>;
}) {
  const transcript = useMemo(
    () => [
      ...orderTranscriptLines(
        withoutRepeatedTurnPrompts(
          [
            ...reconcileChatHistoryArtifacts(messages, tasks),
            ...reconstructTaskTranscript(chatTasksMissingFromHistory(tasks, messages)),
          ],
          new Map([
            ...parents,
            ...tasks.map((task) => [task.operation_id, task.parent_operation_id ?? null] as const),
          ]),
        ),
      ),
      ...(pendingTurn &&
      !pendingTurnShownByHistory(pendingTurn.clientId, messages) &&
      !tasks.some((task) => task.operation_id === pendingTurn.clientId)
        ? [
            {
              lineId: `pending:${pendingTurn.clientId}`,
              role: "human" as const,
              text: pendingTurn.text,
              taskId: pendingTurn.clientId,
              timestamp: pendingTurn.timestamp,
              mode: pendingTurn.mode,
              attachments: pendingTurn.attachments,
              trigger: "human" as const,
            },
          ]
        : []),
    ],
    [messages, parents, pendingTurn, tasks],
  );
  const statusTasks = useMemo(() => chatStatusTasks(tasks, messages), [tasks, messages]);
  const compactionByLine = useMemo(
    () => compactionAnchors(transcript, statusTasks),
    [statusTasks, transcript],
  );
  const continuedTaskIds = useMemo(
    () =>
      new Set(
        tasks.flatMap((task) => (task.parent_operation_id ? [task.parent_operation_id] : [])),
      ),
    [tasks],
  );
  const latestGraphReceiptLineIds = useMemo(() => {
    const latest = new Map<string, string>();
    for (const line of transcript)
      if (line.role === "agent" && line.graphUpdate) latest.set(line.taskId, line.lineId);
    return new Set(latest.values());
  }, [transcript]);
  const inlineArtifactRefreshToken = useMemo(
    () =>
      tasks
        .filter((task) => !isActiveTask(task))
        .map((task) => `${task.operation_id}:${task.status}:${task.updated_at}`)
        .join("|"),
    [tasks],
  );
  return {
    transcript,
    statusTasks,
    compactionByLine,
    continuedTaskIds,
    latestGraphReceiptLineIds,
    inlineArtifactRefreshToken,
  };
}
